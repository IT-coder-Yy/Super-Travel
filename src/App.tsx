import { useEffect, useRef, useState, type ReactNode } from 'react'
import { isTauri } from '@tauri-apps/api/core'
import { ArrowDown, ArrowRight, ArrowUp, CalendarDays, Check, ChevronRight, Clock3, Download, FolderOpen, Heart, History, Image as ImageIcon, Layers, Lightbulb, LoaderCircle, Map, MapPin, MessageCircle, Minus, Navigation, Pencil, Plus, RotateCcw, Save, Send, Settings, ShieldCheck, Sparkles, X } from 'lucide-react'
import { applyTripDraft, cancelAgent, createConversation, listConversations, listRecoveryOptions, readConversation, recoverConversation, resolveTripPlace, restorePreviousTrip, saveConversation, sendAgentMessage } from './api'
import { cancelUpdate, checkForUpdate, chooseDataDirectory, getAppVersion, getBackendConnection, installUpdate, onBeforeClose, type UpdateCheck, type UpdatePhase } from './desktop'
import { MapCanvas } from './components/MapCanvas'
import { RealMap } from './components/RealMap'
import { ModelSettings } from './components/ModelSettings'
import appIcon from './assets/app-icon.svg'
import { createWorkspace, isWorkspaceData, type Activity, type ConversationSummary, type RecoveryOption, type WorkspaceData } from './types'

const PHOTO = 'https://thumb.wikimedia.org/wikipedia/commons/thumb/d/dc/Sunset_over_the_Zigzag_Bridge%2C_West_Lake%2C_Hangzhou_20090909_1.jpg/960px-Sunset_over_the_Zigzag_Bridge%2C_West_Lake%2C_Hangzhou_20090909_1.jpg'
const PHOTO_TWO = 'https://thumb.wikimedia.org/wikipedia/commons/thumb/6/6e/West_lake_twilight.jpg/500px-West_lake_twilight.jpg'
type Panel = 'history' | 'settings' | 'photos' | 'favorites' | 'edit' | 'new' | 'detail' | null
const describeError = (error: unknown) => error instanceof Error ? error.message : '操作没有完成，请重试。'
let startupUpdateCheck: Promise<UpdateCheck> | null = null

function Photo({ src = PHOTO, className = '', alt = '杭州西湖风景参考照片' }: { src?: string; className?: string; alt?: string }) {
  const [failed, setFailed] = useState(false)
  const [loaded, setLoaded] = useState(false)
  return <div className={`photo ${loaded ? 'photo--loaded' : ''} ${className}`} aria-busy={!loaded && !failed}>
    {!failed && <img src={src} alt={alt} referrerPolicy="no-referrer" onLoad={event => { void event.currentTarget.decode().then(() => setLoaded(true)).catch(() => setFailed(true)) }} onError={() => setFailed(true)} />}
    {!loaded && !failed && <div className="photo-loading"><ImageIcon size={20} /><span>照片加载中</span></div>}
    {failed && <div className="photo-fallback"><MapPin size={24} /><span>西湖 · 山水之间</span></div>}
  </div>
}

function Dialog({ title, children, onClose, wide = false, busy = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => { if (dialog?.open) dialog.close() } }, [])
  return <dialog ref={ref} className={`dialog ${wide ? 'dialog--wide' : ''}`} onCancel={event => { event.preventDefault(); if (!busy) onClose() }} onClick={event => { if (!busy && event.target === event.currentTarget) onClose() }}>
    <div className="dialog-header"><h2>{title}</h2><button className="icon-button" onClick={onClose} disabled={busy} aria-label="关闭窗口"><X size={21} /></button></div>
    {children}
  </dialog>
}

export default function App() {
  const [workspace, setWorkspace] = useState<WorkspaceData>(createWorkspace)
  const dataRef = useRef(workspace)
  const currentRef = useRef<ConversationSummary | null>(null)
  const queueRef = useRef<Promise<unknown>>(Promise.resolve())
  const dirtyRef = useRef(false)
  const closeSaveRef = useRef<() => Promise<void>>(async () => undefined)
  const [current, setCurrent] = useState<ConversationSummary | null>(null)
  const [history, setHistory] = useState<ConversationSummary[]>([])
  const [recoveryOptions, setRecoveryOptions] = useState<RecoveryOption[]>([])
  const [scanning, setScanning] = useState(false)
  const [recovering, setRecovering] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [ready, setReady] = useState(false)
  const [loading, setLoading] = useState(true)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [applying, setApplying] = useState(false)
  const [locating, setLocating] = useState<string | null>(null)
  const [connectionError, setConnectionError] = useState('')
  const [dataDir, setDataDir] = useState('连接后显示本地数据目录')
  const [version, setVersion] = useState('0.1.0')
  const [panel, setPanel] = useState<Panel>(null)
  const [selected, setSelected] = useState('bridge')
  const [zoom, setZoom] = useState(1)
  const [mapReset, setMapReset] = useState(0)
  const [layers, setLayers] = useState(true)
  const [chatOpen, setChatOpen] = useState(false)
  const [input, setInput] = useState('')
  const [newTitle, setNewTitle] = useState('我的新旅程')
  const [toast, setToast] = useState<{ message: string; error?: boolean } | null>(null)
  const [update, setUpdate] = useState<UpdateCheck | null>(null)
  const [checking, setChecking] = useState(false)
  const [installing, setInstalling] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [updatePhase, setUpdatePhase] = useState<UpdatePhase>('downloading')
  const chatEnd = useRef<HTMLDivElement>(null)
  const days = workspace.draft ?? workspace.days
  const trip = workspace.draftTrip ?? workspace.trip
  const day = days.find(item => item.day === workspace.selectedDay) ?? days[0]
  const isExample = days.length > 0 && !trip
  const visibleActivities = workspace.selectedDay === 0 ? days.flatMap(item => item.activities) : day?.activities ?? []
  const selectedActivity = days.flatMap(item => item.activities).find(item => item.id === selected) ?? visibleActivities[0] ?? null
  const selectedIndex = visibleActivities.findIndex(item => item.id === selectedActivity?.id)
  const agentWorking = !!workspace.agentPending || ['running', 'replanning'].includes(workspace.agent?.status ?? '')

  function notify(message: string, error = false) { setToast({ message, error }) }
  function resetMap() { setZoom(1); setMapReset(value => value + 1) }
  function change(next: WorkspaceData, isDirty = true) {
    const nextDays = next.draft ?? next.days
    if (next.selectedDay !== 0 && !nextDays.some(item => item.day === next.selectedDay)) next = { ...next, selectedDay: nextDays[0]?.day ?? 1 }
    dataRef.current = next; dirtyRef.current = isDirty; setWorkspace(next); setDirty(isDirty)
  }
  function setDocument(document: ConversationSummary | null) { currentRef.current = document; setCurrent(document) }

  async function writeWorkspace(next: WorkspaceData): Promise<void> {
    const document = currentRef.current ? await saveConversation(currentRef.current, next) : await createConversation(next.trip ? `${next.trip.destination}之旅` : '我的新旅程', next)
    setDocument(document)
    if (dataRef.current === next) { dirtyRef.current = false; setDirty(false) }
    setHistory(items => [document, ...items.filter(item => item.id !== document.id)])
  }

  async function persist(next: WorkspaceData = dataRef.current): Promise<void> {
    if (!ready) throw new Error('本地服务尚未连接，当前修改还没有保存。请在设置中重新连接。')
    setSaving(true)
    const operation = queueRef.current.catch(() => undefined).then(() => writeWorkspace(next))
    queueRef.current = operation
    try { await operation } finally { if (queueRef.current === operation) setSaving(false) }
  }

  closeSaveRef.current = async () => {
    await queueRef.current.catch(() => undefined)
    if (dirtyRef.current) await persist(dataRef.current)
  }

  useEffect(() => {
    let disposed = false
    let cleanup = () => undefined as void
    void onBeforeClose(() => closeSaveRef.current(), message => notify(message, true)).then(unlisten => {
      if (disposed) unlisten()
      else cleanup = unlisten
    }).catch(error => notify(describeError(error), true))
    return () => { disposed = true; cleanup() }
  }, [])

  async function connect() {
    setLoading(true); setConnectionError('')
    try {
      const connection = await getBackendConnection()
      const [items, damaged] = await Promise.all([listConversations(), listRecoveryOptions()])
      setRecoveryOptions(damaged)
      setDataDir(connection.dataDir); setHistory(items); setReady(true)
      if (!currentRef.current && !dirty && items.length > 0) {
        const latest = await readConversation(items[0]!.id)
        if (!isWorkspaceData(latest.data)) throw new Error('本地会话格式不兼容，原文件已保留。请创建新会话或恢复备份。')
        setDocument(latest); change(latest.data, false)
      }
    } catch (error) { setReady(false); setConnectionError(describeError(error)) }
    finally { setLoading(false) }
  }

  useEffect(() => { void connect(); void getAppVersion().then(setVersion).catch(() => undefined) }, [])
  useEffect(() => {
    if (!isTauri()) return
    let disposed = false
    startupUpdateCheck ??= checkForUpdate()
    void startupUpdateCheck.then(result => { if (!disposed) setUpdate(result) }).catch(() => undefined)
    return () => { disposed = true }
  }, [])
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(null), 6500); return () => window.clearTimeout(timer) }, [toast])
  useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [workspace.messages.length, chatOpen])
  useEffect(() => {
    if (!ready || loading || !dirty || applying || locating || recovering || agentWorking || sending) return
    const timer = window.setTimeout(() => { void persist(workspace).catch(error => notify(describeError(error), true)) }, 900)
    return () => window.clearTimeout(timer)
  }, [workspace, ready, loading, dirty, applying, locating, recovering, agentWorking, sending])
  useEffect(() => {
    const identifier = current?.id
    if (!identifier || !agentWorking || recovering) return
    let disposed = false, pending = false
    const timer = window.setInterval(() => {
      if (pending) return
      pending = true
      void readConversation(identifier).then(document => {
        if (!disposed && currentRef.current?.id === identifier && !dirtyRef.current && isWorkspaceData(document.data)) {
          setDocument(document); change({ ...document.data, selectedDay: dataRef.current.selectedDay }, false)
        }
      }).catch(error => { if (!disposed) notify(describeError(error), true) }).finally(() => { pending = false })
    }, 1200)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [current?.id, agentWorking, recovering])
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => { if (dirty || saving || recovering) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty, saving, recovering])

  function selectDay(value: number) {
    if (applying || locating) return
    change({ ...dataRef.current, selectedDay: value }, !agentWorking)
    const target = days.find(item => item.day === value) ?? days[0]
    if (target?.activities[0]) setSelected(target.activities[0].id)
  }
  function startEdit() {
    if (applying || locating) return
    if (agentWorking) { notify('请等待任务结束或先取消任务，再编辑草稿。'); return }
    if (!day) { setChatOpen(true); document.querySelector<HTMLInputElement>('[aria-label="旅行对话输入"]')?.focus(); return }
    if (!dataRef.current.draft) change({ ...dataRef.current, draft: structuredClone(dataRef.current.days), draftTrip: dataRef.current.trip ?? null })
    setPanel('edit')
  }
  function editActivity(id: string, patch: Partial<Activity>) {
    const draft = structuredClone(dataRef.current.draft ?? dataRef.current.days)
    for (const tripDay of draft) tripDay.activities = tripDay.activities.map(activity => activity.id === id ? { ...activity, ...patch, ...(patch.title !== undefined && patch.title !== activity.title ? { place: undefined } : {}) } : activity)
    change({ ...dataRef.current, draft })
  }
  function moveActivity(id: string, delta: number) {
    const draft = structuredClone(dataRef.current.draft ?? dataRef.current.days)
    const targetDay = draft.find(item => item.activities.some(activity => activity.id === id))
    if (!targetDay) return
    const index = targetDay.activities.findIndex(item => item.id === id)
    if (index + delta < 0 || index + delta >= targetDay.activities.length) return
    const item = targetDay.activities.splice(index, 1)[0]!
    targetDay.activities.splice(index + delta, 0, item)
    change({ ...dataRef.current, draft })
  }
  async function applyDraft() {
    const draft = dataRef.current.draft
    if (!draft || agentWorking || applying || locating) return
    if (draft.some(item => item.activities.some(activity => !activity.title.trim() || !/^([01]\d|2[0-3]):[0-5]\d$/.test(activity.time)))) {
      notify('请填写活动名称和有效的时间（例如 09:30）。', true); return
    }
    setApplying(true)
    try {
      if (trip) {
        if (draft.some(item => item.activities.some(activity => !activity.place))) throw new Error('有地点尚未定位，请先点击“重新定位”，再应用修改。')
        if (dirtyRef.current) await persist()
        await queueRef.current
        const document = await applyTripDraft(currentRef.current!)
        setDocument(document); change(document.data, false)
      } else {
        const next = { ...dataRef.current, days: structuredClone(draft), draft: null, draftTrip: null }
        await persist(next); change(next, false)
      }
      setPanel(null); notify('修改已应用，正式行程已保存到本机。')
    } catch (error) { notify(describeError(error), true) }
    finally { setApplying(false) }
  }
  async function locateActivity(activity: Activity) {
    if (!activity.title.trim() || agentWorking || locating || applying) return
    setLocating(activity.id)
    try {
      if (dirtyRef.current) await persist()
      await queueRef.current
      const document = await resolveTripPlace(currentRef.current!, activity.id, activity.title.trim())
      setDocument(document); change(document.data, false); notify('地点已重新定位，应用草稿后保存到正式行程。')
    } catch (error) { notify(describeError(error), true) }
    finally { setLocating(null) }
  }
  async function restoreTrip() {
    if (!currentRef.current || agentWorking || applying) return
    if (dataRef.current.draft) { notify('请先应用或放弃当前草稿，再恢复上一版。'); return }
    setApplying(true)
    const identifier = currentRef.current.id
    try {
      if (dirtyRef.current) await persist()
      await queueRef.current
      if (currentRef.current?.id !== identifier) throw new Error('已切换会话，请在原会话中恢复上一版。')
      const document = await restorePreviousTrip(currentRef.current!)
      if (currentRef.current?.id === identifier) { setDocument(document); change(document.data, false) }
      notify('上一版正式行程已恢复并保存。')
    } catch (error) { notify(describeError(error), true) }
    finally { setApplying(false) }
  }
  async function openHistory(id: string) {
    if (applying || locating) return
    try {
      if (dirty) await persist()
      await queueRef.current
      const document = await readConversation(id)
      if (!isWorkspaceData(document.data)) throw new Error('此会话数据格式不兼容，原文件已保留。')
      setDocument(document); change(document.data, false); setPanel(null); setSelected(document.data.days[0]?.activities[0]?.id ?? '')
    } catch (error) { notify(describeError(error), true) }
  }
  async function newConversation() {
    if (!newTitle.trim() || applying || locating) return
    try {
      if (dirty) await persist()
      await queueRef.current
      const next = createWorkspace()
      const document = await createConversation(newTitle.trim(), next)
      setDocument(document); change(next, false); setHistory(await listConversations()); setPanel(null); setSelected(''); setChatOpen(false)
      notify('新会话已创建，告诉旅伴目的地和旅行天数即可开始。')
    } catch (error) { notify(describeError(error), true) }
  }
  async function sendMessage() {
    const text = input.trim()
    if (!text || text.length > 4000 || sending || workspace.agentPending || applying || locating) return
    setSending(true)
    try {
      if (!currentRef.current || dirtyRef.current) await persist()
      await queueRef.current
      const identifier = currentRef.current!.id
      const document = await sendAgentMessage(identifier, text, crypto.randomUUID())
      if (currentRef.current?.id === identifier) { setDocument(document); change(document.data, false); setInput(''); setChatOpen(true) }
    } catch (error) { notify(describeError(error), true) }
    finally { setSending(false) }
  }
  async function stopAgent() {
    const identifier = currentRef.current?.id
    if (!identifier) return
    try {
      const document = await cancelAgent(identifier)
      if (currentRef.current?.id === identifier && isWorkspaceData(document.data)) { setDocument(document); change(document.data, false) }
    } catch (error) { notify(describeError(error), true) }
  }
  async function checkUpdate() {
    setChecking(true); setUpdate(null)
    try { setUpdate(await checkForUpdate()) } catch (error) { notify(describeError(error), true) }
    finally { setChecking(false) }
  }
  async function runUpdate() {
    setInstalling(true); setProgress(null); setUpdatePhase('downloading')
    try { await persist(); await installUpdate((percent, phase) => { setProgress(percent); setUpdatePhase(phase) }) } catch (error) { notify(describeError(error), true); setInstalling(false) }
  }
  async function selectDataDirectory() {
    try {
      if (dirty) await persist()
      const path = await chooseDataDirectory()
      if (path) notify(`已选择 ${path}。下次启动使用新目录，现有文件不会自动迁移。`)
    } catch (error) { notify(describeError(error), true) }
  }
  async function scanRecovery() {
    setScanning(true)
    try {
      const [items, damaged] = await Promise.all([listConversations(), listRecoveryOptions()])
      setHistory(items); setRecoveryOptions(damaged)
    } catch (error) { notify(describeError(error), true) }
    finally { setScanning(false) }
  }
  async function restoreConversation(option: RecoveryOption) {
    if (!option.token) return
    setRecovering(option.id)
    try {
      const operation = queueRef.current.catch(() => undefined).then(async () => {
        if (currentRef.current?.id !== option.id && dirtyRef.current) await writeWorkspace(dataRef.current)
        const document = await recoverConversation(option.id, option.token!)
        if (!isWorkspaceData(document.data)) throw new Error('备份已恢复，但内容与当前界面不兼容。损坏原件已另存保留。')
        setDocument(document); change(document.data, false)
      })
      queueRef.current = operation
      await operation
      await scanRecovery()
      setPanel(null)
      notify('会话已从备份恢复。损坏原件保留在数据目录的 recovered-originals 文件夹。')
    } catch (error) { notify(describeError(error), true); await scanRecovery() }
    finally { setRecovering(null) }
  }
  function favorite(id: string) { if (applying || locating) return; if (agentWorking) { notify('请等待任务结束或先取消任务，再修改收藏。'); return } change({ ...dataRef.current, favorites: dataRef.current.favorites.includes(id) ? dataRef.current.favorites.filter(item => item !== id) : [...dataRef.current.favorites, id] }) }

  const recoverySection = <section className="recovery-section"><h3>会话恢复</h3><p>损坏的会话不影响其他历史记录。恢复前会另存损坏原件，备份保持不变。</p>
    <button className="secondary-button" onClick={() => void scanRecovery()} disabled={!ready || scanning || !!recovering}>{scanning ? '扫描中…' : '扫描本地备份'}</button>
    {ready && recoveryOptions.length === 0 && <p className="muted-note">未发现损坏会话。</p>}
    {recoveryOptions.map(option => <div className="recovery-item" key={option.id}><strong>{option.title}</strong><p>{option.message}</p>
      {option.token && <details><summary>查看可恢复备份</summary><p>备份时间：{new Date(option.backup_updated_at!).toLocaleString('zh-CN')}。恢复后，备份之后的修改可能丢失，损坏原件会另存保留。{current?.id === option.id && dirty ? ' 当前会话尚未保存的修改也会被这份备份替换。' : ''}</p><button className="secondary-button" disabled={!!recovering || saving || installing} onClick={() => void restoreConversation(option)}>{recovering === option.id ? '恢复中…' : '确认恢复备份'}</button></details>}
    </div>)}
  </section>

  return <div className="app-shell">
    <aside className="sidebar" aria-label="主导航">
      <button className="brand-mark" aria-label="返回地图首页" onClick={() => { setPanel(null); resetMap() }}><img src={appIcon} width={42} height={42} alt="" /></button>
      <nav>
        <button className="nav-item" onClick={() => setPanel('new')}><Navigation size={25} /><span>旅程</span></button>
        <button className={`nav-item ${panel === null ? 'active' : ''}`} onClick={() => setPanel(null)} aria-current={panel === null ? 'page' : undefined}><Map size={25} /><span>地图</span></button>
        <button className={`nav-item ${panel === 'photos' ? 'active' : ''}`} onClick={() => setPanel('photos')}><ImageIcon size={25} /><span>照片</span></button>
        <button className={`nav-item ${panel === 'history' ? 'active' : ''}`} onClick={() => setPanel('history')}><History size={25} /><span>历史旅行</span></button>
        <button className={`nav-item ${panel === 'favorites' ? 'active' : ''}`} onClick={() => setPanel('favorites')}><Heart size={25} /><span>收藏</span></button>
      </nav>
      <div className="sidebar-bottom">{update?.version && <button className="update-notice" aria-label={`发现新版本 v${update.version}，查看更新`} onClick={() => setPanel('settings')}><Download size={18} /><span>更新可用</span></button>}<button className="nav-item settings-nav" onClick={() => setPanel('settings')} aria-label="设置与更新"><Settings size={24} /><span>设置</span></button><div className="travel-signature"><span className={`connection-dot ${ready ? 'online' : ''}`} /><span>好旅行<br />从这里开始</span></div></div>
    </aside>
    <div className="main-shell">
      <header className="topbar">
        <strong className="wordmark">SuperTravelAgent</strong><span className="header-divider" /><span className="trip-title">{current?.title ?? '我的新旅程'}</span><span className="header-tagline">慢下来，遇见更美的风景</span>
        <div className="trip-meta"><span><CalendarDays size={15} />{days.length ? `${days.length} 天 · ${trip?.destination ?? '杭州示例'}` : '等待旅行需求'}</span></div>
        <button className="save-button" disabled={!ready || saving || loading || !!recovering || agentWorking || sending || applying || !!locating} onClick={() => void persist().then(() => notify('会话与行程已保存到本机。')).catch(error => notify(describeError(error), true))}>{saving ? <LoaderCircle size={18} className="spin" /> : <Save size={18} />}<span>{saving ? '保存中' : '保存行程'}</span></button>
      </header>
      <main className={`workspace ${isExample ? '' : 'workspace--real'} ${day ? '' : 'workspace--empty'}`}>
        {isExample ? <MapCanvas activities={visibleActivities} selected={selectedActivity?.id ?? ''} zoom={zoom} layers={layers} onSelect={setSelected} /> : <RealMap activities={visibleActivities} selected={selectedActivity?.id ?? ''} zoom={zoom} layers={layers} onSelect={setSelected} fitKey={`${current?.id ?? 'new'}-${workspace.selectedDay}-${mapReset}`} />}
        <section className="intro" aria-label="旅行概览">{isExample ? <><p className="location-line">HANGZHOU <span>· 杭州</span></p><h1>在湖山之间，<br />留一点空白。</h1><p className="intro-description">三天两晚，把日子过慢一些。</p></> : <><h1>{trip ? `${trip.destination}，慢慢走。` : '下一站，想去哪里？'}</h1><p className="intro-description">{trip ? trip.summary : '告诉旅伴目的地、天数和偏好，开始一段自己的旅程。'}</p></>}</section>
        {days.length > 0 && <div className="day-tabs" role="tablist" aria-label="行程日期">{[0, ...days.map(item => item.day)].map(value => <button key={value} role="tab" aria-selected={workspace.selectedDay === value} className={workspace.selectedDay === value ? 'selected' : ''} onClick={() => selectDay(value)}>{value === 0 ? '全部' : `第 ${value} 天`}</button>)}</div>}
        <div className="map-tools" aria-label="地图工具"><button className={`tool-layer ${layers ? 'is-on' : ''}`} onClick={() => setLayers(!layers)} aria-pressed={layers} aria-label="切换地图图层"><Layers size={23} /><span>标注</span></button><div className="zoom-tools"><button onClick={() => setZoom(value => Math.min(1.6, +(value + .15).toFixed(2)))} disabled={zoom >= 1.6} aria-label="放大地图"><Plus size={23} /></button><button onClick={() => setZoom(value => Math.max(.85, +(value - .15).toFixed(2)))} disabled={zoom <= .85} aria-label="缩小地图"><Minus size={23} /></button><button onClick={resetMap} aria-label="重置地图视角"><Navigation size={21} /></button></div></div>
        {day && <section className="itinerary-card" aria-label="当日行程">
          <div className={`day-cover ${isExample ? '' : 'day-cover--real'}`}>{isExample && <><Photo /><div className="day-cover-shade" /></>}<div className="day-cover-copy"><h2>{day.title}</h2><p>第 {day.day} 天 · {day.subtitle}</p></div>{isExample && <span className="example-badge">示例行程</span>}</div>
          <div className="activities">{day.activities.map((activity, index) => <button className={`activity ${selectedActivity?.id === activity.id ? 'selected' : ''}`} key={activity.id} onClick={() => setSelected(activity.id)}><span className="step-number">{index + 1}</span><span className="activity-copy"><span className="activity-heading"><time>{activity.time}</time><strong>{activity.title}</strong></span><span className="activity-duration"><Clock3 size={13} />{activity.duration}</span><span className="activity-description">{activity.description}</span>{trip && !activity.place && <span className="location-pending">地点已改，等待重新定位</span>}</span>{isExample && <Photo className={`activity-thumb thumb-${index}`} src={index === 1 ? PHOTO_TWO : PHOTO} />}</button>)}</div>
          <div className="itinerary-summary"><MapPin size={18} /><span>{day.activities.length} 个地点 · 建议安排</span>{workspace.draft && <span className="draft-label">修改草稿</span>}</div>
          <button className="edit-button" onClick={startEdit} disabled={agentWorking || applying || !!locating}><Pencil size={18} />{workspace.draft ? '继续编辑草稿' : '编辑当天行程'}</button>
          {workspace.previousTrip && <button className="restore-trip-button" disabled={agentWorking || applying || !!workspace.draft} onClick={() => void restoreTrip()}><RotateCcw size={14} />恢复上一版行程</button>}
        </section>}
        <section className="companion-area" aria-label="旅行对话">
          {(workspace.agentPending || workspace.agent && workspace.agent.status !== 'idle') && <div className="agent-progress" aria-label="Agent 任务进度"><div className="agent-status"><span role="status">{workspace.agentPending ? '规划 Agent 正在处理消息…' : workspace.agent?.status === 'running' ? '执行 Agent 正在查询…' : workspace.agent?.status === 'replanning' ? '规划 Agent 正在调整计划…' : workspace.agent?.status === 'completed' ? '任务已完成' : workspace.agent?.status === 'needs_input' ? '需要你补充信息' : workspace.agent?.status === 'cancelled' ? '任务已取消' : workspace.agent?.status === 'interrupted' ? '上次任务已中断' : '任务未完成'}</span>{agentWorking && <button className="text-button" onClick={() => void stopAgent()}>取消任务</button>}</div>{workspace.agent?.plan?.length ? <details><summary>查看计划 · 已调用工具 {workspace.agent.tool_calls ?? 0} 次</summary><ol>{workspace.agent.plan.map((step, index) => <li key={`${index}-${step}`}>{step}</li>)}</ol></details> : null}{workspace.agent?.error && <p className="inline-error">{workspace.agent.error}</p>}{workspace.agent?.status === 'interrupted' && <p>可重新发送需求继续，已保存的内容仍然保留。</p>}</div>}
          {chatOpen && <div className="chat-history"><div className="chat-heading"><span><Sparkles size={16} />旅伴对话 <small>双 Agent</small></span><button className="icon-button" aria-label="收起对话" onClick={() => setChatOpen(false)}><X size={17} /></button></div><div className="chat-messages">{workspace.messages.length === 0 ? <p className="chat-empty">{isExample ? '可以聊天、补充需求或查询旅行资料。当前展示的是示例行程。' : '例如：帮我安排杭州 3 天旅行，喜欢园林和美食，走路少一些。'}</p> : workspace.messages.map(message => <div key={message.id} className={`message message--${message.role}`}><small>{message.role === 'user' ? '你' : '旅伴'}</small><p>{message.content}</p></div>)}<div ref={chatEnd} /></div></div>}
          <button className="travel-tip" onClick={() => { setChatOpen(true); setInput(day ? '想把这一天安排得更轻松一些' : '帮我安排杭州 3 天旅行，喜欢园林和美食，走路少一些') }}><Lightbulb size={18} /><span>{day ? '想调整节奏？告诉旅伴，也可以直接编辑。' : '试试：杭州 3 天，园林、美食，走路少一些。'}</span><ChevronRight size={18} /></button>
          <form className="chat-composer" onSubmit={event => { event.preventDefault(); void sendMessage() }}><Sparkles size={23} className="composer-icon" /><input aria-label="旅行对话输入" maxLength={4000} value={input} onChange={event => setInput(event.target.value)} placeholder={day ? '想怎么调整？告诉旅伴…' : '想去哪里，玩几天？'} onFocus={() => { if (workspace.messages.length > 0) setChatOpen(true) }} /><button className="send-button" aria-label="发送消息" type="submit" disabled={!input.trim() || sending || !!workspace.agentPending || !ready || applying || !!locating}><Send size={21} /></button></form>
          <div className="companion-actions"><button onClick={() => { setChatOpen(!chatOpen) }}><MessageCircle size={15} />{chatOpen ? '收起对话' : `旅伴对话${workspace.messages.length ? ` · ${workspace.messages.filter(message => message.role === 'user').length}` : ''}`}</button>{day && <button onClick={startEdit} disabled={agentWorking || applying || !!locating}><Pencil size={15} />调整行程</button>}</div>
        </section>
        {selectedActivity && <section className={`place-card ${isExample ? '' : 'place-card--real'}`} aria-label="选中地点"><div className="place-photo">{isExample && <Photo />}<button className={`favorite-button ${workspace.favorites.includes(selectedActivity.id) ? 'is-favorite' : ''}`} aria-label={workspace.favorites.includes(selectedActivity.id) ? '取消收藏地点' : '收藏地点'} aria-pressed={workspace.favorites.includes(selectedActivity.id)} onClick={() => favorite(selectedActivity.id)}><Heart size={24} /></button></div><div className="place-content"><h2>{selectedActivity.title}</h2><p>{selectedActivity.place?.address || selectedActivity.description}</p><div className="place-footer"><span className="place-chip"><MapPin size={12} />第 {Math.max(selectedIndex + 1, 1)} 站</span><button onClick={() => setPanel('detail')}>查看详情 <ArrowRight size={15} /></button></div></div></section>}
        <div className="map-footer"><span className="storage-indicator" title={connectionError || dataDir}><span className={`connection-dot ${ready ? 'online' : ''}`} />{loading ? '正在连接本地服务' : ready ? saving ? '正在保存到本机' : dirty ? '有未保存修改' : workspace.agent?.persisted === false ? '部分任务进度未保存' : current ? '已保存到本机' : '本地服务已连接' : '本地服务未连接'}{!ready && !loading && <button onClick={() => setPanel('settings')}>查看</button>}</span><span className="map-disclaimer"><Navigation size={11} />{isExample ? '概念预览 · 示意地图与示例行程，非实时数据' : day ? '地点已查询 · 时间为建议，开放与预约请另行确认' : '生成行程后，在地图上查看真实地点'}</span></div>
      </main>
    </div>
    {toast && <div className={`toast ${toast.error ? 'toast--error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <ShieldCheck size={18} /> : <Check size={18} />}<span>{toast.message}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setToast(null)}><X size={16} /></button></div>}
    {panel === 'settings' && <Dialog title="设置与更新" onClose={() => { if (!installing && !recovering) setPanel(null) }} busy={!!recovering}><div className="settings-content"><section><h3>本地数据</h3><p>会话和行程以 JSON 文件保存在本机，应用升级不会替换这些文件。</p><div className="path-field"><FolderOpen size={17} /><span>{dataDir}</span></div><div className="button-row"><button className="secondary-button" onClick={() => void selectDataDirectory()} disabled={!ready || saving || !!recovering}>选择数据目录</button><button className="text-button" onClick={() => void connect()} disabled={loading || !!recovering}>{loading ? '连接中…' : '重新连接服务'}</button></div>{connectionError && <p className="inline-error">{connectionError}</p>}</section>{recoverySection}<section><h3>客户端更新 <span className="version">v{version}</span></h3><p>发现新版本后由你确认下载，校验通过并保存当前内容后重启更新。</p><button className="secondary-button" onClick={() => void checkUpdate()} disabled={checking || installing || !!recovering}>{checking ? <LoaderCircle size={16} className="spin" /> : <RotateCcw size={16} />}{checking ? '正在检查' : '检查更新'}</button>{update && <div className="update-result">{!update.configured ? <p>更新源尚未配置。发布仓库与签名配置完成后即可检查新版本。</p> : update.version ? <><strong>发现新版本 v{update.version}</strong><p className="release-notes">{update.body || '此版本没有附加更新说明。'}</p><button className="primary-button" onClick={() => void runUpdate()} disabled={installing || !ready || !!recovering}>{installing ? <LoaderCircle size={16} className="spin" /> : <Download size={16} />}{installing ? updatePhase === 'installing' ? '校验完成，正在安装并重启…' : progress === null ? '正在下载并校验…' : `下载中 ${Math.round(progress)}%` : '保存并更新'}</button>{installing && <button className="text-button" disabled={updatePhase === 'installing'} onClick={() => void cancelUpdate().catch(error => notify(describeError(error), true))}>取消下载</button>}</> : <p>当前已是最新版本。</p>}</div>}</section><ModelSettings disabled={!!recovering || installing || sending} /><details className="attributions"><summary>照片与界面说明</summary><p>西湖照片来自 Wikimedia Commons：DXR（桥畔日落）与 Tyler Valentine（西湖暮色），CC BY-SA 4.0。以裁切方式展示，不能视为特定活动的现场照片。</p><a href="https://commons.wikimedia.org/wiki/File:Sunset_over_the_Zigzag_Bridge,_West_Lake,_Hangzhou_20090909_1.jpg" target="_blank" rel="noreferrer">桥畔日落来源</a><a href="https://commons.wikimedia.org/wiki/File:West_lake_twilight.jpg" target="_blank" rel="noreferrer">西湖暮色来源</a><a href="https://creativecommons.org/licenses/by-sa/4.0/" target="_blank" rel="noreferrer">CC BY-SA 4.0</a><p>界面沿用 SuperTravel-Agent README 预览风格。真实行程使用 OpenStreetMap 底图；历史示例仍显示示意地图。地点连线仅表示访问顺序，不是导航路线。</p></details></div></Dialog>}
    {panel === 'history' && <Dialog title="历史旅行" onClose={() => setPanel(null)}><div className="history-list">{recoveryOptions.length > 0 && <button className="recovery-notice" onClick={() => setPanel('settings')}>有 {recoveryOptions.length} 个会话无法读取，前往设置查看恢复选项。</button>}{history.length === 0 ? <div className="empty-state"><History size={34} /><h3>旅行，从这里留下足迹</h3><p>保存第一份行程后，下次打开就能继续。</p><button className="primary-button" disabled={!ready || saving || !!recovering} onClick={() => void persist().then(() => notify('第一份行程已保存。')).catch(error => notify(describeError(error), true))}>保存当前行程</button></div> : history.map(item => <button className="history-item" key={item.id} disabled={saving || !!recovering} onClick={() => void openHistory(item.id)}><span className="history-icon"><Map size={22} /></span><span><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleString('zh-CN')} · 本地会话</small></span>{current?.id === item.id ? <span className="current-label">当前</span> : <ChevronRight size={18} />}</button>)}</div></Dialog>}
    {panel === 'new' && <Dialog title="开始一段新旅程" onClose={() => setPanel(null)}><div className="new-trip-content"><p>先给这段旅程取个名字。</p><label className="field-label">会话名称<input autoFocus maxLength={80} value={newTitle} onChange={event => setNewTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void newConversation() }} /></label><p className="muted-note">创建空白会话，再告诉旅伴国内目的地、1～7 天行程和你的偏好。</p><button className="primary-button" disabled={!ready || !newTitle.trim() || saving || !!recovering || applying || !!locating} onClick={() => void newConversation()}><Plus size={17} />创建本地会话</button></div></Dialog>}
    {panel === 'edit' && day && <Dialog title="编辑当天行程" onClose={() => setPanel(null)} wide busy={applying || !!locating}><div className="edit-content">
      <div className="edit-notice"><Pencil size={17} /><span>正在编辑草稿。点击“应用修改”后才会替换已保存行程。</span></div>
      <fieldset className="editor-form" disabled={applying || !!locating || agentWorking} aria-busy={applying || !!locating}>
        <div className="editor-day-tabs">{days.map(item => <button key={item.day} className={day.day === item.day ? 'selected' : ''} onClick={() => selectDay(item.day)}>第 {item.day} 天</button>)}</div>
        {day.activities.map((activity, index) => <div className="editor-activity" key={activity.id}>
          <span className="editor-number">{index + 1}</span>
          <div className="editor-fields">
            <div className="editor-inline"><label className="field-label">时间<input type="time" value={activity.time} onChange={event => editActivity(activity.id, { time: event.target.value })} /></label><label className="field-label name-field">地点<input maxLength={60} value={activity.title} onChange={event => editActivity(activity.id, { title: event.target.value })} /></label></div>
            {trip && <div className="editor-location"><span>{activity.place ? activity.place.address || '已定位' : '地点名称已改，请重新定位。'}</span><button type="button" className="text-button" disabled={!activity.title.trim() || !ready} onClick={() => void locateActivity(activity)}>{locating === activity.id ? '定位中…' : '重新定位'}</button></div>}
            <div className="field-label"><label htmlFor={`notes-${activity.id}`}>备注</label><textarea id={`notes-${activity.id}`} rows={2} maxLength={500} value={activity.description} onChange={event => editActivity(activity.id, { description: event.target.value })} /></div>
          </div>
          <div className="reorder-buttons"><button className="icon-button" aria-label={`上移${activity.title}`} disabled={index === 0} onClick={() => moveActivity(activity.id, -1)}><ArrowUp size={16} /></button><button className="icon-button" aria-label={`下移${activity.title}`} disabled={index === day.activities.length - 1} onClick={() => moveActivity(activity.id, 1)}><ArrowDown size={16} /></button></div>
        </div>)}
        <p className="muted-note">{trip ? '改地点后需重新定位；调整顺序时也请调整时间，避免活动重叠。时间为建议，地图不代替实际导航。' : '当前编辑的是示例行程，地点位置不作为真实旅行依据。'}</p>
        <div className="dialog-actions"><button className="text-button" onClick={() => { change({ ...dataRef.current, draft: null, draftTrip: null }); setPanel(null); notify('已放弃草稿，正式行程未改变。') }}>放弃修改</button><button className="primary-button" disabled={!ready || saving || !!recovering} onClick={() => void applyDraft()}><Check size={17} />{applying ? '正在应用…' : '应用修改'}</button></div>
      </fieldset>
    </div></Dialog>}
    {panel === 'photos' && <Dialog title="旅行中的片刻" onClose={() => setPanel(null)} wide><div className="photo-gallery"><figure><Photo /><figcaption>西湖桥畔的日落 · DXR</figcaption></figure><figure><Photo src={PHOTO_TWO} /><figcaption>西湖暮色 · Tyler Valentine</figcaption></figure><p>西湖风景参考，不代表当前行程的地点或实时实拍。授权及来源见“设置与更新”。</p></div></Dialog>}
    {panel === 'favorites' && <Dialog title="收藏的地点" onClose={() => setPanel(null)}><div className="favorites-list">{workspace.favorites.length === 0 ? <div className="empty-state"><Heart size={34} /><h3>把心动的地方留下</h3><p>点击地点卡片上的爱心，收藏你想去的地方。</p></div> : days.flatMap(item => item.activities).filter(activity => workspace.favorites.includes(activity.id)).map(activity => <button className="favorite-item" key={activity.id} onClick={() => { setSelected(activity.id); setPanel('detail') }}><MapPin size={21} /><span><strong>{activity.title}</strong><small>{activity.description}</small></span><ChevronRight size={17} /></button>)}</div></Dialog>}
    {panel === 'detail' && selectedActivity && <Dialog title={selectedActivity.title} onClose={() => setPanel(null)}><div className="detail-content">{isExample && <Photo />}<p>{selectedActivity.description}</p><dl><div><dt>建议时间</dt><dd>{selectedActivity.time}</dd></div><div><dt>预计停留</dt><dd>{selectedActivity.duration}</dd></div>{selectedActivity.place && <div><dt>地点地址</dt><dd>{selectedActivity.place.address || '服务未提供地址'}</dd></div>}<div><dt>信息状态</dt><dd>{isExample ? '示例 · 实际开放与预约待确认' : selectedActivity.place ? '地点已查询 · 开放与预约待确认' : '地点已改，等待重新定位'}</dd></div></dl><p className="muted-note">{isExample ? '照片为西湖风景参考，地图位置仅供界面演示，不作为导航或预订依据。' : '时间安排为旅行建议。地图用于查看地点，出行前请确认开放、预约和交通情况。'}</p><button className="secondary-button" onClick={() => favorite(selectedActivity.id)}><Heart size={17} />{workspace.favorites.includes(selectedActivity.id) ? '取消收藏' : '收藏这个地方'}</button></div></Dialog>}
  </div>
}
