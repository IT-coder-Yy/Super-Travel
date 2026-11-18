import { useEffect, useRef, useState } from 'react'
import { Check, LoaderCircle } from 'lucide-react'
import { ApiError, compareTripRoutes } from '../api'
import { formatCost, routeDetails, transitFare, transitSegment } from '../tripDetails'
import type { Activity, ConversationSummary, TripRouteOptions } from '../types'

type Props = {
  conversation: ConversationSummary
  day: number
  previous: Activity
  activity: Activity
  onSave: (optionId: string) => Promise<void>
  onConflict: (reason: unknown) => boolean
  onCancel: () => void
}

export function RouteComparison({ conversation, day, previous, activity, onSave, onConflict, onCancel }: Props) {
  const [result, setResult] = useState<TripRouteOptions | null>(null)
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const savingRef = useRef(false)
  const mounted = useRef(true)
  const resultsRef = useRef<HTMLFieldSetElement>(null)
  const errorRef = useRef<HTMLParagraphElement>(null)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current?.abort() } }, [])
  useEffect(() => {
    if (result?.options.length) resultsRef.current?.querySelector<HTMLInputElement>('input')?.focus()
    else if (result) resultsRef.current?.focus()
  }, [result])
  useEffect(() => { if (error) errorRef.current?.focus() }, [error])

  async function compare() {
    if (controller.current || savingRef.current) return
    const request = new AbortController()
    controller.current = request
    setLoading(true); setError(''); setResult(null); setSelected('')
    try {
      const next = await compareTripRoutes(conversation, activity.id, request.signal)
      if (mounted.current && !request.signal.aborted) {
        if (next.revision !== conversation.revision) throw new ApiError('会话已被更新。', 409, 'revision_conflict')
        setResult(next)
      }
    } catch (reason) {
      if (mounted.current && !request.signal.aborted && !onConflict(reason)) setError(reason instanceof Error && reason.name === 'TimeoutError' ? '查询超时，请重试；原草稿保持不变。' : reason instanceof Error ? reason.message : '查询未完成，请重试。')
    } finally { controller.current = null; if (mounted.current) setLoading(false) }
  }

  async function save() {
    if (!selected || controller.current || savingRef.current) return
    savingRef.current = true; setSaving(true); setError('')
    try { await onSave(selected) }
    catch (reason) {
      if (mounted.current && !onConflict(reason)) {
        if (reason instanceof ApiError && reason.code === 'route_option_expired') { setResult(null); setSelected('') }
        setError(reason instanceof Error ? reason.message : '方案未保存，请重试。')
      }
    } finally { savingRef.current = false; if (mounted.current) setSaving(false) }
  }

  const taxi = result?.options[0]?.route.taxiEstimates
  return <form className="activity-action route-comparison" onSubmit={event => { event.preventDefault(); void save() }}>
    <div className="route-comparison-scroll">
    <p className="route-comparison-leg">第 {day} 天 · {previous.title} → {activity.title}</p>
    <p className="muted-note">只更换这段公交接驳。查询一次，比较同一供应商本次返回的最多 3 个可区分方案，保留返回顺序。</p>
    <p className="muted-note">每次查询最多 12 秒，不自动重试；结果 5 分钟内可选。保存到草稿后，再由你应用修改。</p>
    <button type="button" className="secondary-button" autoFocus disabled={loading || saving} onClick={() => void compare()}>{loading && <LoaderCircle size={16} className="spin" />}{loading ? '正在查询…' : result ? '重新查询方案' : '查询公交方案'}</button>
    {loading && <p className="muted-note" role="status">正在查询本段公交方案，可以返回编辑取消等待。</p>}
    {result && <fieldset ref={resultsRef} className="activity-action-fields route-options" disabled={saving} tabIndex={-1}>
      <legend>{result.options.length ? `本次可选 ${result.options.length} 个方案` : '本次没有可选方案'}</legend>
      {!result.options.length && <p role="status">{result.reason || '供应商没有返回可用的公交方案，请稍后重试。'}原草稿保持不变。</p>}
      {result.options.map(({ option_id, route }, index) => {
        const leg = routeDetails(previous, { ...activity, incomingRoute: route }, 'transit')!
        return <div className={`route-option ${selected === option_id ? 'route-option--selected' : ''}`} key={option_id}>
          <label className="route-option-choice"><input type="radio" name="route-option" value={option_id} checked={selected === option_id} onChange={() => setSelected(option_id)} />
            <span><strong>方案 {index + 1}</strong><span>{leg.text}</span></span>
          </label>
          <div className="route-option-body">
            <p>{route.transit?.transferCount == null ? '换乘次数待确认' : `换乘 ${route.transit.transferCount} 次（按乘车段计算）`}</p>
            <p>{transitFare(route.transit ?? null)}</p>
            <details><summary>线路与上下车站</summary>{route.transit?.segments.length ? <ol>{route.transit.segments.map((segment, i) => <li key={i}>{transitSegment(segment)}</li>)}</ol> : <p>公交分段详情待确认</p>}</details>
            <p className="muted-note">{leg.detail}</p>
            {leg.conflict && <p className="route-option-warning">{leg.conflict} 可先保存到草稿，再调整活动时间；冲突解决前无法应用。</p>}
          </div>
        </div>
      })}
      {result.options.length > 0 && <div className="route-taxi-reference"><strong>同次查询的出租车参考价</strong>
        {taxi?.length ? <ul>{taxi.map((item, index) => <li key={index}>{item.label || '时段未注明'}：约 {formatCost(item.amountFen)}（仅供参考，以实际结果为准）{item.note && <p>{item.note}</p>}</li>)}</ul> : <p>本次暂无可用出租车估算。</p>}
        <p className="muted-note">出租车是另一种出行选择，不属于公交方案。参考价不与公交票价相加，两者均不计入活动费用小计。</p>
      </div>}
    </fieldset>}
    {error && <p ref={errorRef} tabIndex={-1} className="inline-error" role="alert">{error}</p>}
    </div>
    <div className="dialog-actions"><button type="button" className="text-button" disabled={saving} onClick={onCancel}>返回编辑</button><button type="submit" className="primary-button" disabled={!selected || loading || saving}>{saving ? <LoaderCircle size={17} className="spin" /> : <Check size={17} />}{saving ? '正在保存…' : '保存方案到草稿'}</button></div>
  </form>
}
