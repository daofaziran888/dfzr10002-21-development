import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/filter'
const LINKAGE_ENDPOINT = '/api/filter/linkage'
const columns = [
  "筛选器编号",
  "筛选器名称",
  "关联字段",
  "筛选类型",
  "默认值",
  "可选值来源",
  "联动维度",
  "是否启用",
  "联动方案"
]
const actions = [
  "提交校验",
  "启用配置",
  "停用配置"
]

type Row = Record<string, string | number | null>

type FilterCheck = {
  编号: string
  结果: string
  说明: string[]
  命中维度: string[]
  业务优先级: number
  优先级依据: string
}

type Arbitration = {
  维度: string
  主筛选器: string
  生效默认值: string
  业务优先级: number
  裁决依据: string
  被覆盖: { 筛选器: string; 默认值: string }[]
}

type Conflict = { 筛选器: string; 维度: string; 图表: string; 说明: string }

type Report = {
  ok: boolean
  筛选器校验: FilterCheck[]
  裁决结果: Arbitration[]
  冲突列表: Conflict[]
  失败筛选器: string[]
  冲突维度: string[]
  影响图表: { 图表编号: string; 图表名称: string; 状态: string }[]
  影响看板: { 面板编号: string; 面板名称: string }[]
  刷新顺序: { 序号: number; 图表编号: string }[]
  未生效范围: { 筛选器?: string[]; 图表?: string[]; 看板?: string[] }
  提示: string[]
}

type Plan = {
  id: number
  方案编号: string
  status: string
  筛选器: string[]
  联动维度: string[]
  提交时间: string
  审批时间?: string | null
  审批意见?: string
  失败筛选器?: string[]
  冲突维度?: string[]
  未生效范围?: { 筛选器?: string[]; 图表?: string[]; 看板?: string[] }
  刷新顺序?: { 序号: number; 图表编号: string }[]
  回写明细?: { 筛选器: string[]; 图表: string[]; 看板: string[] } | null
}

const badgeClass = (value: string) => {
  if (value === '通过' || value === '已生效') return 'badge ok'
  if (value === '警告' || value === '待审批') return 'badge warn'
  return 'badge fail'
}

export default function FilterView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const [selected, setSelected] = useState<string[]>([])
  const [dimOptions, setDimOptions] = useState<string[]>([])
  const [dims, setDims] = useState<string[]>([])
  const [report, setReport] = useState<Report | null>(null)
  const [plans, setPlans] = useState<Plan[]>([])
  const [expandedPlan, setExpandedPlan] = useState<number | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('筛选器列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '全局筛选器列表读取失败')
    }
  }, [filters])

  const reloadPlans = useCallback(async () => {
    try {
      const response = await request(`${LINKAGE_ENDPOINT}/plans?size=50`)
      if (!response.ok) {
        throw new Error('联动方案审批队列读取失败')
      }
      const payload = await response.json()
      setPlans(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '联动方案审批队列读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadPlans()
  }, [reload, reloadPlans])

  useEffect(() => {
    request('/api/dimension?size=200')
      .then((response) => (response.ok ? response.json() : { items: [] }))
      .then((payload: { items?: Row[] }) => {
        const names = (payload.items ?? [])
          .filter((item) => item.status === '已发布')
          .map((item) => String(item['维度名称']))
        setDimOptions(names)
      })
      .catch(() => setDimOptions([]))
  }, [])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('全局筛选器动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '全局筛选器操作失败')
    }
  }

  function toggleSelect(code: string) {
    setSelected((prev) => (prev.includes(code) ? prev.filter((item) => item !== code) : [...prev, code]))
  }

  function toggleDim(name: string) {
    setDims((prev) => (prev.includes(name) ? prev.filter((item) => item !== name) : [...prev, name]))
  }

  async function postLinkage(path: string): Promise<any> {
    setErrorMessage('')
    setNotice('')
    if (selected.length === 0 || dims.length === 0) {
      setErrorMessage('请先勾选筛选器与联动维度，再整组提交')
      return null
    }
    try {
      const response = await request(`${LINKAGE_ENDPOINT}/${path}`, {
        method: 'POST',
        body: JSON.stringify({ filter_codes: selected, dimensions: dims }),
      })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload.detail ?? '联动编排请求失败')
      }
      return payload
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '联动编排请求失败')
      return null
    }
  }

  async function validateGroup() {
    const payload = await postLinkage('validate')
    if (payload) {
      setReport(payload as unknown as Report)
      setNotice(payload.ok ? '整组校验通过，可以整组发布' : '整组校验未通过，请逐条处理下方失败项')
    }
  }

  async function publishGroup() {
    const payload = await postLinkage('publish')
    if (!payload) return
    setNotice(payload.message)
    const plan = payload.entry as (Plan & { 校验报告?: Report }) | undefined
    if (plan?.校验报告) {
      setReport(plan.校验报告)
    }
    await reloadPlans()
    await reload()
  }

  async function approvePlan(plan: Plan, action: string) {
    setErrorMessage('')
    try {
      const response = await request(`${LINKAGE_ENDPOINT}/plans/${plan.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      setNotice(payload.message ?? '审批动作已执行')
      if (!payload.ok) {
        setErrorMessage(payload.message ?? '审批动作未生效')
      }
      await reloadPlans()
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '审批动作未生效')
    }
  }

  const allChecked = rows.length > 0 && rows.every((row) => selected.includes(String(row['筛选器编号'])))
  const stats = [
    { label: '筛选器总数', value: total },
    { label: '待校验筛选器', value: rows.filter((row) => row.status === '待校验').length },
    { label: '已启用筛选器', value: rows.filter((row) => row.status === '已启用').length },
    { label: '联动方案', value: plans.length },
  ]

  return (
    <section className="page" data-module="filter">
      <header className="page-head">
        <div>
          <h2>全局筛选器管理</h2>
          <p className="page-desc">维护全局筛选器的类型定义、默认值与维度联动，支持多选后整组校验、整组发布联动方案。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出全局筛选器清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th className="check-col">
              <input
                type="checkbox"
                checked={allChecked}
                onChange={() => {
                  if (allChecked) {
                    setSelected([])
                  } else {
                    setSelected(rows.map((row) => String(row['筛选器编号'])))
                  }
                }}
              />
            </th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const code = String(row['筛选器编号'])
            return (
              <tr key={String(row.id)}>
                <td className="check-col">
                  <input type="checkbox" checked={selected.includes(code)} onChange={() => toggleSelect(code)} />
                </td>
                {columns.map((column) => <td key={column}>{row[column] || '—'}</td>)}
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无全局筛选器数据，可先登记筛选器</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="panel">
        <h3>批量联动编排</h3>
        <p className="page-desc">
          勾选多个筛选器与联动维度后一次提交：整组校验类型、默认值来源与联动维度，冲突按业务优先级裁决；
          任一绑定失败则整组不发布，不会只保存一部分。
        </p>
        <div className="orchestra-bar">
          <div>
            <span className="panel-label">已选筛选器（{selected.length}）</span>
            <div className="chip-row">
              {selected.length === 0 && <span className="muted">尚未勾选筛选器</span>}
              {selected.map((code) => (
                <button key={code} className="chip" type="button" onClick={() => toggleSelect(code)}>{code} ✕</button>
              ))}
            </div>
          </div>
          <div>
            <span className="panel-label">联动维度（{dims.length}）</span>
            <div className="chip-row">
              {dimOptions.map((name) => (
                <label key={name} className={`chip ${dims.includes(name) ? 'chip-on' : ''}`}>
                  <input type="checkbox" checked={dims.includes(name)} onChange={() => toggleDim(name)} />
                  {name}
                </label>
              ))}
            </div>
          </div>
          <div className="orchestra-actions">
            <button className="btn" type="button" onClick={() => void validateGroup()}>整组校验</button>
            <button className="btn primary" type="button" onClick={() => void publishGroup()}>整组发布</button>
          </div>
        </div>

        {report && (
          <div className="report">
            <h4>
              校验结果
              <span className={report.ok ? 'badge ok' : 'badge fail'}>{report.ok ? '整组通过' : '整组未通过'}</span>
            </h4>
            <table className="data-table">
              <thead>
                <tr><th>筛选器</th><th>结果</th><th>命中维度</th><th>业务优先级</th><th>说明</th></tr>
              </thead>
              <tbody>
                {report.筛选器校验.map((item) => (
                  <tr key={item.编号}>
                    <td>{item.编号}</td>
                    <td><span className={badgeClass(item.结果)}>{item.结果}</span></td>
                    <td>{item.命中维度.join('、') || '—'}</td>
                    <td>{item.业务优先级}<span className="muted">（{item.优先级依据}）</span></td>
                    <td>{item.说明.length > 0 ? item.说明.join('；') : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {report.裁决结果.length > 0 && (
              <>
                <h4>维度默认值裁决（按业务优先级只保留一个）</h4>
                <table className="data-table">
                  <thead>
                    <tr><th>联动维度</th><th>主筛选器</th><th>生效默认值</th><th>裁决依据</th><th>被覆盖</th></tr>
                  </thead>
                  <tbody>
                    {report.裁决结果.map((item) => (
                      <tr key={item.维度}>
                        <td>{item.维度}</td>
                        <td>{item.主筛选器}</td>
                        <td>{item.生效默认值 || '—'}</td>
                        <td>{item.裁决依据}</td>
                        <td>{item.被覆盖.length > 0 ? item.被覆盖.map((c) => `${c.筛选器}=${c.默认值}`).join('；') : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            {report.冲突列表.length > 0 && (
              <>
                <h4>冲突明细</h4>
                <ul className="conflict-list">
                  {report.冲突列表.map((item, index) => (
                    <li key={index}><span className="badge fail">冲突</span>{item.说明}</li>
                  ))}
                </ul>
              </>
            )}

            <h4>影响范围与刷新顺序</h4>
            <p className="muted">
              受影响图表：{report.影响图表.map((c) => c.图表编号).join('、') || '无'}；
              受影响看板：{report.影响看板.map((d) => d.面板编号).join('、') || '无'}
            </p>
            {report.刷新顺序.length > 0 && (
              <p className="muted">
                图表刷新顺序：{report.刷新顺序.map((item) => `${item.序号}. ${item.图表编号}`).join(' → ')}
              </p>
            )}
            {!report.ok && report.未生效范围.筛选器 && (
              <p className="error-text">
                未生效范围：筛选器 {report.未生效范围.筛选器.join('、')}
                {report.未生效范围.图表?.length ? `；图表 ${report.未生效范围.图表.join('、')}` : ''}
                {report.未生效范围.看板?.length ? `；看板 ${report.未生效范围.看板.join('、')}` : ''}
                （整组未发布，以上配置均保持原状）
              </p>
            )}
            {report.提示.map((tip, index) => <p key={index} className="muted">{tip}</p>)}
          </div>
        )}
      </section>

      <section className="panel">
        <h3>联动方案审批队列</h3>
        <p className="page-desc">每次整组发布在此生成一条完整方案；重复提交同一方案只保留一条有效记录。审批通过后，筛选器、图表与看板看到同一份联动结果。</p>
        <table className="data-table">
          <thead>
            <tr>
              <th>方案编号</th><th>状态</th><th>筛选器</th><th>联动维度</th><th>提交时间</th><th>审批意见</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {plans.map((plan) => (
              <Fragment key={plan.方案编号}>
                <tr>
                  <td>{plan.方案编号}</td>
                  <td><span className={badgeClass(plan.status)}>{plan.status}</span></td>
                  <td>{plan.筛选器.join('、')}</td>
                  <td>{plan.联动维度.join('、')}</td>
                  <td>{plan.提交时间}</td>
                  <td>{plan.审批意见 || '—'}</td>
                  <td className="row-actions">
                    <button className="link" type="button" onClick={() => setExpandedPlan(expandedPlan === plan.id ? null : plan.id)}>
                      {expandedPlan === plan.id ? '收起' : '详情'}
                    </button>
                    {plan.status === '待审批' && (
                      <>
                        <button className="link" type="button" onClick={() => void approvePlan(plan, '审批通过')}>审批通过</button>
                        <button className="link" type="button" onClick={() => void approvePlan(plan, '审批驳回')}>审批驳回</button>
                      </>
                    )}
                  </td>
                </tr>
                {expandedPlan === plan.id && (
                  <tr>
                    <td colSpan={7} className="plan-detail">
                      {plan.失败筛选器 && plan.失败筛选器.length > 0 && (
                        <p><span className="badge fail">失败筛选器</span>{plan.失败筛选器.join('、')}</p>
                      )}
                      {plan.冲突维度 && plan.冲突维度.length > 0 && (
                        <p><span className="badge warn">冲突维度</span>{plan.冲突维度.join('、')}</p>
                      )}
                      {plan.未生效范围 && (plan.未生效范围.筛选器?.length ?? 0) > 0 && (
                        <p>
                          <span className="badge fail">未生效范围</span>
                          筛选器 {plan.未生效范围.筛选器?.join('、') || '无'}
                          {plan.未生效范围.图表?.length ? `；图表 ${plan.未生效范围.图表.join('、')}` : ''}
                          {plan.未生效范围.看板?.length ? `；看板 ${plan.未生效范围.看板.join('、')}` : ''}
                        </p>
                      )}
                      {plan.刷新顺序 && plan.刷新顺序.length > 0 && (
                        <p><span className="badge">刷新顺序</span>{plan.刷新顺序.map((item) => `${item.序号}. ${item.图表编号}`).join(' → ')}</p>
                      )}
                      {plan.回写明细 && (
                        <p>
                          <span className="badge ok">已回写</span>
                          筛选器 {plan.回写明细.筛选器.join('、')}；图表 {plan.回写明细.图表.join('、')}；看板 {plan.回写明细.看板.join('、')}
                        </p>
                      )}
                      {!plan.回写明细 && plan.status === '待审批' && <p className="muted">待审批：通过后按刷新顺序一次性回写筛选器、图表与看板。</p>}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {plans.length === 0 && (
              <tr><td className="empty-state" colSpan={7}>暂无联动方案，整组发布后在此生成审批记录</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条全局筛选器记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
