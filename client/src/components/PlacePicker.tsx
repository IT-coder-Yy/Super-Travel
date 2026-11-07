import { useEffect, useRef, useState } from 'react'
import { Check, LoaderCircle, MapPin, Search } from 'lucide-react'
import { searchTripPlaces } from '../api'
import type { Activity, ConversationSummary, PlaceCategory, PlaceSelection, PlaceSearchResult } from '../types'

type Props = {
  candidate?: boolean
  conversation: ConversationSummary
  day: number
  destination: string
  activity?: Activity
  initialTime: string
  onSelect: (selection: PlaceSelection) => Promise<void>
  onCancel: () => void
}

export function PlacePicker({ candidate = false, conversation, day, destination, activity, initialTime, onSelect, onCancel }: Props) {
  const [query, setQuery] = useState(activity?.title ?? '')
  const [category, setCategory] = useState<PlaceCategory | ''>('')
  const [results, setResults] = useState<PlaceSearchResult[]>([])
  const [selected, setSelected] = useState('')
  const [searched, setSearched] = useState(false)
  const [searching, setSearching] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [time, setTime] = useState(activity?.time ?? initialTime)
  const originalDuration = activity?.duration.match(/^约 (\d+) 分钟$/)?.[1]
  const [duration, setDuration] = useState(originalDuration ?? '60')
  const [description, setDescription] = useState(activity?.description ?? '')
  const requestRef = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const savingRef = useRef(false)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; requestRef.current?.abort() }
  }, [])

  function invalidateResults() {
    requestRef.current?.abort()
    requestRef.current = null
    setResults([]); setSelected(''); setSearched(false); setSearching(false); setError('')
  }

  async function search() {
    if (query.trim().length < 2 || savingRef.current) return
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setSearching(true); setError(''); setResults([]); setSelected(''); setSearched(false)
    try {
      const response = await searchTripPlaces(conversation, query.trim(), category || null, controller.signal)
      if (!mounted.current || controller.signal.aborted || requestRef.current !== controller) return
      if (response.revision !== conversation.revision) throw new Error('草稿已变化，请返回编辑后重新搜索。')
      setResults(response.results); setSearched(true)
    } catch (reason) {
      if (mounted.current && !controller.signal.aborted && requestRef.current === controller) {
        setError(reason instanceof Error ? reason.message : '搜索没有完成，请重试。')
      }
    } finally {
      if (mounted.current && requestRef.current === controller) setSearching(false)
    }
  }

  async function save() {
    if (savingRef.current || searching || !results.some(result => result.selection_id === selected)) return
    const minutes = Number(duration)
    if (!candidate && (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time) || (time !== activity?.time && Number(time.slice(3)) % 15 !== 0))) {
      setError('开始时间请按 15 分钟设置，例如 09:00、09:15。'); return
    }
    if (!Number.isInteger(minutes) || minutes < 15 || minutes > 720 || (duration !== originalDuration && minutes % 15 !== 0)) {
      setError('停留时长请填写 15～720 分钟，并按 15 分钟递增。'); return
    }
    savingRef.current = true; setSaving(true); setError('')
    try {
      await onSelect({ selection_id: selected, day, activity_id: activity?.id ?? null, time, duration_minutes: minutes, description })
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : '草稿没有保存，请重试。')
    } finally {
      savingRef.current = false
      if (mounted.current) setSaving(false)
    }
  }

  return <div className="place-picker">
    <p className="place-picker-context">{candidate ? destination + ' · 加入候选，暂不排期' : '第 ' + day + ' 天 · ' + destination + (activity ? ' · 替换“' + activity.title + '”' : ' · 新增活动')}</p>
    <form onSubmit={event => { event.preventDefault(); void search() }}>
      <fieldset className="place-picker-fields" disabled={saving}>
        <div className="place-search-row">
          <label className="field-label place-query">搜索地点<input autoFocus required minLength={2} maxLength={100} value={query} placeholder="输入至少两个字的名称或地址" onChange={event => { invalidateResults(); setQuery(event.target.value) }} onKeyDown={event => { if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault() }} /></label>
          <div className="field-label"><label htmlFor="place-picker-category">检索类别</label><select id="place-picker-category" value={category} onChange={event => { invalidateResults(); setCategory(event.target.value as PlaceCategory | '') }}><option value="">不限</option><option value="景点">景点</option><option value="餐饮">餐饮</option><option value="购物">购物</option></select></div>
          <button className="secondary-button" type="submit" disabled={query.trim().length < 2 || searching}>{searching ? <LoaderCircle size={17} className="spin" /> : <Search size={17} />}{searching ? '搜索中…' : '搜索'}</button>
        </div>
        <p className="muted-note">类别用于补充搜索关键词，请根据名称和地址选择地点。</p>
      </fieldset>
    </form>
    <div className="place-search-status" role="status" aria-live="polite">
      {searching ? '正在查询真实地点…' : searched ? results.length ? '找到 ' + results.length + ' 个地点，请选择一个。' : '没有找到地点，请换个名称或补充地址后搜索。' : '搜索结果会显示名称、地址和地图供应商。'}
    </div>
    {results.length > 0 && <fieldset className="place-results" disabled={saving} aria-label="搜索结果">
      <legend className="sr-only">选择真实地点</legend>
      {results.map(result => <label className={'place-result ' + (selected === result.selection_id ? 'is-selected' : '')} key={result.selection_id}>
        <input type="radio" name="trip-place" value={result.selection_id} checked={selected === result.selection_id} onChange={() => { setSelected(result.selection_id); setError('') }} />
        <MapPin size={19} aria-hidden="true" />
        <span><strong>{result.name}</strong><small>{result.address || '服务未提供地址'}</small><span className="place-provider">{result.provider === 'baidu' ? '百度地图' : '高德地图'}</span></span>
      </label>)}
    </fieldset>}
    <form onSubmit={event => { event.preventDefault(); void save() }} noValidate>
      <fieldset className="place-picker-fields place-schedule" disabled={saving}>
        <div className="place-timing-fields">
          {!candidate && <label className="field-label">开始时间<input type="time" required step={900} value={time} onChange={event => setTime(event.target.value)} /></label>}
          <label className="field-label">停留时长（分钟）<input type="number" required min={15} max={720} step={15} value={duration} onChange={event => setDuration(event.target.value)} /></label>
        </div>
        <div className="field-label"><label htmlFor="place-picker-notes">活动备注</label><textarea id="place-picker-notes" rows={2} maxLength={500} value={description} onChange={event => setDescription(event.target.value)} placeholder="可选，写下这次活动的安排" /></div>
        <p className="muted-note">{candidate ? '候选暂不占用行程时间，也不计入行程费用。应用修改后保存到正式候选池。' : <>新设时间按 15 分钟递增。{activity ? '更换为其他地点后，原费用将清空；未改动的备注会保留。' : '新增活动的费用先标为未知。'}保存到草稿后，仍需应用修改才会更新正式行程。</>}</p>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="dialog-actions"><button type="button" className="text-button" onClick={onCancel}>{candidate ? '返回候选池' : '返回编辑'}</button><button type="submit" className="primary-button" disabled={!selected || searching}>{saving ? <LoaderCircle size={17} className="spin" /> : <Check size={17} />}{saving ? '正在保存…' : candidate ? '加入候选草稿' : '保存到草稿'}</button></div>
      </fieldset>
    </form>
  </div>
}
