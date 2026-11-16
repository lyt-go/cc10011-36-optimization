import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/aggregate'
const columns = [
  "规则编号",
  "源数据粒度",
  "目标粒度",
  "聚合函数集",
  "数据偏移分钟",
  "补全策略",
  "空值处理",
  "输出数据集"
]
const actions = [
  "提交规则评审",
  "生效规则",
  "废弃规则"
]

type Row = Record<string, string | number | null>
type GrainLayer = {
  目标粒度: string
  映射版本: number
  缓存命中: boolean
  数据新鲜度: string
  维度映射: Array<{
    原始维度名: string
    标准维度名: string
    标准值: string | null
    命中规则编号: string
    规则版本: number
    映射版本: number
    转换状态: string
    冲突规则: string[]
  }>
}
type LayeredAggregate = {
  规则编号: string
  输出数据集: string | null
  映射版本: number
  整体缓存命中: boolean
  stale: boolean
  冲突规则: string[]
  分层聚合结果: GrainLayer[]
}

export default function AggregateView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [layered, setLayered] = useState<LayeredAggregate | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('聚合规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合配置列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      if (!response.ok) {
        throw new Error('聚合配置动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合配置操作失败')
    }
  }

  async function openLayered(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/layered`)
      if (!response.ok) throw new Error('分层聚合结果读取失败')
      setLayered(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分层聚合结果读取失败')
    }
  }

  return (
    <section className="page" data-module="aggregate">
      <header className="page-head">
        <div>
          <h2>聚合配置管理</h2>
          <p className="page-desc">分层聚合随映射缓存联动失效：规则变更只失效包含对应原始维度的粒度，且一份聚合只读取当前生效的单一映射版本。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('聚合规则登记入口尚未接入审批流')}>
            登记聚合规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出聚合配置清单
          </button>
        </div>
      </header>

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
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openLayered(row)}>查看分层聚合</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无聚合配置数据，可先登记聚合规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {layered && (
        <div className="result-panel">
          <div className="result-head">
            <h3 className="section-title">分层聚合结果 · {layered.规则编号}（{layered.输出数据集 ?? '未指定数据集'}）</h3>
            <button className="btn ghost" type="button" onClick={() => setLayered(null)}>关闭</button>
          </div>
          <p className="empty-tip">
            统一映射版本：v{layered.映射版本}　｜　整体缓存命中：{layered.整体缓存命中 ? '是' : '否（含重算粒度）'}
            {layered.stale && <span className="cell-warn">　｜　存在转换异常，已沿用上一可用结果，冲突规则：{layered.冲突规则.join('、')}</span>}
          </p>
          {layered.分层聚合结果.map((grain) => (
            <div key={grain.目标粒度} className="grain-block">
              <h4 className="grain-title">
                粒度：{grain.目标粒度}　｜　{grain.数据新鲜度}　｜　缓存{grain.缓存命中 ? '命中' : '重算'}
              </h4>
              <table className="data-table compact">
                <thead>
                  <tr>
                    <th>原始维度名</th><th>标准维度名</th><th>命中规则</th>
                    <th>规则版本</th><th>映射版本</th><th>转换状态</th><th>冲突规则</th>
                  </tr>
                </thead>
                <tbody>
                  {grain.维度映射.map((mapped) => (
                    <tr key={mapped.原始维度名}>
                      <td>{mapped.原始维度名}</td>
                      <td>{mapped.标准维度名}</td>
                      <td>{mapped.命中规则编号}</td>
                      <td>v{mapped.规则版本}</td>
                      <td>v{mapped.映射版本}</td>
                      <td className={mapped.转换状态 === 'convert_error' ? 'cell-warn' : ''}>{mapped.转换状态}</td>
                      <td>{mapped.冲突规则.join('、')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条聚合配置记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
