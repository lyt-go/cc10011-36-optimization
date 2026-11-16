import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/drilldown'
const columns = [
  "路径编号",
  "起始维度",
  "下钻层级序列",
  "每层聚合粒度",
  "默认度量",
  "路径深度",
  "缓存秒数",
  "数据新鲜度"
]
const actions = [
  "提交路径评审",
  "启用路径",
  "归档路径"
]

type Row = Record<string, string | number | null>
type Layer = {
  层级: number
  原始维度名: string
  标准维度名: string
  标准值: string | null
  命中规则编号: string
  规则版本: number
  映射版本: number
  转换状态: string
  冲突规则: string[]
  上一可用标准值: string | null
}
type LayeredResult = {
  路径编号: string
  映射版本: number
  缓存命中: boolean
  数据新鲜度: string
  依赖原始维度: string[]
  stale: boolean
  冲突规则: string[]
  层级结果: Layer[]
}

export default function DrilldownView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [layered, setLayered] = useState<LayeredResult | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('下钻路径列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻列表读取失败')
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
        throw new Error('多维下钻动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻操作失败')
    }
  }

  async function openLayered(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/layered`)
      if (!response.ok) throw new Error('分层下钻结果读取失败')
      setLayered(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分层下钻结果读取失败')
    }
  }

  return (
    <section className="page" data-module="drilldown">
      <header className="page-head">
        <div>
          <h2>多维下钻管理</h2>
          <p className="page-desc">下钻分层消费标准维度映射结果，每条结果钉住映射版本；规则变更只失效包含该维度的路径，稳定命中直接复用。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('下钻路径登记入口尚未接入审批流')}>
            登记下钻路径
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出多维下钻清单
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
                <button className="link" type="button" onClick={() => void openLayered(row)}>查看分层结果</button>
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无多维下钻数据，可先登记下钻路径</td>
            </tr>
          )}
        </tbody>
      </table>

      {layered && (
        <div className="result-panel">
          <div className="result-head">
            <h3 className="section-title">分层下钻结果 · {layered.路径编号}</h3>
            <button className="btn ghost" type="button" onClick={() => setLayered(null)}>关闭</button>
          </div>
          <p className="empty-tip">
            数据新鲜度：{layered.数据新鲜度}　｜　缓存命中：{layered.缓存命中 ? '是（稳定命中复用）' : '否（按当前映射版本重算）'}
            {layered.stale && <span className="cell-warn">　｜　存在转换异常，已沿用上一可用结果，冲突规则：{layered.冲突规则.join('、')}</span>}
          </p>
          <table className="data-table compact">
            <thead>
              <tr>
                <th>层级</th><th>原始维度名</th><th>标准维度名</th><th>命中规则</th>
                <th>规则版本</th><th>映射版本</th><th>转换状态</th><th>冲突规则</th><th>上一可用值</th>
              </tr>
            </thead>
            <tbody>
              {layered.层级结果.map((layer) => (
                <tr key={layer.层级}>
                  <td>{layer.层级}</td>
                  <td>{layer.原始维度名}</td>
                  <td>{layer.标准维度名}</td>
                  <td>{layer.命中规则编号}</td>
                  <td>v{layer.规则版本}</td>
                  <td>v{layer.映射版本}</td>
                  <td className={layer.转换状态 === 'convert_error' ? 'cell-warn' : ''}>{layer.转换状态}</td>
                  <td>{layer.冲突规则.join('、')}</td>
                  <td>{layer.上一可用标准值 ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条多维下钻记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
