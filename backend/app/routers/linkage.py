"""筛选器批量联动编排接口：多选筛选器一次提交，整组校验、整组发布、审批回写。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, LinkagePayload, PageResult
from app.services.linkage import LinkageService

router = APIRouter(prefix="/api/filter/linkage", tags=["筛选器联动编排"])

service = LinkageService()


@router.post("/validate")
def validate_group(payload: LinkagePayload) -> dict[str, Any]:
    """整组校验：逐条返回筛选器结果、维度裁决与冲突明细；不落任何数据。"""
    return service.validate_group(payload.filter_codes, payload.dimensions)


@router.post("/publish", response_model=ActionResult)
def publish_group(payload: LinkagePayload) -> ActionResult:
    """整组发布：任一绑定失败则整组不发布；重复提交同一方案只保留一条有效记录。"""
    plan, duplicated, message = service.publish_group(
        payload.filter_codes,
        payload.dimensions,
        remark=payload.remark or "",
    )
    ok = plan.get("status") == "待审批"
    if duplicated:
        message = f"{message}；方案状态：{plan.get('status')}"
    return ActionResult(ok=ok or duplicated, message=message, entry=plan)


@router.get("/plans", response_model=PageResult[dict])
def list_plans(
    status: str | None = Query(default=None, description="待审批、已生效、已驳回、校验失败"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """审批队列：发布联动方案后这里同步生成一条完整方案记录。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_plans(status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/plans/{plan_id}", response_model=dict)
def get_plan(plan_id: int) -> dict:
    """方案详情：含校验报告、裁决结果、失败筛选器、冲突维度与未生效范围。"""
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"联动方案 {plan_id} 不存在")
    return plan


@router.post("/plans/{plan_id}/actions", response_model=ActionResult)
def run_action(plan_id: int, payload: EntryPayload) -> ActionResult:
    """审批通过 / 审批驳回；通过时把同一份联动结果一次性回写筛选器、图表与看板。"""
    action = str(payload.values.get("action") or "").strip()
    remark = str(payload.values.get("remark") or payload.remark or "").strip()
    plan, message = service.run_action(plan_id, action, remark=remark)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=plan)
