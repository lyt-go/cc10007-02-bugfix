import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/sequence'
const columns = [
  "模式编号",
  "用户分群",
  "事件序列",
  "最大步长",
  "最小支持度",
  "时间窗口天",
  "序列频次",
  "转化率"
]
// 状态机严格按生命周期流转，前端按钮按当前状态收敛，只给合法入口。
const actionFlow: Record<string, { action: string; next: string }[]> = {
  "草稿": [{ action: "发起挖掘", next: "挖掘中" }],
  "挖掘中": [{ action: "确认模式", next: "已确认" }],
  "已确认": [{ action: "归档模式", next: "已归档" }],
  "已归档": [{ action: "恢复模式", next: "挖掘中" }],
}

type Row = Record<string, string | number | null>
type InputRow = Record<string, string | number | null>
type AuditRow = Record<string, string | number | null>

export default function SequenceView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [okMessage, setOkMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [inputs, setInputs] = useState<InputRow[]>([])
  const [auditOf, setAuditOf] = useState<{ code: string; items: AuditRow[] } | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('序列模式列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '行为序列列表读取失败')
    }
  }, [filters])

  const reloadInputs = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/cluster-inputs`)
      if (response.ok) {
        const payload = await response.json()
        setInputs(payload.items ?? [])
      }
    } catch {
      // 明细区加载失败不阻塞主列表，页脚给出提示。
      setErrorMessage('聚类输入分群明细数据集读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadInputs()
  }, [reload, reloadInputs])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setOkMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      // 后端拦截越级推进时 HTTP 仍为 200、ok=false，必须把冲突原因展示出来。
      if (!response.ok || payload.ok === false) {
        setErrorMessage(payload.message || '行为序列动作未生效，请稍后重试')
        return
      }
      setOkMessage(payload.message || '动作已生效')
      await reload()
      await reloadInputs()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '行为序列操作失败')
    }
  }

  async function openAudit(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/audit`)
      if (!response.ok) {
        throw new Error('审计留痕读取失败')
      }
      const payload = await response.json()
      setAuditOf({ code: String(row['模式编号']), items: payload.items ?? [] })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '审计留痕读取失败')
    }
  }

  const stats = useMemo(() => ([
    { label: '序列模式总数', value: rows.length },
    { label: '已确认模式', value: rows.filter((r) => r.status === '已确认').length },
    { label: '挖掘中模式', value: rows.filter((r) => r.status === '挖掘中').length },
  ]), [rows])

  return (
    <section className="page" data-module="sequence">
      <header className="page-head">
        <div>
          <h2>行为序列管理</h2>
          <p className="page-desc">挖掘用户行为序列模式，识别高频行为路径和转化瓶颈。状态严格按 草稿 → 挖掘中 → 已确认 → 已归档 流转。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('序列模式登记入口尚未接入审批流')}>
            登记序列模式
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出行为序列清单
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
            <th>状态</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>确认版本</th>
            <th>可执行动作</th>
            <th>留痕</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.status ?? '—')}</td>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row['已确认版本'] ? `v${String(row['已确认版本'])}` : '未确认'}</td>
              <td className="row-actions">
                {(actionFlow[String(row.status)] ?? []).map(({ action }) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
              <td>
                <button className="link" type="button" onClick={() => void openAudit(row)}>审计留痕</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无行为序列数据，可先登记序列模式</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">聚类输入分群明细数据集（仅已确认序列版本，可复核）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>输入编号</th>
            <th>模式编号</th>
            <th>序列版本</th>
            <th>分群特征（事件记录重读重算）</th>
            <th>事件序列快照</th>
            <th>状态</th>
            <th>生成时间</th>
          </tr>
        </thead>
        <tbody>
          {inputs.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row['输入编号'] ?? '—')}</td>
              <td>{String(row['模式编号'] ?? '—')}</td>
              <td>v{String(row['序列版本号'] ?? '—')}</td>
              <td>{String(row['分群特征'] ?? '—')}</td>
              <td>{String(row['事件序列快照'] ?? '—')}</td>
              <td>{String(row['状态'] ?? '—')}</td>
              <td>{String(row['生成时间'] ?? '—')}</td>
            </tr>
          ))}
          {inputs.length === 0 && (
            <tr><td className="empty-state" colSpan={7}>暂无已确认序列产生的聚类输入</td></tr>
          )}
        </tbody>
      </table>

      {auditOf && (
        <section className="audit-panel">
          <h3 className="section-title">{auditOf.code} 变更留痕（归档与恢复全程可复查）</h3>
          <table className="data-table">
            <thead>
              <tr><th>动作</th><th>变更前状态</th><th>变更后状态</th><th>版本号</th><th>结果</th><th>原因</th><th>操作时间</th></tr>
            </thead>
            <tbody>
              {auditOf.items.map((item) => (
                <tr key={String(item.id)}>
                  <td>{String(item['动作'])}</td>
                  <td>{String(item['变更前状态'] ?? '—')}</td>
                  <td>{String(item['变更后状态'])}</td>
                  <td>v{String(item['版本号'])}</td>
                  <td>{String(item['结果'])}</td>
                  <td>{String(item['原因'] || '—')}</td>
                  <td>{String(item['操作时间'])}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="btn ghost" type="button" onClick={() => setAuditOf(null)}>关闭留痕</button>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条行为序列记录</span>
        {okMessage && <span className="ok-text">{okMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
