import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dimensionmap'
const columns = [
  "规则编号",
  "原始维度名",
  "标准维度名",
  "映射类型",
  "值转换逻辑",
  "默认取值",
  "映射优先级",
  "规则版本",
  "最近更新时间"
]
const actions = [
  "提交规则评审",
  "生效规则",
  "废弃规则"
]

type Row = Record<string, string | number | null>
type ChainEvent = {
  时间: string
  变更类型: string
  规则编号: string
  原始维度名: string
  规则版本: number
  旧映射版本: number
  新映射版本: number
  定向失效结果: Record<string, string[]>
}
type ChainOverview = {
  当前映射版本: number
  命中缓存: Array<Record<string, string | number>>
  下游缓存: Record<string, { 缓存条数: number; 明细: Array<Record<string, unknown>> }>
  数据集版本: number | null
  失效链事件: ChainEvent[]
}
type Dataset = {
  数据集编号?: string
  映射版本?: number
  生成时间?: string
  记录数?: number
  重复提交?: boolean
  items?: Array<Record<string, string | number | string[]>>
}

export default function DimensionmapView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [rawFields, setRawFields] = useState<Row[]>([])
  const [dataset, setDataset] = useState<Dataset | null>(null)
  const [chain, setChain] = useState<ChainOverview | null>(null)
  const filterFields = useMemo(() => ["规则编号", "原始维度名", "标准维度名"], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('映射规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度映射列表读取失败')
    }
  }, [filters])

  const reloadAux = useCallback(async () => {
    try {
      const [fieldResp, datasetResp, chainResp] = await Promise.all([
        request(`${ENDPOINT}/raw-fields`),
        request(`${ENDPOINT}/dataset/current`),
        request(`${ENDPOINT}/chain`),
      ])
      if (fieldResp.ok) setRawFields((await fieldResp.json()).items ?? [])
      if (datasetResp.ok) {
        const data = await datasetResp.json()
        setDataset(data.generated ? data : null)
      }
      if (chainResp.ok) setChain(await chainResp.json())
    } catch {
      // 辅助面板失败不阻塞主表
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadAux()
  }, [reloadAux])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '维度映射动作未生效，请稍后重试')
      }
      const event = payload.entry?._invalidation_event as ChainEvent | undefined
      setInfoMessage(event
        ? `${payload.message}；映射版本 v${event.新映射版本}，已定向失效：${Object.entries(event.定向失效结果)
            .map(([name, keys]) => `${name} ${keys.length} 条`).join('、')}`
        : payload.message)
      await reload()
      await reloadAux()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度映射操作失败')
    }
  }

  async function changePriority(row: Row) {
    const next = window.prompt(`调整「${row.规则编号}」的映射优先级（数值越小越优先）`, String(row.映射优先级 ?? ''))
    if (next === null) return
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/priority`, {
        method: 'POST',
        body: JSON.stringify({ values: { 映射优先级: next.trim() } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '优先级调整失败')
      setInfoMessage(payload.message)
      await reload()
      await reloadAux()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '优先级调整失败')
    }
  }

  async function generateDataset() {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/dataset/generate`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '数据集生成失败')
      setInfoMessage(payload.message)
      await reloadAux()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据集生成失败')
    }
  }

  return (
    <section className="page" data-module="dimensionmap">
      <header className="page-head">
        <div>
          <h2>维度映射管理</h2>
          <p className="page-desc">按优先级把原始维度映射为标准维度；规则版本与命中快照可追溯，变更只定向失效相关维度的下钻与聚合缓存。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void generateDataset()}>
            生成标准维度映射数据集
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出维度映射清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">当前映射版本</span>
          <strong className="stat-value">v{chain?.当前映射版本 ?? 1}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">命中缓存条数</span>
          <strong className="stat-value">{chain?.命中缓存.length ?? 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">数据集记录数</span>
          <strong className="stat-value">{dataset?.记录数 ?? '未生成'}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">失效链事件</span>
          <strong className="stat-value">{chain?.失效链事件.length ?? 0}</strong>
        </article>
      </div>

      {infoMessage && <p className="info-banner">{infoMessage}</p>}

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
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void changePriority(row)}>调整优先级</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无维度映射数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">共享原始维度字段记录（职责内只读）</h3>
      <table className="data-table compact">
        <thead>
          <tr><th>字段编号</th><th>原始维度名</th><th>原始值</th><th>字段类型</th><th>来源表</th></tr>
        </thead>
        <tbody>
          {rawFields.map((field) => (
            <tr key={String(field.id)}>
              <td>{field.字段编号}</td>
              <td>{field.原始维度名}</td>
              <td>{field.原始值}</td>
              <td>{field.字段类型}</td>
              <td>{field.来源表}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 className="section-title">标准维度映射数据集{dataset ? `（映射版本 v${dataset.映射版本}${dataset.重复提交 ? '，重复提交复用' : ''}）` : ''}</h3>
      {dataset ? (
        <table className="data-table compact">
          <thead>
            <tr>
              <th>原始维度名</th><th>原始值</th><th>标准维度名</th><th>标准值</th>
              <th>命中规则</th><th>规则版本</th><th>映射版本</th><th>转换状态</th><th>冲突规则</th>
            </tr>
          </thead>
          <tbody>
            {(dataset.items ?? []).map((item, index) => (
              <tr key={index}>
                <td>{String(item.原始维度名)}</td>
                <td>{String(item.原始值)}</td>
                <td>{String(item.标准维度名)}</td>
                <td>{String(item.标准值)}</td>
                <td>{String(item.命中规则编号 || '未命中')}</td>
                <td>v{String(item.规则版本)}</td>
                <td>v{String(item.映射版本)}</td>
                <td className={item.转换状态 === 'convert_error' ? 'cell-warn' : ''}>{String(item.转换状态)}</td>
                <td>{Array.isArray(item.冲突规则) ? item.冲突规则.join('、') : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="empty-tip">尚未生成数据集，点击右上角「生成标准维度映射数据集」；同版本重复提交不会生成第二份。</p>
      )}

      <h3 className="section-title">映射命中 → 缓存失效 → 聚合读取链路</h3>
      {chain && (
        <>
          <table className="data-table compact">
            <thead>
              <tr><th>时间</th><th>变更类型</th><th>规则编号</th><th>原始维度名</th><th>规则版本</th><th>映射版本</th><th>定向失效结果</th></tr>
            </thead>
            <tbody>
              {chain.失效链事件.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>暂无规则变更事件</td></tr>
              )}
              {chain.失效链事件.map((event, index) => (
                <tr key={index}>
                  <td>{event.时间}</td>
                  <td>{event.变更类型}</td>
                  <td>{event.规则编号}</td>
                  <td>{event.原始维度名}</td>
                  <td>v{event.规则版本}</td>
                  <td>v{event.旧映射版本} → v{event.新映射版本}</td>
                  <td>
                    {Object.entries(event.定向失效结果)
                      .map(([name, keys]) => `${name}：${keys.length ? keys.join('、') : '无'}`)
                      .join(' ｜ ')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="empty-tip">
            下游缓存：{Object.entries(chain.下游缓存)
              .map(([name, info]) => `${name} ${info.缓存条数} 条`).join('、')}
            ；下游只读取当前映射版本 v{chain.当前映射版本}，不混版本聚合。
          </p>
        </>
      )}

      <footer className="page-foot">
        <span>共 {total} 条维度映射记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
