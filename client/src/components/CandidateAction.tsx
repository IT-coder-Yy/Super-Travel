import { useEffect, useRef, useState } from 'react'
import { CalendarPlus, LoaderCircle, Search, Trash2 } from 'lucide-react'
import { findCandidateSlots } from '../api'
import type { Candidate, CandidateSlot, ConversationSummary, TripDay } from '../types'

type Props = {
  kind: 'insert' | 'delete'
  conversation: ConversationSummary
  candidate: Candidate
  days: TripDay[]
  selectedDay: number
  onSave: (optionId?: string, time?: string) => Promise<void>
  onCancel: () => void
}
const toMinutes = (time: string) => Number(time.slice(0, 2)) * 60 + Number(time.slice(3))
const toTime = (minutes: number) => String(Math.floor(minutes / 60)).padStart(2, '0') + ':' + String(minutes % 60).padStart(2, '0')
const defaultTime = (slot: CandidateSlot) => toTime(Math.max(Math.ceil(toMinutes(slot.earliest_start) / 15) * 15, Math.min(9 * 60, Math.floor(toMinutes(slot.latest_start) / 15) * 15)))

export function CandidateAction({ kind, conversation, candidate, days, selectedDay, onSave, onCancel }: Props) {
  const [day, setDay] = useState(days.some(item => item.day === selectedDay) ? selectedDay : days[0]?.day ?? 1)
  const originalDuration = candidate.duration.match(/^约 (\d+) 分钟$/)?.[1]
  const [duration, setDuration] = useState(originalDuration ?? '60')
  const [options, setOptions] = useState<CandidateSlot[]>([])
  const [selected, setSelected] = useState('')
  const [time, setTime] = useState('')
  const [searched, setSearched] = useState(false)
  const [reason, setReason] = useState('')
  const [reading, setReading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  const savingRef = useRef(false)
  const requestRef = useRef<AbortController | null>(null)
  const selectedOption = options.find(option => option.option_id === selected)
  const targetActivities = days.find(item => item.day === day)?.activities ?? []
  const validTime = !!selectedOption && /^([01]\d|2[0-3]):[0-5]\d$/.test(time) && toMinutes(time) % 15 === 0 && time >= selectedOption.earliest_start && time <= selectedOption.latest_start

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; requestRef.current?.abort() }
  }, [])

  function invalidate() {
    requestRef.current?.abort(); requestRef.current = null
    setOptions([]); setSelected(''); setTime(''); setSearched(false); setReason(''); setReading(false); setError('')
  }
  async function search() {
    if (savingRef.current) return
    const minutes = Number(duration)
    if (!Number.isInteger(minutes) || minutes < 15 || minutes > 720 || (duration !== originalDuration && minutes % 15 !== 0)) {
      setError('停留时长请填写 15～720 分钟，并按 15 分钟递增。'); return
    }
    invalidate()
    const controller = new AbortController()
    requestRef.current = controller; setReading(true)
    try {
      const result = await findCandidateSlots(conversation, candidate.id, day, minutes, controller.signal)
      if (!mounted.current || controller.signal.aborted || requestRef.current !== controller) return
      if (result.revision !== conversation.revision) throw new Error('行程已变化，请返回候选池后重新检查空档。')
      setOptions(result.options); setReason(result.reason ?? '当天没有容得下该停留时长与已知接驳的空档。请缩短停留时间或选择其他日期。'); setSearched(true)
    } catch (reason) {
      if (mounted.current && !controller.signal.aborted && requestRef.current === controller) setError(reason instanceof Error ? reason.message : '空档查询失败，请重试。')
    } finally {
      if (mounted.current && requestRef.current === controller) setReading(false)
    }
  }
  async function save() {
    if (savingRef.current || reading || kind === 'insert' && !validTime) return
    savingRef.current = true; setSaving(true); setError('')
    try { await onSave(kind === 'insert' ? selected : undefined, kind === 'insert' ? time : undefined) }
    catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : '候选没有保存，请重试。') }
    finally { savingRef.current = false; if (mounted.current) setSaving(false) }
  }
  return <div className="candidate-action">
    <p className="activity-action-heading">{candidate.title}<span className="candidate-action-source">{candidate.destination} · {candidate.place.address || '服务未提供地址'}</span></p>
    {kind === 'delete' ? <>
      <p className="candidate-delete-note">确认后从候选草稿删除，备注和费用随候选移除；若已收藏，将在应用修改时移除该收藏。</p>
      <p className="muted-note">应用前可以放弃草稿；应用后可以恢复上一版行程和候选池。</p>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="dialog-actions"><button autoFocus className="text-button" disabled={saving} onClick={onCancel}>取消删除</button><button className="primary-button danger-button" disabled={saving} onClick={() => void save()}>{saving ? <LoaderCircle size={17} className="spin" /> : <Trash2 size={17} />}{saving ? '正在删除…' : '确认从候选删除'}</button></div>
    </> : <>
      <form onSubmit={event => { event.preventDefault(); void search() }} noValidate>
        <fieldset className="activity-action-fields" disabled={saving}>
          <div className="place-timing-fields">
            <div className="field-label"><label htmlFor="candidate-target-day">目标日期</label><select id="candidate-target-day" autoFocus value={day} onChange={event => { invalidate(); setDay(Number(event.target.value)) }}>{days.map(item => <option key={item.day} value={item.day}>第 {item.day} 天</option>)}</select></div>
            <label className="field-label">停留时长（分钟）<input type="number" min={15} max={720} step={15} value={duration} onChange={event => { invalidate(); setDuration(event.target.value) }} /></label>
          </div>
          <p className="muted-note">按全天日程与相邻接驳检查空档，不代表营业或预约可用；不会调整其他活动。</p>
          <button className="secondary-button" type="submit" disabled={reading}>{reading ? <LoaderCircle size={17} className="spin" /> : <Search size={17} />}{reading ? '正在检查空档…' : '检查可用时段'}</button>
        </fieldset>
      </form>
      <div className="candidate-slot-status" role="status" aria-live="polite">{reading ? '正在查询相邻接驳并检查空档…' : searched ? options.length ? '找到 ' + options.length + ' 个可插入时段，请选择一个。' : '没有可用时段。' + reason : '先选择日期与停留时长，再检查可用时段。'}</div>
      {options.length > 0 && <fieldset className="candidate-slots" disabled={saving || reading} aria-label="可用时段"><legend>选择可用时段</legend>{options.map(option => <label key={option.option_id} className={'candidate-slot ' + (selected === option.option_id ? 'is-selected' : '')}>
        <input type="radio" name="candidate-slot" value={option.option_id} checked={selected === option.option_id} onChange={() => { setSelected(option.option_id); setTime(defaultTime(option)); setError('') }} />
        <span><strong>{option.earliest_start}–{option.latest_start} 开始</strong><span>{option.position === 0 ? '当天第一项' : (targetActivities[option.position - 1]?.title ?? '上一项活动') + '之后'}{targetActivities[option.position] ? ' · ' + targetActivities[option.position].title + '之前' : ' · 当天最后一项'}</span>{option.warnings.map((warning, index) => <small className="candidate-warning" key={index}>接驳待确认 · {warning}</small>)}</span>
      </label>)}</fieldset>}
      {selectedOption && <div className="field-label candidate-start-time"><label htmlFor="candidate-start-time">开始时间</label><input id="candidate-start-time" aria-describedby="candidate-start-hint" type="time" step={900} min={selectedOption.earliest_start} max={selectedOption.latest_start} value={time} disabled={saving} aria-invalid={!validTime || undefined} onChange={event => setTime(event.target.value)} /><span id="candidate-start-hint">允许 {selectedOption.earliest_start}–{selectedOption.latest_start}，按 15 分钟选择。</span></div>}
      {selectedOption && !validTime && <p className="inline-error" role="alert">开始时间必须在所选时段内，并按 15 分钟填写。</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="dialog-actions"><button className="text-button" disabled={saving} onClick={onCancel}>返回候选池</button><button className="primary-button" disabled={saving || reading || !validTime} onClick={() => void save()}>{saving ? <LoaderCircle size={17} className="spin" /> : <CalendarPlus size={17} />}{saving ? '正在排入…' : '排入草稿'}</button></div>
    </>}
  </div>
}
