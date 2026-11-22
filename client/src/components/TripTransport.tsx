import { useEffect, useRef, useState } from 'react'
import { Check, LoaderCircle } from 'lucide-react'
import { legTransportMode, transportModes } from '../tripDetails'
import type { Activity, TripDay, TripRoute } from '../types'

type Mode = TripRoute['mode']
export type TransportLeg = { day: number; previous: Activity; activity: Activity }
type Props = {
  days: TripDay[]
  currentMode: Mode
  leg?: TransportLeg
  blocked: boolean
  onSave: (mode: Mode | null) => Promise<void>
  onConflict: (reason: unknown) => boolean
  onCancel: () => void
}

const legName = ({ day, previous, activity }: TransportLeg) => `第 ${day} 天 · ${previous.title} → ${activity.title}`

export function TripTransport({ days, currentMode, leg, blocked, onSave, onConflict, onCancel }: Props) {
  const initial = leg ? leg.activity.incomingTransportOverride?.mode ?? 'follow' : currentMode
  const [choice, setChoice] = useState<Mode | 'follow'>(initial)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const mounted = useRef(true)
  const changed = choice !== initial
  const mode = choice === 'follow' ? currentMode : choice
  const legs = days.flatMap(day => day.activities.slice(1).map((activity, index) => ({ day: day.day, previous: day.activities[index]!, activity })))
  const affected = leg ? [leg] : legs.filter(item => !item.activity.incomingTransportOverride)
  const retained = legs.filter(item => item.activity.incomingTransportOverride && item.activity.id !== leg?.activity.id)
  const changesMode = leg ? mode !== legTransportMode(leg.activity, currentMode) : mode !== currentMode
  const cleared = affected.filter(({ activity }) => activity.incomingRoute && (leg ? changesMode : activity.incomingRoute.mode !== mode))

  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  async function save() {
    if (!changed || blocked || savingRef.current) return
    savingRef.current = true
    setSaving(true)
    setError('')
    try { await onSave(choice === 'follow' ? null : choice) }
    catch (reason) { if (mounted.current && !onConflict(reason)) setError(reason instanceof Error ? reason.message : '交通方式未保存，请重试。') }
    finally { savingRef.current = false; if (mounted.current) setSaving(false) }
  }

  return <form className="activity-action transport-action" onSubmit={event => { event.preventDefault(); void save() }}>
    <fieldset className="activity-action-fields" disabled={saving || blocked} aria-busy={saving}>
      {leg && <p className="route-comparison-leg" id="transport-leg">{legName(leg)}</p>}
      <div className="field-label">
        <label htmlFor="trip-transport-mode">{leg ? '本段接驳方式' : '整程接驳方式'}</label>
        <select id="trip-transport-mode" autoFocus value={choice} onChange={event => { setChoice(event.target.value as typeof choice); setError('') }} aria-describedby={leg ? 'transport-leg transport-scope' : 'transport-scope'}>
          {leg && <option value="follow">跟随整程（{transportModes[currentMode]}）</option>}
          {Object.entries(transportModes).map(([value, label]) => <option key={value} value={value}>{label}{value === initial ? '（当前）' : ''}</option>)}
        </select>
      </div>
      <p className="muted-note" id="transport-scope">{leg ? '选择具体方式会固定这一段。选择“跟随整程”后，它会随整程默认方式一起改变。' : '整程默认适用于各天跟随整程的路段。'}</p>
      {changed && <div className="transport-impact" aria-live="polite">
        <strong>{leg ? (choice === 'follow' ? `本段将跟随整程（${transportModes[mode]}）` : `本段将指定${transportModes[mode]}`) : `将改为${transportModes[mode]}，影响 ${affected.length} 段接驳`}</strong>
        {leg && !changesMode && <p>实际方式相同，保留本段已有路线{leg.activity.incomingRoute?.selection === 'user' ? '和已选公交方案' : ''}。</p>}
        {cleared.length > 0 && <><p>保存后会清除以下旧路线及其参考报价</p>
          <ul>{cleared.map(item => <li key={item.activity.id}>{legName(item)}{item.activity.incomingRoute?.selection === 'user' && '（会清除已选公交方案）'}</li>)}</ul></>}
        {retained.length > 0 && <details><summary>保留 {retained.length} 段已指定的方式</summary>
          <ul>{retained.map(item => <li key={item.activity.id}>{legName(item)} · {transportModes[legTransportMode(item.activity, currentMode)]}</li>)}</ul></details>}
      </div>}
      <p className="muted-note">保存到草稿后，点击“应用修改”才核查路线。每段最多 12 秒，整次最多查询 12 段、共 30 秒。耗时未知时会标出原因，时间不足时可回到草稿调整。</p>
      {error && <p className="inline-error" role="alert">{error}</p>}
      {!blocked && <div className="dialog-actions"><button type="button" className="text-button" onClick={onCancel}>返回编辑</button><button type="submit" className="primary-button" disabled={!changed}>{saving ? <LoaderCircle size={17} className="spin" /> : <Check size={17} />}{saving ? '正在保存…' : '保存方式到草稿'}</button></div>}
    </fieldset>
  </form>
}
