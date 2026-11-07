import { useEffect, useRef, useState } from 'react'
import { ArrowRight, BookmarkPlus, LoaderCircle, Trash2 } from 'lucide-react'
import { previewTripActivityDeletion } from '../api'
import type { Activity, ActivityDeletePreview, ActivityMove, ConversationSummary, TripDay } from '../types'

type Props = {
  kind: 'move' | 'delete'
  conversation: ConversationSummary
  activity: Activity
  days: TripDay[]
  onSave: (move?: ActivityMove) => Promise<void>
  onPark: () => Promise<void>
  onCancel: () => void
}

export function ActivityAction({ kind, conversation, activity, days, onSave, onPark, onCancel }: Props) {
  const sourceDay = days.find(day => day.activities.some(item => item.id === activity.id))!
  const [targetDay, setTargetDay] = useState(sourceDay.day)
  const [targetIndex, setTargetIndex] = useState(sourceDay.activities.findIndex(item => item.id === activity.id))
  const [time, setTime] = useState(activity.time)
  const [preview, setPreview] = useState<ActivityDeletePreview | null>(null)
  const [reading, setReading] = useState(kind === 'delete')
  const [reload, setReload] = useState(0)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const mounted = useRef(true)
  const targetActivities = (days.find(day => day.day === targetDay)?.activities ?? []).filter(item => item.id !== activity.id)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    if (kind !== 'delete') return
    const controller = new AbortController()
    setReading(true); setPreview(null); setError('')
    void previewTripActivityDeletion(conversation, activity.id, controller.signal).then(result => {
      if (controller.signal.aborted) return
      if (result.revision !== conversation.revision || result.activity_id !== activity.id) throw new Error('草稿已变化，请返回编辑后重新查看删除影响。')
      setPreview(result)
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '未能读取删除影响，请重试。')
    }).finally(() => { if (!controller.signal.aborted) setReading(false) })
    return () => controller.abort()
  }, [kind, conversation.id, conversation.revision, activity.id, reload])

  async function save(park = false) {
    if (savingRef.current || kind === 'delete' && (!preview || reading)) return
    if (kind === 'move' && (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time) || time !== activity.time && Number(time.slice(3)) % 15 !== 0)) {
      setError('新设置的开始时间请按 15 分钟填写，原有时间不改动时可以保留。'); return
    }
    savingRef.current = true; setSaving(true); setError('')
    try {
      if (park) await onPark()
      else await onSave(kind === 'move' ? { activity_id: activity.id, target_day: targetDay, target_index: targetIndex, time } : undefined)
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : '修改未保存，请重试。')
    } finally {
      savingRef.current = false
      if (mounted.current) setSaving(false)
    }
  }

  return <div className="activity-action">
    <p className="activity-action-heading">第 {sourceDay.day} 天 · {activity.title}</p>
    {kind === 'move' ? <form onSubmit={event => { event.preventDefault(); void save() }} noValidate>
      <fieldset className="activity-action-fields" disabled={saving}>
        <div className="place-timing-fields">
          <div className="field-label"><label htmlFor="move-target-day">目标日期</label><select id="move-target-day" autoFocus value={targetDay} onChange={event => {
            const value = Number(event.target.value)
            const available = days.find(day => day.day === value)?.activities.filter(item => item.id !== activity.id) ?? []
            setTargetDay(value); setTargetIndex(Math.min(available.length, 7)); setError('')
          }}>{days.map(day => <option value={day.day} key={day.day}>第 {day.day} 天{day.day === sourceDay.day ? '（当前）' : ''}</option>)}</select></div>
          <div className="field-label"><label htmlFor="move-target-index">插入位置</label><select id="move-target-index" value={targetIndex} onChange={event => { setTargetIndex(Number(event.target.value)); setError('') }}>
            <option value={0}>排在最前</option>
            {targetActivities.slice(0, 7).map((item, index) => <option key={item.id} value={index + 1}>{item.time} {item.title}之后</option>)}
          </select></div>
        </div>
        <label className="field-label move-time">移动后开始时间<input type="time" step={900} value={time} onChange={event => { setTime(event.target.value); setError('') }} /></label>
        <p className="muted-note">保留停留时长（{activity.duration}）、备注和费用。请让开始时间与插入位置一致；不会自动调整其他活动。</p>
        {targetActivities.length >= 8 && <p className="inline-error" role="alert">目标日期已有 8 项活动，请选择其他日期。</p>}
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="dialog-actions"><button className="text-button" type="button" onClick={onCancel}>返回编辑</button><button className="primary-button" type="submit" disabled={targetActivities.length >= 8}>{saving ? <LoaderCircle size={17} className="spin" /> : <ArrowRight size={17} />}{saving ? '正在保存…' : '移动到草稿'}</button></div>
      </fieldset>
    </form> : <div>
      {reading && <p className="activity-impact-status" role="status"><LoaderCircle size={17} className="spin" />正在读取删除影响…</p>}
      {preview && <dl className="activity-impact">
        <div><dt>备注</dt><dd>{preview.has_note ? '随活动从草稿移除' : '没有备注'}</dd></div>
        <div><dt>收藏</dt><dd>{preview.is_favorite ? '应用修改时移除该活动的收藏' : '未收藏'}</dd></div>
        <div><dt>地图地点</dt><dd>{preview.has_map_point ? '从当前草稿地图移除' : '没有已定位的地图点'}</dd></div>
        <div><dt>已有接驳记录</dt><dd>{preview.affected_routes} 条将清理，应用时核查新的相邻路线</dd></div>
      </dl>}
      <p className="muted-note">确认后仅从草稿删除。应用前可以放弃草稿；应用后可以恢复上一版行程。</p>
      {error && <p className="inline-error" role="alert">{error}</p>}
      {!reading && !preview && <button type="button" className="secondary-button" onClick={() => setReload(value => value + 1)}>重新读取删除影响</button>}
      <div className="activity-park-alternative"><p>只是暂时不去？移入候选可以保留备注、费用和收藏。</p><button type="button" className="secondary-button" disabled={saving || reading || !preview} onClick={() => void save(true)}><BookmarkPlus size={17} />暂时不去，移入候选</button></div>
      <div className="dialog-actions"><button type="button" autoFocus className="text-button" disabled={saving} onClick={onCancel}>取消删除</button><button type="button" className="primary-button danger-button" disabled={saving || reading || !preview} onClick={() => void save()}>{saving ? <LoaderCircle size={17} className="spin" /> : <Trash2 size={17} />}{saving ? '正在删除…' : '确认从草稿删除'}</button></div>
    </div>}
  </div>
}
