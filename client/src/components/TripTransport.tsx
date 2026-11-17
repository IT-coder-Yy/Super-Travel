import { useEffect, useRef, useState } from 'react'
import { Check, LoaderCircle } from 'lucide-react'
import { transportModes } from '../tripDetails'
import type { TripDay, TripRoute } from '../types'

type Props = {
  days: TripDay[]
  currentMode: TripRoute['mode']
  onSave: (mode: TripRoute['mode']) => Promise<void>
  onCancel: () => void
}

export function TripTransport({ days, currentMode, onSave, onCancel }: Props) {
  const [mode, setMode] = useState(currentMode)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const mounted = useRef(true)
  const changed = mode !== currentMode
  const legs = days.flatMap(day => day.activities.slice(1).map((activity, index) => ({ day: day.day, previous: day.activities[index]!, activity })))
  const invalidated = changed ? legs.filter(({ activity }) => activity.incomingRoute && activity.incomingRoute.mode !== mode) : []

  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  async function save() {
    if (!changed || savingRef.current) return
    savingRef.current = true; setSaving(true); setError('')
    try { await onSave(mode) }
    catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : '交通方式未保存，请重试。') }
    finally { savingRef.current = false; if (mounted.current) setSaving(false) }
  }

  return <form className="activity-action transport-action" onSubmit={event => { event.preventDefault(); void save() }}>
    <fieldset className="activity-action-fields" disabled={saving} aria-busy={saving}>
      <div className="field-label"><label htmlFor="trip-transport-mode">整程接驳方式</label><select id="trip-transport-mode" autoFocus value={mode} onChange={event => { setMode(event.target.value as TripRoute['mode']); setError('') }} aria-describedby="transport-scope">
        {Object.entries(transportModes).map(([value, label]) => <option key={value} value={value}>{label}{value === currentMode ? '（当前）' : ''}</option>)}
      </select></div>
      <p className="muted-note" id="transport-scope">统一用于每天相邻活动之间的接驳，不包含往返目的地或跨日交通。活动顺序、时间、备注和费用保持不变。</p>
      <div className="transport-impact" aria-live="polite">
        <strong>{changed ? `将改为${transportModes[mode]}，影响 ${legs.length} 段接驳` : '请选择另一种交通方式'}</strong>
        {changed && <>
          <p>{invalidated.length ? `以下 ${invalidated.length} 条旧路线及其参考报价将从草稿清除：` : '没有需要清除的旧路线记录。'}</p>
          {invalidated.length > 0 && <ul>{invalidated.map(({ day, previous, activity }) => <li key={activity.id}>第 {day} 天 · {previous.title} → {activity.title}</li>)}</ul>}
        </>}
      </div>
      <p className="muted-note">保存仅更新草稿，不查询路线。应用修改时，单段最多查询 12 秒，整次最多尝试查询 12 段、30 秒；失败也计数，不自动重试。未核验的耗时会显示为未知。</p>
      <p className="muted-note">不会自动调整活动时间。若新路线与安排冲突，应用会停止，草稿保留供你修改；应用前仍可放弃草稿。</p>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="dialog-actions"><button type="button" className="text-button" onClick={onCancel}>返回编辑</button><button type="submit" className="primary-button" disabled={!changed}>{saving ? <LoaderCircle size={17} className="spin" /> : <Check size={17} />}{saving ? '正在保存…' : '保存方式到草稿'}</button></div>
    </fieldset>
  </form>
}
