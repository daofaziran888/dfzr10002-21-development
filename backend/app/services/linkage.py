"""筛选器批量联动编排：整组校验、业务优先级裁决、幂等发布与审批回写。

业务规则收在这里：
- 整组校验：类型、默认值来源、联动维度逐条校验，任一失败则整组不可发布；
- 冲突裁决：同一联动维度被多个筛选器争夺时按业务优先级只保留一个主筛选器；
  已启用且标记「绑定锁定」的图表把维度锁定给某筛选器，锁定者在方案内时优先保持，
  锁定者不在方案内而主筛选器易主时记为冲突、整组不发布；
- 业务优先级：筛选器上显式填写的「业务优先级」分值优先；缺省按状态权重
  （已启用 300 / 待校验 200 / 草稿 100 / 已停用 0）加启用加成 50 推导，同分按编号升序；
- 原子发布：校验失败只落一条方案记录，筛选器 / 图表 / 看板配置零改动；
  审批通过时先构造全部回写变更再一次性落库，不留半生效配置；
- 幂等：方案指纹 = 排序后的筛选器编号 + 联动维度，重复提交复用同一条记录。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "linkage"
FILTER_MODULE = "filter"
CHART_MODULE = "chart"
DASHBOARD_MODULE = "dashboard"
DIMENSION_MODULE = "dimension"

PLAN_PREFIX = "PLAN"
# 支持联动的筛选类型：自由文本类无法驱动维度联动
LINKABLE_TYPES = {"下拉单选", "下拉多选", "级联选择"}
# 业务优先级推导权重（显式「业务优先级」字段可覆盖）
STATUS_WEIGHT = {"已启用": 300, "待校验": 200, "草稿": 100, "已停用": 0}
ENABLED_BONUS = 50
# 可选值来源前缀 -> (目标模块, 编号字段)
SOURCE_MODULES = {"维度": (DIMENSION_MODULE, "维度编号"), "数据集": ("dataset", "数据集编号")}
SOURCE_DEAD_STATUSES = {"已停用", "已下线"}
# 有效方案状态：同指纹重复提交不新增第二条
ACTIVE_STATUSES = {"待审批", "已生效"}
ACTION_RULES = {"审批通过": "已生效", "审批驳回": "已驳回"}
SPLIT_PATTERN = re.compile(r"[,，、/]")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _split(text: Any) -> list[str]:
    return [part.strip() for part in SPLIT_PATTERN.split(str(text or "")) if part.strip()]


def _find_by_code(module: str, field: str, code: str) -> dict[str, Any] | None:
    for row in store.rows(module):
        if str(row.get(field, "")).strip() == code:
            return row
    return None


def _chart_pairs(chart: dict[str, Any]) -> list[tuple[str, str]]:
    """把图表「筛选条件」解析成 (维度, 筛选器编号) 列表。"""
    pairs: list[tuple[str, str]] = []
    for item in _split(chart.get("筛选条件")):
        if "=" in item:
            dim, code = item.split("=", 1)
            pairs.append((dim.strip(), code.strip()))
    return pairs


def business_priority(entry: dict[str, Any]) -> tuple[int, str]:
    """业务优先级：显式分值 > 状态权重 + 启用加成；返回 (分值, 依据说明)。"""
    explicit = str(entry.get("业务优先级") or "").strip()
    if explicit.isdigit():
        return int(explicit), f"显式业务优先级 {explicit}"
    score = STATUS_WEIGHT.get(str(entry.get("status", "")), 0)
    basis = f"状态权重 {score}"
    if str(entry.get("是否启用") or "").strip() == "是":
        score += ENABLED_BONUS
        basis += f" + 启用加成 {ENABLED_BONUS}"
    return score, basis


class LinkageService:
    # ---------- 整组校验 ----------
    def validate_group(self, filter_codes: list[str], dimensions: list[str]) -> dict[str, Any]:
        codes = [str(code).strip() for code in filter_codes if str(code).strip()]
        dims = [str(dim).strip() for dim in dimensions if str(dim).strip()]
        report: dict[str, Any] = {
            "ok": True,
            "筛选器": codes,
            "联动维度": dims,
            "维度校验": [],
            "筛选器校验": [],
            "裁决结果": [],
            "冲突列表": [],
            "失败筛选器": [],
            "冲突维度": [],
            "影响图表": [],
            "影响看板": [],
            "刷新顺序": [],
            "未生效范围": {},
            "提示": [],
        }
        if not codes:
            report["ok"] = False
            report["提示"].append("未选择任何筛选器，无法编排联动")
            return report
        if not dims:
            report["ok"] = False
            report["提示"].append("未选择任何联动维度，无法编排联动")
            return report

        # 1. 联动维度必须存在且已发布
        for name in dims:
            row = _find_by_code(DIMENSION_MODULE, "维度名称", name)
            if row is None:
                report["ok"] = False
                report["维度校验"].append({"维度": name, "结果": "失败", "说明": "维度不存在"})
            elif row.get("status") != "已发布":
                report["ok"] = False
                report["维度校验"].append({"维度": name, "结果": "失败", "说明": f"维度状态为「{row.get('status')}」，未发布"})
            else:
                report["维度校验"].append({"维度": name, "结果": "通过", "说明": row.get("维度编号", "")})

        # 2. 筛选器逐条校验：存在性、状态、类型、默认值来源、命中维度
        entries: dict[str, dict[str, Any]] = {}
        hit_dims: dict[str, list[str]] = {}
        failed: set[str] = set()
        for code in codes:
            entry = _find_by_code(FILTER_MODULE, "筛选器编号", code)
            item: dict[str, Any] = {"编号": code, "结果": "通过", "说明": [], "命中维度": [], "业务优先级": 0, "优先级依据": ""}
            if entry is None:
                item["结果"] = "失败"
                item["说明"].append("筛选器不存在或已归档")
                failed.add(code)
                report["筛选器校验"].append(item)
                continue
            entries[code] = entry
            score, basis = business_priority(entry)
            item["业务优先级"] = score
            item["优先级依据"] = basis
            if entry.get("status") == "已停用":
                item["结果"] = "失败"
                item["说明"].append("筛选器已停用，需先启用再编排")
            if str(entry.get("筛选类型") or "").strip() not in LINKABLE_TYPES:
                item["结果"] = "失败"
                item["说明"].append(f"筛选类型「{entry.get('筛选类型')}」不支持维度联动")
            source = str(entry.get("可选值来源") or "").strip()
            source_ok, source_msg = self._check_source(source)
            if not source_ok:
                item["结果"] = "失败"
                item["说明"].append(f"默认值来源不可用：{source_msg}")
            hits = [dim for dim in dims if dim in _split(entry.get("联动维度"))]
            hit_dims[code] = hits
            item["命中维度"] = hits
            if not hits:
                if item["结果"] == "通过":
                    item["结果"] = "警告"
                item["说明"].append("未命中本次联动维度，不参与编排")
            if item["结果"] == "失败":
                failed.add(code)
            report["筛选器校验"].append(item)

        # 3. 维度主筛选器裁决：同一维度被多个筛选器争夺时按业务优先级只保留一个；
        #    已启用且「绑定锁定」的图表把维度锁定给某筛选器，锁定者在方案内时优先保持
        locked: dict[str, list[tuple[str, str]]] = {}
        for chart in store.rows(CHART_MODULE):
            if chart.get("status") != "已启用" or str(chart.get("绑定锁定") or "").strip() != "是":
                continue
            for dim, code in _chart_pairs(chart):
                locked.setdefault(dim, []).append((str(chart.get("图表编号", "")), code))

        def _rank(code: str) -> tuple[int, str]:
            return (-business_priority(entries[code])[0], code)

        winners: dict[str, dict[str, Any]] = {}
        for dim in dims:
            candidates = [
                code for code in codes
                if code in entries and code not in failed and dim in hit_dims.get(code, [])
            ]
            if not candidates:
                continue
            locked_codes = {code for _, code in locked.get(dim, [])}
            kept = sorted(code for code in candidates if code in locked_codes)
            if kept:
                ordered = kept + sorted((code for code in candidates if code not in kept), key=_rank)
            else:
                ordered = sorted(candidates, key=_rank)
            winner = ordered[0]
            winner_kept = winner in locked_codes
            covered = []
            for loser in ordered[1:]:
                default = str(entries[loser].get("默认值") or "").strip()
                if default:
                    covered.append({"筛选器": loser, "默认值": default})
                    reason = "图表锁定保持" if winner_kept else (
                        f"业务优先级 {business_priority(entries[winner])[0]} > {business_priority(entries[loser])[0]}"
                    )
                    for item in report["筛选器校验"]:
                        if item["编号"] == loser:
                            if item["结果"] == "通过":
                                item["结果"] = "警告"
                            item["说明"].append(f"维度「{dim}」默认值被 {winner} 覆盖（{reason}）")
            score, basis = business_priority(entries[winner])
            if winner_kept:
                basis = f"图表锁定保持（{basis}）"
            winners[dim] = {
                "维度": dim,
                "主筛选器": winner,
                "生效默认值": str(entries[winner].get("默认值") or "").strip(),
                "业务优先级": score,
                "裁决依据": basis,
                "被覆盖": covered,
            }
        report["裁决结果"] = [winners[dim] for dim in dims if dim in winners]

        # 4. 图表锁定冲突：锁定筛选器不在方案内、主筛选器易主即冲突
        for dim, arb in winners.items():
            for chart_code, locked_code in locked.get(dim, []):
                if locked_code != arb["主筛选器"] and locked_code not in codes:
                    owner = arb["主筛选器"]
                    if owner not in failed:
                        failed.add(owner)
                        for item in report["筛选器校验"]:
                            if item["编号"] == owner:
                                item["结果"] = "失败"
                                item["说明"].append(
                                    f"与图表 {chart_code} 冲突：维度「{dim}」已被该图表锁定给 {locked_code}"
                                )
                    report["冲突列表"].append({
                        "筛选器": owner,
                        "维度": dim,
                        "图表": chart_code,
                        "说明": f"图表 {chart_code} 已启用且维度「{dim}」锁定给 {locked_code}，方案主筛选器为 {owner}",
                    })
                    if dim not in report["冲突维度"]:
                        report["冲突维度"].append(dim)

        # 5. 受影响图表 / 看板与明确的刷新顺序
        affected_charts = self._affected_charts(codes, dims)
        affected_dashboards = self._affected_dashboards(affected_charts)
        report["影响图表"] = affected_charts
        report["影响看板"] = affected_dashboards
        report["刷新顺序"] = self._refresh_order(affected_charts, affected_dashboards)

        # 6. 汇总：任一失败则整组不可发布，并给出未生效范围
        report["失败筛选器"] = [code for code in codes if code in failed]
        if report["失败筛选器"]:
            report["ok"] = False
        if not report["ok"]:
            report["未生效范围"] = {
                "筛选器": codes,
                "图表": [chart["图表编号"] for chart in affected_charts],
                "看板": [dash["面板编号"] for dash in affected_dashboards],
            }
            report["提示"].append("整组校验未通过：所有筛选器与图表配置保持原状，未产生任何部分生效")
        return report

    def _check_source(self, source: str) -> tuple[bool, str]:
        if not source:
            return False, "可选值来源为空"
        if ":" not in source:
            return False, f"来源「{source}」格式应为 类型:编号"
        kind, code = source.split(":", 1)
        kind, code = kind.strip(), code.strip()
        target = SOURCE_MODULES.get(kind)
        if target is None:
            return False, f"来源类型「{kind}」不支持（应为 维度 或 数据集）"
        module, field = target
        row = _find_by_code(module, field, code)
        if row is None:
            return False, f"{kind} {code} 不存在"
        if str(row.get("status", "")) in SOURCE_DEAD_STATUSES:
            return False, f"{kind} {code} 已停用/下线"
        return True, f"{kind} {code} 可用"

    def _affected_charts(self, codes: list[str], dims: list[str]) -> list[dict[str, Any]]:
        affected = []
        for chart in store.rows(CHART_MODULE):
            pairs = _chart_pairs(chart)
            dims_in = {dim for dim, _ in pairs}
            codes_in = {code for _, code in pairs}
            if dims_in & set(dims) or codes_in & set(codes):
                affected.append({
                    "图表编号": str(chart.get("图表编号", "")),
                    "图表名称": str(chart.get("图表名称", "")),
                    "状态": str(chart.get("status", "")),
                })
        return affected

    def _affected_dashboards(self, affected_charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chart_codes = {chart["图表编号"] for chart in affected_charts}
        affected = []
        for dash in store.rows(DASHBOARD_MODULE):
            if chart_codes & set(_split(dash.get("图表集合"))):
                affected.append({
                    "面板编号": str(dash.get("面板编号", "")),
                    "面板名称": str(dash.get("面板名称", "")),
                })
        return affected

    def _refresh_order(
        self,
        affected_charts: list[dict[str, Any]],
        affected_dashboards: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """明确刷新顺序：看板引用顺序优先，剩余图表按编号升序。"""
        chart_codes = {chart["图表编号"] for chart in affected_charts}
        ordered: list[str] = []
        for dash in store.rows(DASHBOARD_MODULE):
            if str(dash.get("面板编号", "")) not in {d["面板编号"] for d in affected_dashboards}:
                continue
            for code in _split(dash.get("图表集合")):
                if code in chart_codes and code not in ordered:
                    ordered.append(code)
        for code in sorted(chart_codes):
            if code not in ordered:
                ordered.append(code)
        return [{"序号": index + 1, "图表编号": code} for index, code in enumerate(ordered)]

    # ---------- 整组发布（幂等） ----------
    def publish_group(
        self,
        filter_codes: list[str],
        dimensions: list[str],
        remark: str = "",
    ) -> tuple[dict[str, Any], bool, str]:
        report = self.validate_group(filter_codes, dimensions)
        fingerprint = self._fingerprint(report["筛选器"], report["联动维度"])
        plans = store.rows(MODULE)
        existing = next((row for row in plans if row.get("指纹") == fingerprint), None)
        if existing is not None and existing.get("status") in ACTIVE_STATUSES:
            return existing, True, f"相同方案 {existing.get('方案编号')} 已存在（{existing.get('status')}），重复提交未新增"

        if existing is None:
            plan = {
                "id": max((int(row.get("id", 0)) for row in plans), default=0) + 1,
                "方案编号": f"{PLAN_PREFIX}-{max((int(row.get('id', 0)) for row in plans), default=0) + 1:04d}",
                "指纹": fingerprint,
            }
            plans.append(plan)
        else:
            plan = existing

        plan.update({
            "筛选器": report["筛选器"],
            "联动维度": report["联动维度"],
            "校验报告": report,
            "裁决结果": report["裁决结果"],
            "失败筛选器": report["失败筛选器"],
            "冲突维度": report["冲突维度"],
            "影响图表": report["影响图表"],
            "影响看板": report["影响看板"],
            "刷新顺序": report["刷新顺序"],
            "未生效范围": report["未生效范围"],
            "提交时间": _now(),
            "审批时间": None,
            "审批意见": remark,
            "回写明细": None,
        })
        if report["ok"]:
            plan["status"] = "待审批"
            plan["pending"] = True
            plan["abnormal"] = False
            message = f"联动方案 {plan['方案编号']} 整组校验通过，已发布进入审批队列"
        else:
            plan["status"] = "校验失败"
            plan["pending"] = False
            plan["abnormal"] = True
            message = (
                f"联动方案 {plan['方案编号']} 整组校验未通过，未发布；"
                f"失败筛选器 {len(report['失败筛选器'])} 个，全部配置保持原状"
            )
        return plan, False, message

    def _fingerprint(self, codes: list[str], dims: list[str]) -> str:
        raw = "|".join(sorted(codes)) + "#" + "|".join(sorted(dims))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    # ---------- 审批队列 ----------
    def list_plans(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, plan_id)

    # ---------- 审批回写 ----------
    def run_action(self, plan_id: int, action: str, remark: str = "") -> tuple[dict[str, Any] | None, str]:
        plan = store.find(MODULE, plan_id)
        if plan is None:
            return None, f"联动方案 {plan_id} 不存在"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于联动方案审批范围"
        if plan.get("status") != "待审批":
            return None, f"方案 {plan.get('方案编号')} 当前状态为「{plan.get('status')}」，仅待审批方案可审批"

        if action == "审批驳回":
            plan["status"] = "已驳回"
            plan["pending"] = False
            plan["abnormal"] = True
            plan["审批时间"] = _now()
            plan["审批意见"] = remark or "审批驳回"
            plan["未生效范围"] = {
                "筛选器": list(plan.get("筛选器", [])),
                "图表": [chart["图表编号"] for chart in plan.get("影响图表", [])],
                "看板": [dash["面板编号"] for dash in plan.get("影响看板", [])],
            }
            return plan, f"方案 {plan['方案编号']} 已驳回，筛选器与图表配置保持原状"

        # 审批通过：先按方案快照构造全部回写变更，全部就绪后一次性落库，不留半生效配置
        changes, error = self._build_writeback(plan)
        if error:
            plan["status"] = "已驳回"
            plan["pending"] = False
            plan["abnormal"] = True
            plan["审批时间"] = _now()
            plan["审批意见"] = f"回写中止：{error}"
            return None, f"回写中止，方案已驳回且未改动任何配置：{error}"
        for row, values in changes:
            row.update(values)

        plan["status"] = "已生效"
        plan["pending"] = False
        plan["abnormal"] = False
        plan["审批时间"] = _now()
        plan["审批意见"] = remark or "审批通过"
        plan["未生效范围"] = {}
        plan["回写明细"] = {
            "筛选器": list(plan.get("筛选器", [])),
            "图表": [item["图表编号"] for item in plan.get("刷新顺序", [])],
            "看板": [dash["面板编号"] for dash in plan.get("影响看板", [])],
            "刷新顺序": plan.get("刷新顺序", []),
        }
        return plan, f"方案 {plan['方案编号']} 审批通过，筛选器、图表与看板已看到同一份联动结果"

    def _build_writeback(self, plan: dict[str, Any]) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], str]:
        """按方案快照计算全部回写变更；任何目标缺失都整体中止，返回错误说明。"""
        winners = {arb["维度"]: arb for arb in plan.get("裁决结果", [])}
        covered_by: dict[str, list[dict[str, str]]] = {}
        for arb in plan.get("裁决结果", []):
            for item in arb.get("被覆盖", []):
                covered_by.setdefault(item["筛选器"], []).append({"维度": arb["维度"], "主筛选器": arb["主筛选器"]})

        changes: list[tuple[dict[str, Any], dict[str, Any]]] = []
        plan_code = str(plan.get("方案编号", ""))

        # 1. 筛选器：联动维度、联动默认值说明、方案归属、状态启用
        hit_map: dict[str, list[str]] = {}
        for item in plan.get("校验报告", {}).get("筛选器校验", []):
            hit_map[item["编号"]] = list(item.get("命中维度", []))
        for code in plan.get("筛选器", []):
            entry = _find_by_code(FILTER_MODULE, "筛选器编号", code)
            if entry is None:
                return [], f"筛选器 {code} 在审批期间被移除"
            hits = hit_map.get(code, [])
            default_notes = []
            for dim in hits:
                arb = winners.get(dim)
                if arb and arb["主筛选器"] == code:
                    default_notes.append(f"{dim}={arb['生效默认值']}（生效）")
            for item in covered_by.get(code, []):
                entry_default = str(entry.get("默认值") or "").strip()
                default_notes.append(f"{item['维度']}={entry_default}（被 {item['主筛选器']} 覆盖）")
            changes.append((entry, {
                "联动维度": "、".join(hits),
                "联动方案": plan_code,
                "联动默认值": "；".join(default_notes),
                "是否启用": "是",
                "status": "已启用",
                "pending": False,
                "abnormal": False,
            }))

        # 2. 图表：按刷新顺序重写筛选条件为维度主筛选器，标注方案与刷新序号
        order_map = {item["图表编号"]: item["序号"] for item in plan.get("刷新顺序", [])}
        for chart_code in order_map:
            chart = _find_by_code(CHART_MODULE, "图表编号", chart_code)
            if chart is None:
                return [], f"图表 {chart_code} 在审批期间被移除"
            pairs = _chart_pairs(chart)
            rewritten = [
                (dim, winners[dim]["主筛选器"] if dim in winners else code)
                for dim, code in pairs
            ]
            known_dims = {dim for dim, _ in rewritten}
            for dim, arb in winners.items():
                if dim not in known_dims:
                    rewritten.append((dim, arb["主筛选器"]))
            changes.append((chart, {
                "筛选条件": ",".join(f"{dim}={code}" for dim, code in rewritten),
                "联动方案": plan_code,
                "刷新序号": order_map[chart_code],
            }))

        # 3. 看板：标注同一份联动方案
        for dash in plan.get("影响看板", []):
            row = _find_by_code(DASHBOARD_MODULE, "面板编号", dash["面板编号"])
            if row is None:
                return [], f"看板 {dash['面板编号']} 在审批期间被移除"
            changes.append((row, {"联动方案": plan_code}))

        return changes, ""
