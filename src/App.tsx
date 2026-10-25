import { useEffect, useRef, useState, type ReactNode } from 'react'
import { isTauri } from '@tauri-apps/api/core'
import { ArrowDown, ArrowRight, ArrowUp, CalendarDays, Check, ChevronRight, Clock3, Download, FolderOpen, Heart, History, Image as ImageIcon, Layers, Lightbulb, LoaderCircle, Map, MapPin, MessageCircle, Minus, Navigation, Pencil, Plus, RotateCcw, Save, Send, Settings, ShieldCheck, Sparkles, Users, X } from 'lucide-react'
import { createConversation, listConversations, readConversation, saveConversation } from './api'
import { cancelUpdate, checkForUpdate, chooseDataDirectory, getAppVersion, getBackendConnection, installUpdate, onBeforeClose, type UpdateCheck, type UpdatePhase } from './desktop'
import { MapCanvas } from './components/MapCanvas'
import { createExample, isWorkspaceData, type Activity, type ConversationSummary, type TripDay, type WorkspaceData } from './types'

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
  const [workspace, setWorkspace] = useState<WorkspaceData>(createExample)
  const dataRef = useRef(workspace)
  const currentRef = useRef<ConversationSummary | null>(null)
  const queueRef = useRef<Promise<unknown>>(Promise.resolve())
  const dirtyRef = useRef(false)
  const closeSaveRef = useRef<() => Promise<void>>(async () => undefined)
  const [current, setCurrent] = useState<ConversationSummary | null>(null)
  const [history, setHistory] = useState<ConversationSummary[]>([])
  const [ready, setReady] = useState(false)
  const [loading, setLoading] = useState(true)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [applying, setApplying] = useState(false)
  const [connectionError, setConnectionError] = useState('')
  const [dataDir, setDataDir] = useState('连接后显示本地数据目录')
  const [version, setVersion] = useState('0.1.0')
  const [panel, setPanel] = useState<Panel>(null)
  const [selected, setSelected] = useState('bridge')
  const [zoom, setZoom] = useState(1)
  const [layers, setLayers] = useState(true)
  const [chatOpen, setChatOpen] = useState(false)
  const [input, setInput] = useState('')
  const [newTitle, setNewTitle] = useState('我的杭州慢游')
  const [toast, setToast] = useState<{ message: string; error?: boolean } | null>(null)
  const [update, setUpdate] = useState<UpdateCheck | null>(null)
  const [checking, setChecking] = useState(false)
  const [installing, setInstalling] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [updatePhase, setUpdatePhase] = useState<UpdatePhase>('downloading')
  const chatEnd = useRef<HTMLDivElement>(null)
  const days = workspace.draft ?? workspace.days
  const day = days.find(item => item.day === workspace.selectedDay) ?? days[0]!
  const visibleActivities = workspace.selectedDay === 0 ? days.flatMap(item => item.activities) : day.activities
  const selectedActivity = days.flatMap(item => item.activities).find(item => item.id === selected) ?? visibleActivities[0] ?? null
  const selectedIndex = visibleActivities.findIndex(item => item.id === selectedActivity?.id)

  function notify(message: string, error = false) { setToast({ message, error }) }
  function change(next: WorkspaceData, isDirty = true) { dataRef.current = next; dirtyRef.current = isDirty; setWorkspace(next); setDirty(isDirty) }
  function setDocument(document: ConversationSummary | null) { currentRef.current = document; setCurrent(document) }

  async function persist(next: WorkspaceData = dataRef.current): Promise<void> {
    if (!ready) throw new Error('本地服务尚未连接，当前修改还没有保存。请在设置中重新连接。')
    setSaving(true)
    const operation = queueRef.current.catch(() => undefined).then(async () => {
      const document = currentRef.current ? await saveConversation(currentRef.current, next) : await createConversation('杭州三日慢游', next)
      setDocument(document)
      if (dataRef.current === next) { dirtyRef.current = false; setDirty(false) }
      setHistory(items => [document, ...items.filter(item => item.id !== document.id)])
    })
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
      const items = await listConversations()
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
    if (!ready || loading || !dirty || applying) return
    const timer = window.setTimeout(() => { void persist(workspace).catch(error => notify(describeError(error), true)) }, 900)
    return () => window.clearTimeout(timer)
  }, [workspace, ready, loading, dirty, applying])
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => { if (dirty || saving) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty, saving])

  function selectDay(value: number) {
    change({ ...dataRef.current, selectedDay: value })
    const target = days.find(item => item.day === value) ?? days[0]
    if (target?.activities[0]) setSelected(target.activities[0].id)
  }
  function startEdit() {
    if (!dataRef.current.draft) change({ ...dataRef.current, draft: structuredClone(dataRef.current.days) })
    setPanel('edit')
  }
  function editActivity(id: string, patch: Partial<Activity>) {
    const draft = structuredClone(dataRef.current.draft ?? dataRef.current.days)
    for (const tripDay of draft) tripDay.activities = tripDay.activities.map(activity => activity.id === id ? { ...activity, ...patch } : activity)
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
    if (!draft) return
    if (draft.some(item => item.activities.some(activity => !activity.title.trim() || !/^([01]\d|2[0-3]):[0-5]\d$/.test(activity.time)))) {
      notify('请填写活动名称和有效的时间（例如 09:30）。', true); return
    }
    const next = { ...dataRef.current, days: structuredClone(draft), draft: null }
    setApplying(true)
    try { await persist(next); change(next, false); setPanel(null); notify('修改已应用，正式行程已保存到本机。') } catch (error) { notify(describeError(error), true) }
    finally { setApplying(false) }
  }
  async function openHistory(id: string) {
    try {
      if (dirty) await persist()
      await queueRef.current
      const document = await readConversation(id)
      if (!isWorkspaceData(document.data)) throw new Error('此会话数据格式不兼容，原文件已保留。')
      setDocument(document); change(document.data, false); setPanel(null); setSelected(document.data.days[0]?.activities[0]?.id ?? '')
    } catch (error) { notify(describeError(error), true) }
  }
  async function newConversation() {
    if (!newTitle.trim()) return
    try {
      if (dirty) await persist()
      await queueRef.current
      const next = createExample()
      const document = await createConversation(newTitle.trim(), next)
      setDocument(document); change(next, false); setHistory(await listConversations()); setPanel(null); setSelected('bridge'); setChatOpen(false)
      notify('新的本地示例会话已创建。')
    } catch (error) { notify(describeError(error), true) }
  }
  function sendMessage() {
    const text = input.trim()
    if (!text || text.length > 4000) return
    const next: WorkspaceData = { ...dataRef.current, messages: [...dataRef.current.messages,
      { id: crypto.randomUUID(), role: 'user', content: text },
      { id: crypto.randomUUID(), role: 'assistant', content: '已记下你的旅行想法。当前版本正在搭建桌面基础，尚未连接规划 Agent 和实时工具。你可以先浏览、编辑并保存示例行程；这段对话也会保存到本机。' },
    ] }
    change(next); setInput(''); setChatOpen(true)
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
  function favorite(id: string) { change({ ...dataRef.current, favorites: dataRef.current.favorites.includes(id) ? dataRef.current.favorites.filter(item => item !== id) : [...dataRef.current.favorites, id] }) }

  return <div className="app-shell">
    <aside className="sidebar" aria-label="主导航">
      <button className="brand-mark" aria-label="返回地图首页" onClick={() => { setPanel(null); setZoom(1) }}><img src="/app-icon.svg" width={38} height={38} alt="" /></button>
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
        <strong className="wordmark">SuperTravelAgent</strong><span className="header-divider" /><span className="trip-title">{current?.title ?? '杭州三日慢游'}</span><span className="header-tagline">慢下来，遇见更美的风景</span>
        <div className="trip-meta"><span><CalendarDays size={15} />日期待定</span><span><Users size={15} />2 位成人 · 3 天</span></div>
        <button className="save-button" disabled={!ready || saving || loading} onClick={() => void persist().then(() => notify('会话与行程已保存到本机。')).catch(error => notify(describeError(error), true))}>{saving ? <LoaderCircle size={18} className="spin" /> : <Save size={18} />}<span>{saving ? '保存中' : '保存行程'}</span></button>
      </header>
      <main className="workspace">
        <MapCanvas activities={visibleActivities} selected={selectedActivity?.id ?? ''} zoom={zoom} layers={layers} onSelect={setSelected} />
        <section className="intro" aria-label="杭州旅行概览"><p className="location-line">HANGZHOU <span>· 杭州</span></p><h1>在湖山之间，<br />留一点空白。</h1><p className="intro-description">三天两晚，把日子过慢一些。</p></section>
        <div className="day-tabs" role="tablist" aria-label="行程日期">{[0, 1, 2, 3].map(value => <button key={value} role="tab" aria-selected={workspace.selectedDay === value} className={workspace.selectedDay === value ? 'selected' : ''} onClick={() => selectDay(value)}>{value === 0 ? '全部' : `第 ${value} 天`}</button>)}</div>
        <div className="map-tools" aria-label="地图工具"><button className={`tool-layer ${layers ? 'is-on' : ''}`} onClick={() => setLayers(!layers)} aria-pressed={layers} aria-label="切换地图图层"><Layers size={23} /><span>图层</span></button><div className="zoom-tools"><button onClick={() => setZoom(value => Math.min(1.6, +(value + .15).toFixed(2)))} disabled={zoom >= 1.6} aria-label="放大地图"><Plus size={23} /></button><button onClick={() => setZoom(value => Math.max(.85, +(value - .15).toFixed(2)))} disabled={zoom <= .85} aria-label="缩小地图"><Minus size={23} /></button><button onClick={() => setZoom(1)} aria-label="重置地图视角"><Navigation size={21} /></button></div></div>
        <section className="itinerary-card" aria-label="当日行程">
          <div className="day-cover"><Photo /><div className="day-cover-shade" /><div className="day-cover-copy"><h2>{day.title}</h2><p>第 {day.day} 天 · {day.subtitle}</p></div><span className="example-badge">示例行程</span></div>
          <div className="activities">{day.activities.map((activity, index) => <button className={`activity ${selectedActivity?.id === activity.id ? 'selected' : ''}`} key={activity.id} onClick={() => setSelected(activity.id)}><span className="step-number">{index + 1}</span><span className="activity-copy"><span className="activity-heading"><time>{activity.time}</time><strong>{activity.title}</strong></span><span className="activity-duration"><Clock3 size={13} />{activity.duration}</span><span className="activity-description">{activity.description}</span></span><Photo className={`activity-thumb thumb-${index}`} src={index === 1 ? PHOTO_TWO : PHOTO} /></button>)}</div>
          <div className="itinerary-summary"><MapPin size={18} /><span>{day.activities.length} 个地点 · 节奏舒缓</span>{workspace.draft && <span className="draft-label">修改草稿</span>}</div>
          <button className="edit-button" onClick={startEdit}><Pencil size={18} />{workspace.draft ? '继续编辑草稿' : '编辑当天行程'}</button>
        </section>
        <section className="companion-area" aria-label="旅行对话">
          {chatOpen && <div className="chat-history"><div className="chat-heading"><span><Sparkles size={16} />旅伴对话 <small>本地预览</small></span><button className="icon-button" aria-label="收起对话" onClick={() => setChatOpen(false)}><X size={17} /></button></div><div className="chat-messages">{workspace.messages.length === 0 ? <p className="chat-empty">写下你的旅行想法。当前版本可保存对话，真实规划将在后续接入。</p> : workspace.messages.map(message => <div key={message.id} className={`message message--${message.role}`}><small>{message.role === 'user' ? '你' : '应用提示'}</small><p>{message.content}</p></div>)}<div ref={chatEnd} /></div></div>}
          <button className="travel-tip" onClick={() => { setChatOpen(true); setInput('想把这一天安排得更轻松一些') }}><Lightbulb size={18} /><span>旅伴建议：留一点时间，给路上的风景。</span><ChevronRight size={18} /></button>
          <form className="chat-composer" onSubmit={event => { event.preventDefault(); sendMessage() }}><Sparkles size={23} className="composer-icon" /><input aria-label="旅行对话输入" maxLength={4000} value={input} onChange={event => setInput(event.target.value)} placeholder="想少走一点路？先告诉旅伴…" onFocus={() => { if (workspace.messages.length > 0) setChatOpen(true) }} /><button className="send-button" aria-label="发送消息" type="submit" disabled={!input.trim()}><Send size={21} /></button></form>
          <div className="companion-actions"><button onClick={() => { setChatOpen(!chatOpen) }}><MessageCircle size={15} />{chatOpen ? '收起对话' : `旅伴对话${workspace.messages.length ? ` · ${workspace.messages.length / 2}` : ''}`}</button><button onClick={startEdit}><Pencil size={15} />调整行程</button></div>
        </section>
        {selectedActivity && <section className="place-card" aria-label="选中地点"><div className="place-photo"><Photo /><button className={`favorite-button ${workspace.favorites.includes(selectedActivity.id) ? 'is-favorite' : ''}`} aria-label={workspace.favorites.includes(selectedActivity.id) ? '取消收藏地点' : '收藏地点'} aria-pressed={workspace.favorites.includes(selectedActivity.id)} onClick={() => favorite(selectedActivity.id)}><Heart size={24} /></button></div><div className="place-content"><h2>{selectedActivity.title}</h2><p>{selectedActivity.description}</p><div className="place-footer"><span className="place-chip"><MapPin size={12} />第 {Math.max(selectedIndex + 1, 1)} 站</span><button onClick={() => setPanel('detail')}>查看详情 <ArrowRight size={15} /></button></div></div></section>}
        <div className="map-footer"><span className="storage-indicator" title={connectionError || dataDir}><span className={`connection-dot ${ready ? 'online' : ''}`} />{loading ? '正在连接本地服务' : ready ? saving ? '正在保存到本机' : dirty ? '有未保存修改' : current ? '已保存到本机' : '本地服务已连接' : '本地服务未连接'}{!ready && !loading && <button onClick={() => setPanel('settings')}>查看</button>}</span><span className="map-disclaimer"><Navigation size={11} /> 概念预览 · 示意地图与示例行程，非实时数据</span></div>
      </main>
    </div>
    {toast && <div className={`toast ${toast.error ? 'toast--error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <ShieldCheck size={18} /> : <Check size={18} />}<span>{toast.message}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setToast(null)}><X size={16} /></button></div>}
    {panel === 'settings' && <Dialog title="设置与更新" onClose={() => { if (!installing) setPanel(null) }}><div className="settings-content"><section><h3>本地数据</h3><p>会话和行程以 JSON 文件保存在本机，应用升级不会替换这些文件。</p><div className="path-field"><FolderOpen size={17} /><span>{dataDir}</span></div><div className="button-row"><button className="secondary-button" onClick={() => void selectDataDirectory()} disabled={!ready || saving}>选择数据目录</button><button className="text-button" onClick={() => void connect()} disabled={loading}>{loading ? '连接中…' : '重新连接服务'}</button></div>{connectionError && <p className="inline-error">{connectionError}</p>}</section><section><h3>客户端更新 <span className="version">v{version}</span></h3><p>发现新版本后由你确认下载，校验通过并保存当前内容后重启更新。</p><button className="secondary-button" onClick={() => void checkUpdate()} disabled={checking || installing}>{checking ? <LoaderCircle size={16} className="spin" /> : <RotateCcw size={16} />}{checking ? '正在检查' : '检查更新'}</button>{update && <div className="update-result">{!update.configured ? <p>更新源尚未配置。发布仓库与签名配置完成后即可检查新版本。</p> : update.version ? <><strong>发现新版本 v{update.version}</strong><p className="release-notes">{update.body || '此版本没有附加更新说明。'}</p><button className="primary-button" onClick={() => void runUpdate()} disabled={installing || !ready}>{installing ? <LoaderCircle size={16} className="spin" /> : <Download size={16} />}{installing ? updatePhase === 'installing' ? '校验完成，正在安装并重启…' : progress === null ? '正在下载并校验…' : `下载中 ${Math.round(progress)}%` : '保存并更新'}</button>{installing && <button className="text-button" disabled={updatePhase === 'installing'} onClick={() => void cancelUpdate().catch(error => notify(describeError(error), true))}>取消下载</button>}</> : <p>当前已是最新版本。</p>}</div>}</section><section><h3>模型与工具</h3><p>当前为桌面基础版本。规划 Agent、执行 Agent 和远程 MCP 尚未接入，示例不会触发模型费用。</p></section><details className="attributions"><summary>照片与界面说明</summary><p>西湖照片来自 Wikimedia Commons：DXR（桥畔日落）与 Tyler Valentine（西湖暮色），CC BY-SA 4.0。以裁切方式展示，不能视为特定活动的现场照片。</p><a href="https://commons.wikimedia.org/wiki/File:Sunset_over_the_Zigzag_Bridge,_West_Lake,_Hangzhou_20090909_1.jpg" target="_blank" rel="noreferrer">桥畔日落来源</a><a href="https://commons.wikimedia.org/wiki/File:West_lake_twilight.jpg" target="_blank" rel="noreferrer">西湖暮色来源</a><a href="https://creativecommons.org/licenses/by-sa/4.0/" target="_blank" rel="noreferrer">CC BY-SA 4.0</a><p>界面遵循用户指定的 SuperTravel-Agent README 概念预览；底图为本项目绘制的示意地图，不用于导航。</p></details></div></Dialog>}
    {panel === 'history' && <Dialog title="历史旅行" onClose={() => setPanel(null)}><div className="history-list">{history.length === 0 ? <div className="empty-state"><History size={34} /><h3>旅行，从这里留下足迹</h3><p>保存第一份行程后，下次打开就能继续。</p><button className="primary-button" disabled={!ready || saving} onClick={() => void persist().then(() => notify('第一份行程已保存。')).catch(error => notify(describeError(error), true))}>保存当前行程</button></div> : history.map(item => <button className="history-item" key={item.id} disabled={saving} onClick={() => void openHistory(item.id)}><span className="history-icon"><Map size={22} /></span><span><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleString('zh-CN')} · 本地会话</small></span>{current?.id === item.id ? <span className="current-label">当前</span> : <ChevronRight size={18} />}</button>)}</div></Dialog>}
    {panel === 'new' && <Dialog title="开始一段新旅程" onClose={() => setPanel(null)}><div className="new-trip-content"><p>先给这段旅程取个名字。</p><label className="field-label">会话名称<input autoFocus maxLength={80} value={newTitle} onChange={event => setNewTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void newConversation() }} /></label><p className="muted-note">当前创建本地示例会话，沿用杭州三日展示数据；真实需求规划将在后续接入。</p><button className="primary-button" disabled={!ready || !newTitle.trim() || saving} onClick={() => void newConversation()}><Plus size={17} />创建本地会话</button></div></Dialog>}
    {panel === 'edit' && <Dialog title="编辑当天行程" onClose={() => setPanel(null)} wide busy={applying}><div className="edit-content">
      <div className="edit-notice"><Pencil size={17} /><span>正在编辑草稿。点击“应用修改”后才会替换已保存行程。</span></div>
      <fieldset className="editor-form" disabled={applying} aria-busy={applying}>
        <div className="editor-day-tabs">{days.map(item => <button key={item.day} className={day.day === item.day ? 'selected' : ''} onClick={() => selectDay(item.day)}>第 {item.day} 天</button>)}</div>
        {day.activities.map((activity, index) => <div className="editor-activity" key={activity.id}>
          <span className="editor-number">{index + 1}</span>
          <div className="editor-fields">
            <div className="editor-inline"><label className="field-label">时间<input type="time" value={activity.time} onChange={event => editActivity(activity.id, { time: event.target.value })} /></label><label className="field-label name-field">地点<input maxLength={60} value={activity.title} onChange={event => editActivity(activity.id, { title: event.target.value })} /></label></div>
            <div className="field-label"><label htmlFor={`notes-${activity.id}`}>备注</label><textarea id={`notes-${activity.id}`} rows={2} maxLength={500} value={activity.description} onChange={event => editActivity(activity.id, { description: event.target.value })} /></div>
          </div>
          <div className="reorder-buttons"><button className="icon-button" aria-label={`上移${activity.title}`} disabled={index === 0} onClick={() => moveActivity(activity.id, -1)}><ArrowUp size={16} /></button><button className="icon-button" aria-label={`下移${activity.title}`} disabled={index === day.activities.length - 1} onClick={() => moveActivity(activity.id, 1)}><ArrowDown size={16} /></button></div>
        </div>)}
        <p className="muted-note">本阶段可编辑本地示例；改变地点后不会自动查询新的真实坐标或路线。</p>
        <div className="dialog-actions"><button className="text-button" onClick={() => { change({ ...dataRef.current, draft: null }); setPanel(null); notify('已放弃草稿，正式行程未改变。') }}>放弃修改</button><button className="primary-button" disabled={!ready || saving} onClick={() => void applyDraft()}><Check size={17} />{applying ? '正在应用…' : '应用修改'}</button></div>
      </fieldset>
    </div></Dialog>}
    {panel === 'photos' && <Dialog title="旅行中的片刻" onClose={() => setPanel(null)} wide><div className="photo-gallery"><figure><Photo /><figcaption>西湖桥畔的日落 · DXR</figcaption></figure><figure><Photo src={PHOTO_TWO} /><figcaption>西湖暮色 · Tyler Valentine</figcaption></figure><p>风景参考照片，不是当前示例路线的实时实拍。授权及来源见“设置与更新”。</p></div></Dialog>}
    {panel === 'favorites' && <Dialog title="收藏的地点" onClose={() => setPanel(null)}><div className="favorites-list">{workspace.favorites.length === 0 ? <div className="empty-state"><Heart size={34} /><h3>把心动的地方留下</h3><p>点击地点卡片上的爱心，收藏你想去的地方。</p></div> : days.flatMap(item => item.activities).filter(activity => workspace.favorites.includes(activity.id)).map(activity => <button className="favorite-item" key={activity.id} onClick={() => { setSelected(activity.id); setPanel('detail') }}><MapPin size={21} /><span><strong>{activity.title}</strong><small>{activity.description}</small></span><ChevronRight size={17} /></button>)}</div></Dialog>}
    {panel === 'detail' && selectedActivity && <Dialog title={selectedActivity.title} onClose={() => setPanel(null)}><div className="detail-content"><Photo /><p>{selectedActivity.description}</p><dl><div><dt>建议时间</dt><dd>{selectedActivity.time}</dd></div><div><dt>预计停留</dt><dd>{selectedActivity.duration}</dd></div><div><dt>信息状态</dt><dd>示例 · 实际开放与预约待确认</dd></div></dl><p className="muted-note">照片为西湖风景参考，地图位置仅供界面演示，不作为导航或预订依据。</p><button className="secondary-button" onClick={() => favorite(selectedActivity.id)}><Heart size={17} />{workspace.favorites.includes(selectedActivity.id) ? '取消收藏' : '收藏这个地方'}</button></div></Dialog>}
  </div>
}
