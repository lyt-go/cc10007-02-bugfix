import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/cluster'
const columns = [
  "任务编号",
  "聚类算法",
  "特征维度",
  "目标聚类数",
  "距离度量",
  "迭代次数上限",
  "聚类标签",
  "轮廓系数"
]
// 聚类任务同样严格按生命周期收敛按钮，越级动作由后端二次拦截。
const actionFlow: Record<string, { action: string }[]> = {
  "草稿": [{ action: "启动聚类" }],
  "聚类计算中": [{ action: "确认完成" }],
  "已完成": [{ action: "归档结果" }],
  "已归档": [{ action: "恢复结果" }],
}

type Row = Record<string, string | number | null>
type PendingRow = Record<string, string | number | null>

export default function ClusterView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [okMessage, setOkMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [pending, setPending] = useState<PendingRow[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('聚类任务列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚类分群列表读取失败')
    }
  }, [filters])

  const reloadPending = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/pending?only_open=true`)
      if (response.ok) {
        const payload = await response.json()
        setPending(payload.items ?? [])
      }
    } catch {
      setErrorMessage('聚类任务待处理清单读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadPending()
  }, [reload, reloadPending])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setOkMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      // 后端返回 ok=false 时把冲突原因直接展示，任务保持待执行。
      if (!response.ok || payload.ok === false) {
        setErrorMessage(payload.message || '聚类分群动作未生效，请稍后重试')
        await reloadPending()
        return
      }
      setOkMessage(payload.message || '动作已生效')
      await reload()
      await reloadPending()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚类分群操作失败')
    }
  }

  async function syncSequenceStatus() {
    setErrorMessage('')
    setOkMessage('')
    try {
      const response = await request(`${ENDPOINT}/sync-sequence-status`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        setErrorMessage(payload.message || '序列状态回写失败')
        return
      }
      setOkMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '序列状态回写失败')
    }
  }

  const stats = useMemo(() => ([
    { label: '聚类任务总数', value: rows.length },
    { label: '已完成任务', value: rows.filter((r) => r.status === '已完成').length },
    { label: '计算中任务', value: rows.filter((r) => r.status === '聚类计算中').length },
  ]), [rows])

  return (
    <section className="page" data-module="cluster">
      <header className="page-head">
        <div>
          <h2>聚类分群管理</h2>
          <p className="page-desc">管理无监督聚类分群任务。待执行任务只引用已确认序列版本，序列状态实时回写本列表。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('聚类任务登记入口尚未接入审批流')}>
            登记聚类任务
          </button>
          <button className="btn" type="button" onClick={() => void syncSequenceStatus()}>
            回写序列状态
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出聚类分群清单
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
            <th>关联序列</th>
            <th>引用版本</th>
            <th>聚类输入</th>
            <th>序列状态回写</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.status ?? '—')}</td>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{String(row['关联序列编号'] ?? '未关联')}</td>
              <td>{row['关联序列版本'] ? `v${String(row['关联序列版本'])}` : '—'}</td>
              <td>{String(row['关联输入编号'] ?? '—')}</td>
              <td>{String(row['序列状态'] ?? '未关联')}</td>
              <td className="row-actions">
                {(actionFlow[String(row.status)] ?? []).map(({ action }) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 5}>暂无聚类分群数据，可先登记聚类任务</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">聚类任务待处理清单（消费冲突及原因，任务保持待执行）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>任务编号</th>
            <th>关联序列</th>
            <th>引用版本</th>
            <th>冲突原因</th>
            <th>处理状态</th>
            <th>登记时间</th>
          </tr>
        </thead>
        <tbody>
          {pending.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row['任务编号'])}</td>
              <td>{String(row['关联序列编号'] ?? '—')}</td>
              <td>{row['关联序列版本'] ? `v${String(row['关联序列版本'])}` : '—'}</td>
              <td>{String(row['冲突原因'])}</td>
              <td>{String(row['处理状态'])}</td>
              <td>{String(row['登记时间'])}</td>
            </tr>
          ))}
          {pending.length === 0 && (
            <tr><td className="empty-state" colSpan={6}>暂无待处理冲突</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>
          共 {total} 条聚类分群记录，收尾后可
          <Link className="inline-link" to="/sequence">返回行为序列清单查关联结果</Link>
        </span>
        {okMessage && <span className="ok-text">{okMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
