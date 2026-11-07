import { useConversations } from './hooks/useConversations'
import { Dialog } from './components/Dialog'
import { Photo, PHOTO, PHOTO_TWO } from './components/Photo'
import { useAppUpdate } from './hooks/useAppUpdate'
import { useEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowRight, ArrowUp, CalendarDays, Check, ChevronRight, Clock3, Download, FolderOpen, BookmarkPlus, Heart, History, Image as ImageIcon, Layers, Lightbulb, LoaderCircle, Map, MapPin, MessageCircle, Minus, Navigation, Pencil, Plus, RotateCcw, Save, Send, Settings, ShieldCheck, Sparkles, Trash2, X } from 'lucide-react'
import { ApiError, exportTrip, addTripCandidate, deleteTripCandidate, discardTripDraft, insertTripCandidate, parkTripActivity, applyTripDraft, cancelAgent, createConversation, deleteTripActivity, listConversations, moveTripActivity, readConversation, resolveTripPlace, restorePreviousTrip, selectTripPlace, sendAgentMessage } from './api'
import { cancelUpdate, chooseDataDirectory, getAppVersion, onBeforeClose } from './desktop'
import { MapCanvas } from './components/MapCanvas'
import { RealMap } from './components/RealMap'
import { TripLeg } from './components/TripLeg'
import { TripExport } from './components/TripExport'
import { PlacePicker } from './components/PlacePicker'
import { ActivityAction } from './components/ActivityAction'
import { CandidatePool } from './components/CandidatePool'
import { CandidateAction } from './components/CandidateAction'
import { clearInvalidRoutes, formatCost, knownCosts, routeDetails, tripOverview } from './tripDetails'
import { ModelSettings } from './components/ModelSettings'
import { TravelPreferences } from './components/TravelPreferences'
import appIcon from './assets/app-icon.svg'
import { beginTripDraft, currentCandidates, hasTripDraft, createWorkspace, isWorkspaceData, type Activity, type ActivityMove, type Candidate, type ConversationSummary, type PlaceSelection, type TripExportFormat } from './types'

type Panel = 'history' | 'settings' | 'photos' | 'favorites' | 'candidates' | 'edit' | 'new' | 'detail' | 'export' | null
type PlacePickerState = { candidate?: boolean; conversation: ConversationSummary; day: number; activity?: Activity; initialTime: string }
type CandidateActionState = { kind: 'insert' | 'delete'; conversation: ConversationSummary; candidate: Candidate }
type ActivityActionState = { kind: 'move' | 'delete'; conversation: ConversationSummary; activity: Activity }
const activityMinutes = (activity: Activity) => Number(activity.duration.match(/^约 (\d+) 分钟$/)?.[1] ?? 60)
const describeError = (error: unknown) => error instanceof Error ? error.message : '操作没有完成，请重试。'

export default function App() {
  const [panel, setPanel] = useState<Panel>(null)
  const [toast, setToast] = useState<{ message: string; error?: boolean } | null>(null)
  function notify(message: string, error = false) { setToast({ message, error }) }
  const {
    workspace, dataRef, currentRef, queueRef, dirtyRef,
    current, history, setHistory, recoveryOptions, scanning, recovering,
    ready, loading, dirty, saving, dataDir, connectionError,
    change, setDocument, persist, connect, scanRecovery, restoreConversation
  } = useConversations(notify, () => setPanel(null))
  const closeSaveRef = useRef<() => Promise<void>>(async () => undefined)
  const placeFlowRef = useRef(false)
  const placeFocusRef = useRef<string | null>(null)
  const placeSaveRef = useRef<Promise<void> | null>(null)
  const [sending, setSending] = useState(false)
  const [applying, setApplying] = useState(false)
  const [exportSaving, setExportSaving] = useState(false)
  const [locating, setLocating] = useState<string | null>(null)
  const [preparingPlace, setPreparingPlace] = useState(false)
  const [placePicker, setPlacePicker] = useState<PlacePickerState | null>(null)
  const [candidateAction, setCandidateAction] = useState<CandidateActionState | null>(null)
  const [focusedCandidate, setFocusedCandidate] = useState<string | null>(null)
  const [activityAction, setActivityAction] = useState<ActivityActionState | null>(null)
  const [placeSaving, setPlaceSaving] = useState(false)
  const [placeError, setPlaceError] = useState('')
  const [timeInputs, setTimeInputs] = useState<Record<string, string>>({})
  const [version, setVersion] = useState('0.1.0')
  const [selected, setSelected] = useState('bridge')
  const [zoom, setZoom] = useState(1)
  const [mapReset, setMapReset] = useState(0)
  const [layers, setLayers] = useState(true)
  const [chatOpen, setChatOpen] = useState(false)
  const [input, setInput] = useState('')
  const [newTitle, setNewTitle] = useState('我的新旅程')
  const { update, checking, installing, progress, updatePhase, checkUpdate, runUpdate } = useAppUpdate(persist, notify)
  const chatEnd = useRef<HTMLDivElement>(null)
  const hasDraft = hasTripDraft(workspace)
  const canExport = !!workspace.trip && workspace.days.length > 0
  const candidates = currentCandidates(workspace)
  const days = workspace.draft ?? workspace.days
  const trip = workspace.draftTrip ?? workspace.trip
  const day = days.find(item => item.day === workspace.selectedDay) ?? days[0]
  const isExample = days.length > 0 && !trip
  const visibleActivities = workspace.selectedDay === 0 ? days.flatMap(item => item.activities) : day?.activities ?? []
  const selectedActivity = visibleActivities.find(item => item.id === selected) ?? visibleActivities[0] ?? null
  const selectedIndex = visibleActivities.findIndex(item => item.id === selectedActivity?.id)
  const agentWorking = !!workspace.agentPending || ['running', 'replanning'].includes(workspace.agent?.status ?? '')
  const placeFlow = preparingPlace || !!placePicker || !!activityAction || !!candidateAction
  const hasInvalidTime = Object.keys(timeInputs).length > 0
  const favoriteActivities = [...days.flatMap(item => item.activities), ...candidates].filter(activity => workspace.favorites.includes(activity.id))
  const costSummary = knownCosts(days)
  const routeConflict = trip ? days.flatMap(item => item.activities.map((activity, index) => routeDetails(item.activities[index - 1], activity, trip.transportMode ?? 'walking')?.conflict)).find(Boolean) : undefined

  function resetMap() { setZoom(1); setMapReset(value => value + 1) }
  closeSaveRef.current = async () => {
    if (placeSaveRef.current) await placeSaveRef.current
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

  useEffect(() => { void getAppVersion().then(setVersion).catch(() => undefined) }, [])
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(null), 6500); return () => window.clearTimeout(timer) }, [toast])
  useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [workspace.messages.length, chatOpen])
  useEffect(() => {
    if (!ready || loading || !dirty || applying || locating || recovering || agentWorking || sending || placeFlow || panel === 'export') return
    const timer = window.setTimeout(() => { if (!placeFlowRef.current) void persist(workspace).catch(error => notify(describeError(error), true)) }, 900)
    return () => window.clearTimeout(timer)
  }, [workspace, ready, loading, dirty, applying, locating, recovering, agentWorking, sending, placeFlow, panel])
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
    const handler = (event: BeforeUnloadEvent) => { if (dirty || saving || recovering || placeSaving) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty, saving, recovering, placeSaving])

  useEffect(() => {
    if (!['edit', 'candidates'].includes(panel ?? '') || placePicker || activityAction || candidateAction || preparingPlace || !placeFocusRef.current) return
    const trigger = document.getElementById(placeFocusRef.current) as HTMLButtonElement | null
    const fallback = panel === 'candidates' ? document.getElementById('add-trip-candidate') as HTMLButtonElement | null : document.querySelector<HTMLButtonElement>('.editor-place-actions button:not(:disabled)') ?? document.getElementById('add-trip-place') as HTMLButtonElement | null
    if (trigger && !trigger.disabled) trigger.focus()
    else fallback?.focus()
    placeFocusRef.current = null
  }, [panel, placePicker, activityAction, candidateAction, preparingPlace])

  function selectDay(value: number) {
    if (applying || locating || placeFlowRef.current) return
    change({ ...dataRef.current, selectedDay: value }, !agentWorking)
    const target = days.find(item => item.day === value) ?? days[0]
    setSelected(target?.activities[0]?.id ?? '')
  }
  function startEdit() {
    if (applying || locating || placeFlowRef.current) return
    if (agentWorking) { notify('请等待任务结束或先取消任务，再编辑草稿。'); return }
    if (!day) { setChatOpen(true); document.querySelector<HTMLInputElement>('[aria-label="旅行对话输入"]')?.focus(); return }
    if (dataRef.current.draft == null || dataRef.current.draftCandidates == null) change(beginTripDraft(dataRef.current))
    setTimeInputs({})
    setPlaceError('')
    setPanel('edit')
  }
  function editActivity(id: string, patch: Partial<Activity>) {
    if (placeFlowRef.current) return
    setPlaceError('')
    const draft = structuredClone(dataRef.current.draft ?? dataRef.current.days)
    for (const tripDay of draft) tripDay.activities = tripDay.activities.map(activity => activity.id === id ? { ...activity, ...patch, ...(patch.title !== undefined && patch.title !== activity.title ? { place: undefined, cost: undefined } : {}) } : activity)
    clearInvalidRoutes(draft)
    change({ ...beginTripDraft(dataRef.current), draft })
  }
  function editActivityTime(activity: Activity, value: string) {
    if (placeFlowRef.current) return
    if (trip && value !== activity.time && (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value) || Number(value.slice(3)) % 15 !== 0)) {
      setTimeInputs(items => ({ ...items, [activity.id]: value }))
      return
    }
    setTimeInputs(items => { const next = { ...items }; delete next[activity.id]; return next })
    editActivity(activity.id, { time: value })
  }
  function moveActivity(id: string, delta: number) {
    if (placeFlowRef.current) return
    const draft = structuredClone(dataRef.current.draft ?? dataRef.current.days)
    const targetDay = draft.find(item => item.activities.some(activity => activity.id === id))
    if (!targetDay) return
    const index = targetDay.activities.findIndex(item => item.id === id)
    if (index + delta < 0 || index + delta >= targetDay.activities.length) return
    const item = targetDay.activities.splice(index, 1)[0]!
    targetDay.activities.splice(index + delta, 0, item)
    clearInvalidRoutes(draft)
    change({ ...beginTripDraft(dataRef.current), draft })
  }
  async function openPlacePicker(activity?: Activity) {
    if (!ready || !day || !trip || applying || locating || agentWorking || sending || placeFlowRef.current || hasInvalidTime) return
    const identifier = currentRef.current?.id
    const targetDay = day.day
    placeFocusRef.current = activity ? 'replace-trip-place-' + activity.id : 'add-trip-place'
    placeFlowRef.current = true
    setPreparingPlace(true); setPlaceError('')
    try {
      await queueRef.current.catch(() => undefined)
      if (dirtyRef.current) await persist()
      await queueRef.current
      const current = currentRef.current
      if (!current || current.id !== identifier || !dataRef.current.draft) throw new Error('草稿已变化，请重新打开编辑。')
      const currentDay = dataRef.current.draft.find(item => item.day === targetDay)
      if (!currentDay) throw new Error('这一天已变化，请重新打开编辑。')
      const last = currentDay.activities.at(-1)
      const end = last ? Number(last.time.slice(0, 2)) * 60 + Number(last.time.slice(3)) + activityMinutes(last) + 30 : 9 * 60
      const start = Math.min(23 * 60, Math.ceil(end / 15) * 15)
      const initialTime = String(Math.floor(start / 60)).padStart(2, '0') + ':' + String(start % 60).padStart(2, '0')
      setPlacePicker({ conversation: { ...current }, day: targetDay, activity: activity ? structuredClone(activity) : undefined, initialTime })
    } catch (error) {
      placeFlowRef.current = false
      setPlaceError(describeError(error))
    } finally { setPreparingPlace(false) }
  }
  async function openActivityAction(kind: 'move' | 'delete', activity: Activity) {
    if (!ready || !trip || applying || locating || agentWorking || sending || placeFlowRef.current || hasInvalidTime) return
    const identifier = currentRef.current?.id
    placeFocusRef.current = kind + '-trip-activity-' + activity.id
    placeFlowRef.current = true
    setPreparingPlace(true); setPlaceError('')
    try {
      await queueRef.current.catch(() => undefined)
      if (dirtyRef.current) await persist()
      await queueRef.current
      const current = currentRef.current
      const savedActivity = dataRef.current.draft?.flatMap(item => item.activities).find(item => item.id === activity.id)
      if (!current || current.id !== identifier || !savedActivity) throw new Error('草稿已变化，请重新打开编辑。')
      setActivityAction({ kind, conversation: { ...current }, activity: structuredClone(savedActivity) })
    } catch (error) {
      placeFlowRef.current = false
      setPlaceError(describeError(error))
    } finally { setPreparingPlace(false) }
  }
  async function saveActivityAction(move?: ActivityMove) {
    if (!activityAction || placeSaveRef.current) return
    const action = activityAction
    const operation = (async () => {
      const current = currentRef.current
      if (!current || current.id !== action.conversation.id || current.revision !== action.conversation.revision || dirtyRef.current) {
        throw new Error('草稿已变化，请返回编辑后重新操作。')
      }
      if (action.kind === 'move' && !move) throw new Error('请先选择移动日期、位置和时间。')
      setPlaceSaving(true)
      const document = action.kind === 'move' ? await moveTripActivity(action.conversation, move!)
        : await deleteTripActivity(action.conversation, action.activity.id)
      if (currentRef.current?.id !== action.conversation.id || currentRef.current.revision !== action.conversation.revision) return
      if (!isWorkspaceData(document.data)) throw new Error('草稿已保存，但返回内容无法显示，请重新打开此会话。')
      setDocument(document); change(document.data, false)
      setHistory(items => [document, ...items.filter(item => item.id !== document.id)])
      const currentDay = document.data.draft?.find(item => item.day === document.data.selectedDay)
      setSelected(action.kind === 'move' ? action.activity.id : currentDay?.activities[0]?.id ?? '')
      placeFlowRef.current = false
      setActivityAction(null)
      notify(action.kind === 'move' ? '活动已移动到草稿，应用修改后更新正式行程。' : '活动已从草稿删除，应用前可以放弃修改。')
    })()
    placeSaveRef.current = operation
    try { await operation } finally { placeSaveRef.current = null; setPlaceSaving(false) }
  }
  function closeEditorAction() {
    if (placeSaveRef.current || preparingPlace) return
    placeFlowRef.current = false
    setPlacePicker(null)
    setActivityAction(null)
    setCandidateAction(null)
  }
  async function savePlaceSelection(selection: PlaceSelection) {
    if (!placePicker || placeSaveRef.current) return
    const picker = placePicker
    const operation = (async () => {
      const current = currentRef.current
      if (!current || current.id !== picker.conversation.id || current.revision !== picker.conversation.revision || dirtyRef.current) {
        throw new Error('草稿已变化，请返回编辑后重新搜索。')
      }
      setPlaceSaving(true)
      const oldIds = new Set((dataRef.current.draft ?? []).flatMap(item => item.activities.map(activity => activity.id)))
      const document = picker.candidate ? await addTripCandidate(picker.conversation, { selection_id: selection.selection_id, duration_minutes: selection.duration_minutes, description: selection.description }) : await selectTripPlace(picker.conversation, selection)
      if (currentRef.current?.id !== picker.conversation.id || currentRef.current.revision !== picker.conversation.revision) return
      if (!isWorkspaceData(document.data)) throw new Error('草稿已保存，但返回内容无法显示，请重新打开此会话。')
      setDocument(document); change(document.data, false)
      setHistory(items => [document, ...items.filter(item => item.id !== document.id)])
      const added = document.data.draft?.flatMap(item => item.activities).find(activity => !oldIds.has(activity.id))
      if (!picker.candidate) setSelected(picker.activity?.id ?? added?.id ?? '')
      placeFlowRef.current = false
      setPlacePicker(null)
      notify(picker.candidate ? '地点已加入候选草稿，应用修改后保存到正式候选池。' : '地点已保存到草稿，应用修改后更新正式行程。')
    })()
    placeSaveRef.current = operation
    try { await operation } finally { placeSaveRef.current = null; setPlaceSaving(false) }
  }
  async function openCandidateFlow(kind: 'search' | 'insert' | 'delete', candidate?: Candidate) {
    if (!ready || !trip || !day || applying || locating || agentWorking || sending || placeFlowRef.current || hasInvalidTime) return
    const identifier = currentRef.current?.id
    placeFocusRef.current = candidate ? kind + '-trip-candidate-' + candidate.id : 'add-trip-candidate'
    placeFlowRef.current = true
    setPreparingPlace(true); setPlaceError('')
    try {
      await queueRef.current.catch(() => undefined)
      if (dirtyRef.current) await persist()
      await queueRef.current
      const current = currentRef.current
      if (!current || current.id !== identifier) throw new Error('会话已变化，请重新打开候选池。')
      if (kind === 'search') setPlacePicker({ candidate: true, conversation: { ...current }, day: day.day, initialTime: '09:00' })
      else {
        const saved = currentCandidates(dataRef.current).find(item => item.id === candidate?.id)
        if (!saved) throw new Error('候选已变化，请重新打开候选池。')
        setCandidateAction({ kind, conversation: { ...current }, candidate: structuredClone(saved) })
      }
    } catch (error) { placeFlowRef.current = false; setPlaceError(describeError(error)) }
    finally { setPreparingPlace(false) }
  }
  async function saveCandidateAction(optionId?: string, time?: string) {
    if (!candidateAction || placeSaveRef.current) return
    const action = candidateAction
    const operation = (async () => {
      const current = currentRef.current
      if (!current || current.id !== action.conversation.id || current.revision !== action.conversation.revision || dirtyRef.current) throw new Error('行程已变化，请返回候选池后重新操作。')
      if (action.kind === 'insert' && (!optionId || !time)) throw new Error('请先检查空档，选择时段和开始时间。')
      setPlaceSaving(true)
      const document = action.kind === 'insert' ? await insertTripCandidate(action.conversation, optionId!, time!) : await deleteTripCandidate(action.conversation, action.candidate.id)
      if (currentRef.current?.id !== action.conversation.id || currentRef.current.revision !== action.conversation.revision) return
      if (!isWorkspaceData(document.data)) throw new Error('草稿已保存，但返回内容无法显示，请重新打开此会话。')
      setDocument(document); change(document.data, false)
      setHistory(items => [document, ...items.filter(item => item.id !== document.id)])
      if (action.kind === 'insert') setSelected(action.candidate.id)
      setCandidateAction(null); setFocusedCandidate(null); placeFlowRef.current = false
      notify(action.kind === 'insert' ? '候选已排入草稿，应用修改后更新正式行程。' : '已从候选草稿删除，应用前可以放弃修改。')
    })()
    placeSaveRef.current = operation
    try { await operation } finally { placeSaveRef.current = null; setPlaceSaving(false) }
  }
  async function parkActivity(activity: Activity) {
    if (placeSaveRef.current || applying || locating || agentWorking || sending || !ready || hasInvalidTime) return
    const fromAction = !!activityAction
    if (placeFlowRef.current && !fromAction) return
    if (!activity.description.trim()) throw new Error('活动备注不能为空，请填写备注后再移入候选。')
    const identifier = currentRef.current?.id
    placeFlowRef.current = true
    if (!fromAction) placeFocusRef.current = 'park-trip-activity-' + activity.id
    setPreparingPlace(true); setPlaceError('')
    const operation = (async () => {
      await queueRef.current.catch(() => undefined)
      if (dirtyRef.current) await persist()
      await queueRef.current
      const current = currentRef.current
      if (!current || current.id !== identifier || fromAction && current.revision !== activityAction!.conversation.revision) throw new Error('草稿已变化，请返回编辑后重新操作。')
      setPlaceSaving(true)
      const document = await parkTripActivity(current, activity.id)
      if (currentRef.current?.id !== current.id || currentRef.current.revision !== current.revision) return
      if (!isWorkspaceData(document.data)) throw new Error('草稿已保存，但返回内容无法显示，请重新打开此会话。')
      setDocument(document); change(document.data, false)
      setHistory(items => [document, ...items.filter(item => item.id !== document.id)])
      setSelected((document.data.draft ?? document.data.days).find(item => item.day === document.data.selectedDay)?.activities[0]?.id ?? '')
      setActivityAction(null); placeFlowRef.current = false
      notify('活动已移入候选草稿，备注、费用和收藏均已保留。')
    })()
    placeSaveRef.current = operation
    try { await operation }
    finally { placeSaveRef.current = null; setPlaceSaving(false); setPreparingPlace(false); if (!fromAction) placeFlowRef.current = false }
  }
  async function discardDraft() {
    if (!hasTripDraft(dataRef.current) || applying || locating || placeFlowRef.current || agentWorking) return
    setPlaceError('')
    setApplying(true)
    try {
      await queueRef.current.catch(() => undefined)
      if (dirtyRef.current) {
        const next = { ...dataRef.current, draft: null, draftTrip: null, draftCandidates: null }
        await persist(next); change(next, false)
      } else if (trip) {
        if (!currentRef.current) throw new Error('会话尚未保存，请先保存后再放弃草稿。')
        const document = await discardTripDraft(currentRef.current)
        if (!isWorkspaceData(document.data)) throw new Error('返回内容无法显示，请重新打开此会话。')
        setDocument(document); change(document.data, false)
      } else {
        const next = { ...dataRef.current, draft: null, draftTrip: null, draftCandidates: null }
        await persist(next); change(next, false)
      }
      setTimeInputs({}); setPanel(null); notify('已放弃日程和候选草稿，正式内容未改变。')
    } catch (error) { setPlaceError(describeError(error)); notify(describeError(error), true) }
    finally { setApplying(false) }
  }
  async function applyDraft(forExport = false) {
    const draft = dataRef.current.draft ?? dataRef.current.days
    if (!hasTripDraft(dataRef.current) || agentWorking || applying || locating || placeFlowRef.current || hasInvalidTime) {
      if (forExport) throw new Error(hasInvalidTime ? '日程中还有无效时间，请关闭导出窗口并返回编辑修正。' : '当前无法应用草稿，请等待其他操作完成后重试。')
      return
    }
    if (routeConflict) { if (forExport) throw new Error(routeConflict); setPlaceError(routeConflict); notify(routeConflict, true); return }
    if (draft.some(item => item.activities.some(activity => !activity.title.trim() || !/^([01]\d|2[0-3]):[0-5]\d$/.test(activity.time)))) {
      const message = '请填写活动名称和有效的时间（例如 09:30）。'
      if (forExport) throw new Error(message)
      setPlaceError(message); notify(message, true); return
    }
    setPlaceError('')
    setApplying(true)
    try {
      if (trip) {
        if (draft.some(item => item.activities.some(activity => !activity.place))) throw new Error('有地点尚未定位，请先点击“重新定位”，再应用修改。')
        if (dirtyRef.current) await persist()
        await queueRef.current
        const document = await applyTripDraft(currentRef.current!)
        setDocument(document); change(document.data, false)
      } else {
        const next = { ...dataRef.current, days: structuredClone(draft), candidates: structuredClone(currentCandidates(dataRef.current)), draft: null, draftTrip: null, draftCandidates: null }
        await persist(next); change(next, false)
      }
      if (!forExport) { setPanel(null); notify('修改已应用，正式行程已保存到本机。') }
    } catch (error) { if (forExport) throw error; setPlaceError(describeError(error)); notify(describeError(error), true) }
    finally { setApplying(false) }
  }
  async function prepareExport(apply: boolean, signal: AbortSignal, format: TripExportFormat) {
    const identifier = currentRef.current?.id
    if (!ready || !identifier || !dataRef.current.trip || !dataRef.current.days.length) throw new Error('请先生成并保存真实正式行程，再导出。')
    if (agentWorking || sending || applying || locating || placeFlowRef.current || recovering) throw new Error('请等待当前操作完成，再导出行程。')
    await queueRef.current.catch(() => undefined)
    signal.throwIfAborted()
    if (currentRef.current?.id !== identifier) throw new Error('会话已切换，请关闭窗口后重新导出。')
    if (dirtyRef.current) await persist(dataRef.current)
    signal.throwIfAborted()
    if (apply) await applyDraft(true)
    signal.throwIfAborted()
    const document = currentRef.current!
    if (document.id !== identifier) throw new Error('会话已切换，请关闭窗口后重新导出。')
    const snapshot = await exportTrip(document, signal, format)
    if (snapshot.revision !== document.revision) throw new ApiError('行程版本已变化，请重新读取并确认。', 409)
    return snapshot
  }
  async function reloadExport(signal: AbortSignal) {
    const identifier = currentRef.current?.id
    if (!identifier) throw new Error('当前没有已保存的会话。')
    await queueRef.current.catch(() => undefined)
    signal.throwIfAborted()
    const document = await readConversation(identifier, signal)
    signal.throwIfAborted()
    if (currentRef.current?.id !== identifier) throw new Error('会话已切换，请关闭窗口后重新导出。')
    if (!isWorkspaceData(document.data)) throw new Error('会话内容无法显示，原文件已保留。')
    setDocument(document); change(document.data, false); setTimeInputs({})
  }
  async function locateActivity(activity: Activity) {
    if (!activity.title.trim() || agentWorking || locating || applying || placeFlowRef.current) return
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
    if (!currentRef.current || agentWorking || applying || placeFlowRef.current) return
    if (hasTripDraft(dataRef.current)) { notify('请先应用或放弃当前草稿，再恢复上一版。'); return }
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
    if (applying || locating || placeFlowRef.current) return
    try {
      if (dirty) await persist()
      await queueRef.current
      const document = await readConversation(id)
      if (!isWorkspaceData(document.data)) throw new Error('此会话数据格式不兼容，原文件已保留。')
      setDocument(document); change(document.data, false); setPanel(null); setSelected(document.data.days[0]?.activities[0]?.id ?? '')
    } catch (error) { notify(describeError(error), true) }
  }
  async function newConversation() {
    if (!newTitle.trim() || applying || locating || placeFlowRef.current) return
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
    if (!text || text.length > 4000 || sending || workspace.agentPending || applying || locating || placeFlowRef.current) return
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
  async function selectDataDirectory() {
    try {
      if (dirty) await persist()
      const path = await chooseDataDirectory()
      if (path) notify(`已选择 ${path}。下次启动使用新目录，现有文件不会自动迁移。`)
    } catch (error) { notify(describeError(error), true) }
  }
  function favorite(id: string) { if (applying || locating || placeFlowRef.current) return; if (agentWorking) { notify('请等待任务结束或先取消任务，再修改收藏。'); return } change({ ...dataRef.current, favorites: dataRef.current.favorites.includes(id) ? dataRef.current.favorites.filter(item => item !== id) : [...dataRef.current.favorites, id] }) }

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
        <button className={`nav-item ${panel === 'candidates' ? 'active' : ''}`} onClick={() => { setFocusedCandidate(null); setPlaceError(''); setPanel('candidates') }}><BookmarkPlus size={25} /><span>候选</span></button>
        <button className={`nav-item ${panel === 'favorites' ? 'active' : ''}`} onClick={() => setPanel('favorites')}><Heart size={25} /><span>收藏</span></button>
      </nav>
      <div className="sidebar-bottom">{update?.version && <button className="update-notice" aria-label={`发现新版本 v${update.version}，查看更新`} onClick={() => setPanel('settings')}><Download size={18} /><span>更新可用</span></button>}<button className="nav-item settings-nav" onClick={() => setPanel('settings')} aria-label="设置与更新"><Settings size={24} /><span>设置</span></button><div className="travel-signature"><span className={`connection-dot ${ready ? 'online' : ''}`} /><span>好旅行<br />从这里开始</span></div></div>
    </aside>
    <div className="main-shell">
      <header className="topbar">
        <strong className="wordmark">SuperTravelAgent</strong><span className="header-divider" /><span className="trip-title">{current?.title ?? '我的新旅程'}</span><span className="header-tagline">慢下来，遇见更美的风景</span>
        <div className="trip-meta"><span><CalendarDays size={15} />{days.length ? `${days.length} 天 · ${trip?.destination ?? '杭州示例'}` : '等待旅行需求'}</span></div>
        <button className="save-button" disabled={!ready || saving || loading || !!recovering || agentWorking || sending || applying || !!locating || placeFlow} onClick={() => void persist().then(() => notify('会话与行程已保存到本机。')).catch(error => notify(describeError(error), true))}>{saving ? <LoaderCircle size={18} className="spin" /> : <Save size={18} />}<span>{saving ? '保存中' : '保存行程'}</span></button>
        <button className="secondary-button export-button" aria-label="导出行程" title={canExport ? '预览并导出正式行程' : '生成并保存真实正式行程后可导出'} disabled={!canExport || !ready || loading || !!recovering || agentWorking || sending || applying || !!locating || placeFlow} onClick={() => setPanel('export')}><Download size={18} /><span>导出行程</span></button>
      </header>
      <main className={`workspace ${isExample ? '' : 'workspace--real'} ${day ? '' : 'workspace--empty'}`}>
        {isExample ? <MapCanvas activities={visibleActivities} selected={selectedActivity?.id ?? ''} zoom={zoom} layers={layers} onSelect={setSelected} /> : <RealMap activities={visibleActivities} selected={selectedActivity?.id ?? ''} zoom={zoom} layers={layers} onSelect={setSelected} fitKey={`${current?.id ?? 'new'}-${workspace.selectedDay}-${mapReset}`} />}
        {(days.length > 0 || !chatOpen && !workspace.agentPending && (!workspace.agent || workspace.agent.status === 'idle')) && <section className="intro" aria-label="旅行概览">{isExample ? <><p className="location-line">HANGZHOU <span>· 杭州</span></p><h1>在湖山之间，<br />留一点空白。</h1><p className="intro-description">三天两晚，把日子过慢一些。</p></> : <><h1>{trip ? `${trip.destination}，慢慢走。` : '下一站，想去哪里？'}</h1><p className="intro-description">{trip ? <>{hasDraft ? '草稿概览' : '正式概览'}：{tripOverview(days)}</> : '告诉旅伴目的地、天数和偏好，开始一段自己的旅程。'}</p></>}</section>}
        {days.length > 0 && <div className="day-tabs" role="tablist" aria-label="行程日期">{[0, ...days.map(item => item.day)].map(value => <button key={value} role="tab" aria-selected={workspace.selectedDay === value} className={workspace.selectedDay === value ? 'selected' : ''} onClick={() => selectDay(value)}>{value === 0 ? '全部' : `第 ${value} 天`}</button>)}</div>}
        <div className="map-tools" aria-label="地图工具"><button className={`tool-layer ${layers ? 'is-on' : ''}`} onClick={() => setLayers(!layers)} aria-pressed={layers} aria-label="切换地图图层"><Layers size={23} /><span>标注</span></button><div className="zoom-tools"><button onClick={() => setZoom(value => Math.min(1.6, +(value + .15).toFixed(2)))} disabled={zoom >= 1.6} aria-label="放大地图"><Plus size={23} /></button><button onClick={() => setZoom(value => Math.max(.85, +(value - .15).toFixed(2)))} disabled={zoom <= .85} aria-label="缩小地图"><Minus size={23} /></button><button onClick={resetMap} aria-label="重置地图视角"><Navigation size={21} /></button></div></div>
        {day && <section className="itinerary-card" aria-label="当日行程">
          <div className={`day-cover ${isExample ? '' : 'day-cover--real'}`}>{isExample && <><Photo /><div className="day-cover-shade" /></>}<div className="day-cover-copy"><h2>{isExample ? day.title : `第 ${day.day} 天行程`}</h2><p>{isExample ? `第 ${day.day} 天 · ${day.subtitle}` : day.activities.length ? `${day.activities.length} 项活动` : '自由安排'}</p></div>{isExample && <span className="example-badge">示例行程</span>}</div>
          <div className="activities">{day.activities.length === 0 && <div className="empty-day"><CalendarDays size={24} /><h3>自由安排</h3><p>这一天还没有活动，可以继续添加想去的地点。</p><button className="secondary-button" onClick={startEdit} disabled={agentWorking || applying || !!locating}>新增当天活动</button></div>}{day.activities.map((activity, index) => <button className={`activity ${selectedActivity?.id === activity.id ? 'selected' : ''}`} key={activity.id} onClick={() => setSelected(activity.id)}><span className="step-number">{index + 1}</span><span className="activity-copy"><span className="activity-heading"><time>{activity.time}</time><strong>{activity.title}</strong></span><span className="activity-duration"><Clock3 size={13} />{activity.duration}</span><span className="activity-description">{activity.description}</span>{trip && <><span className="activity-cost">{activity.cost ? `${formatCost(activity.cost.amountFen)} · 用户填写` : '活动费用未知'}</span><TripLeg previous={day.activities[index - 1]} activity={activity} mode={trip.transportMode ?? 'walking'} /></>}{trip && !activity.place && <span className="location-pending">地点已改，等待重新定位</span>}</span>{isExample && <Photo className={`activity-thumb thumb-${index}`} src={index === 1 ? PHOTO_TWO : PHOTO} />}</button>)}</div>
          <div className="itinerary-summary"><MapPin size={18} /><span>{day.activities.length} 个地点 · 建议安排</span>{hasDraft && <span className="draft-label">修改草稿</span>}</div>
          {trip && <p className="trip-cost-summary" aria-label="行程费用">已知费用小计 <strong>{formatCost(costSummary.amountFen)}</strong><span>{costSummary.unknown ? `另有 ${costSummary.unknown} 项活动费用未知` : '活动费用均已填写'} · 全部天数，不含未填写的交通住宿等开支</span></p>}
          <button className="edit-button" onClick={startEdit} disabled={agentWorking || applying || !!locating}><Pencil size={18} />{hasDraft ? '继续编辑草稿' : '编辑当天行程'}</button>
          {workspace.previousTrip && <button className="restore-trip-button" disabled={agentWorking || applying || hasDraft} onClick={() => void restoreTrip()}><RotateCcw size={14} />恢复上一版行程</button>}
        </section>}
        <section className="companion-area" aria-label="旅行对话">
          {(workspace.agentPending || workspace.agent && workspace.agent.status !== 'idle') && <div className="agent-progress" aria-label="Agent 任务进度"><div className="agent-status"><span role="status">{workspace.agentPending ? '规划 Agent 正在处理消息…' : workspace.agent?.status === 'running' ? '执行 Agent 正在查询…' : workspace.agent?.status === 'replanning' ? '规划 Agent 正在调整计划…' : workspace.agent?.status === 'completed' ? '任务已完成' : workspace.agent?.status === 'needs_input' ? '需要你补充信息' : workspace.agent?.status === 'cancelled' ? '任务已取消' : workspace.agent?.status === 'interrupted' ? '上次任务已中断' : '任务未完成'}</span>{agentWorking && <button className="text-button" onClick={() => void stopAgent()}>取消任务</button>}</div>{!sending && !workspace.agentPending && workspace.agent?.preference_summary?.trim() && <p className="agent-preference-summary">本次规划偏好：{workspace.agent.preference_summary}</p>}{workspace.agent?.plan?.length ? <details><summary>查看计划 · 已调用工具 {workspace.agent.tool_calls ?? 0} 次</summary><ol>{workspace.agent.plan.map((step, index) => <li key={`${index}-${step}`}>{step}</li>)}</ol></details> : null}{workspace.agent?.error && <p className="inline-error">{workspace.agent.error}</p>}{workspace.agent?.status === 'interrupted' && <p>可重新发送需求继续，已保存的内容仍然保留。</p>}</div>}
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
    {panel === 'export' && <Dialog title="导出行程" onClose={() => setPanel(null)} wide busy={applying || exportSaving}><TripExport hasDraft={hasDraft} onPrepare={prepareExport} onReload={reloadExport} onSaving={setExportSaving} /></Dialog>}
    {panel === 'settings' && <Dialog title="设置与更新" onClose={() => { if (!installing && !recovering) setPanel(null) }} busy={!!recovering}><div className="settings-content"><section><h3>本地数据</h3><p>会话和行程以 JSON 文件保存在本机，应用升级不会替换这些文件。</p><div className="path-field"><FolderOpen size={17} /><span>{dataDir}</span></div><div className="button-row"><button className="secondary-button" onClick={() => void selectDataDirectory()} disabled={!ready || saving || !!recovering}>选择数据目录</button><button className="text-button" onClick={() => void connect()} disabled={loading || !!recovering}>{loading ? '连接中…' : '重新连接服务'}</button></div>{connectionError && <p className="inline-error">{connectionError}</p>}</section><TravelPreferences disabled={!!recovering || installing} />{recoverySection}<section><h3>客户端更新 <span className="version">v{version}</span></h3><p>发现新版本后由你确认下载，校验通过并保存当前内容后重启更新。</p><button className="secondary-button" onClick={() => void checkUpdate()} disabled={checking || installing || !!recovering}>{checking ? <LoaderCircle size={16} className="spin" /> : <RotateCcw size={16} />}{checking ? '正在检查' : '检查更新'}</button>{update && <div className="update-result">{!update.configured ? <p>更新源尚未配置。发布仓库与签名配置完成后即可检查新版本。</p> : update.version ? <><strong>发现新版本 v{update.version}</strong><p className="release-notes">{update.body || '此版本没有附加更新说明。'}</p><button className="primary-button" onClick={() => void runUpdate()} disabled={installing || !ready || !!recovering}>{installing ? <LoaderCircle size={16} className="spin" /> : <Download size={16} />}{installing ? updatePhase === 'installing' ? '校验完成，正在安装并重启…' : progress === null ? '正在下载并校验…' : `下载中 ${Math.round(progress)}%` : '保存并更新'}</button>{installing && <button className="text-button" disabled={updatePhase === 'installing'} onClick={() => void cancelUpdate().catch(error => notify(describeError(error), true))}>取消下载</button>}</> : <p>当前已是最新版本。</p>}</div>}</section><ModelSettings disabled={!!recovering || installing || sending} /><details className="attributions"><summary>照片与界面说明</summary><p>西湖照片来自 Wikimedia Commons：DXR（桥畔日落）与 Tyler Valentine（西湖暮色），CC BY-SA 4.0。以裁切方式展示，不能视为特定活动的现场照片。</p><a href="https://commons.wikimedia.org/wiki/File:Sunset_over_the_Zigzag_Bridge,_West_Lake,_Hangzhou_20090909_1.jpg" target="_blank" rel="noreferrer">桥畔日落来源</a><a href="https://commons.wikimedia.org/wiki/File:West_lake_twilight.jpg" target="_blank" rel="noreferrer">西湖暮色来源</a><a href="https://creativecommons.org/licenses/by-sa/4.0/" target="_blank" rel="noreferrer">CC BY-SA 4.0</a><p>界面沿用 SuperTravel-Agent README 预览风格。真实行程使用 OpenStreetMap 底图；历史示例仍显示示意地图。地点连线仅表示访问顺序，不是导航路线。</p></details></div></Dialog>}
    {panel === 'history' && <Dialog title="历史旅行" onClose={() => setPanel(null)}><div className="history-list">{recoveryOptions.length > 0 && <button className="recovery-notice" onClick={() => setPanel('settings')}>有 {recoveryOptions.length} 个会话无法读取，前往设置查看恢复选项。</button>}{history.length === 0 ? <div className="empty-state"><History size={34} /><h3>旅行，从这里留下足迹</h3><p>保存第一份行程后，下次打开就能继续。</p><button className="primary-button" disabled={!ready || saving || !!recovering} onClick={() => void persist().then(() => notify('第一份行程已保存。')).catch(error => notify(describeError(error), true))}>保存当前行程</button></div> : history.map(item => <button className="history-item" key={item.id} disabled={saving || !!recovering} onClick={() => void openHistory(item.id)}><span className="history-icon"><Map size={22} /></span><span><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleString('zh-CN')} · 本地会话</small></span>{current?.id === item.id ? <span className="current-label">当前</span> : <ChevronRight size={18} />}</button>)}</div></Dialog>}
    {panel === 'new' && <Dialog title="开始一段新旅程" onClose={() => setPanel(null)}><div className="new-trip-content"><p>先给这段旅程取个名字。</p><label className="field-label">会话名称<input autoFocus maxLength={80} value={newTitle} onChange={event => setNewTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void newConversation() }} /></label><p className="muted-note">创建空白会话，再告诉旅伴国内目的地、1～7 天行程和你的偏好。</p><button className="primary-button" disabled={!ready || !newTitle.trim() || saving || !!recovering || applying || !!locating} onClick={() => void newConversation()}><Plus size={17} />创建本地会话</button></div></Dialog>}
    {panel === 'edit' && day && <Dialog title={placePicker ? placePicker.activity ? '替换地点' : '新增地点' : activityAction ? activityAction.kind === 'move' ? '移动活动' : '删除活动' : '编辑当天行程'} onClose={() => { if (placePicker || activityAction) closeEditorAction(); else setPanel(null) }} wide busy={applying || !!locating || preparingPlace || placeSaving}><div className="edit-content">
      {placePicker ? <PlacePicker conversation={placePicker.conversation} day={placePicker.day} destination={trip!.destination} activity={placePicker.activity} initialTime={placePicker.initialTime} onSelect={savePlaceSelection} onCancel={closeEditorAction} /> : activityAction ? <ActivityAction kind={activityAction.kind} conversation={activityAction.conversation} activity={activityAction.activity} days={days} onSave={saveActivityAction} onPark={() => parkActivity(activityAction.activity)} onCancel={closeEditorAction} /> : <>
      <div className="edit-notice"><Pencil size={17} /><span>正在编辑草稿。点击“应用修改”后才会替换已保存行程。</span></div>
      <fieldset className="editor-form" disabled={applying || !!locating || agentWorking || preparingPlace} aria-busy={applying || !!locating || preparingPlace}>
        <div className="editor-day-tabs">{days.map(item => <button key={item.day} className={day.day === item.day ? 'selected' : ''} onClick={() => selectDay(item.day)}>第 {item.day} 天</button>)}</div>
        {trip && <div className="editor-place-toolbar"><span>选择真实地点，补充当天安排。</span><button id="add-trip-place" type="button" className="secondary-button" disabled={!ready || day.activities.length >= 8 || hasInvalidTime} onClick={() => void openPlacePicker()}>{preparingPlace ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}新增地点</button></div>}
        {trip && day.activities.length >= 8 && <p className="muted-note">当天已有 8 项活动，可替换现有地点。</p>}
        {placeError && <p className="inline-error" role="alert">{placeError}</p>}
        {hasInvalidTime && !day.activities.some(activity => timeInputs[activity.id] !== undefined) && <p className="inline-error" role="alert">其他日期还有未保存的时间，请返回对应日期修正后再应用。</p>}
        {day.activities.length === 0 && <div className="editor-empty-day"><h3>自由安排</h3><p>这一天暂时留白，点击“新增地点”继续安排。</p></div>}
        {day.activities.map((activity, index) => <div className="editor-activity" key={activity.id}>
          <span className="editor-number">{index + 1}</span>
          <div className="editor-fields">
            <div className="editor-inline"><label className="field-label">时间<input type="time" step={trip ? 900 : undefined} value={timeInputs[activity.id] ?? activity.time} aria-invalid={timeInputs[activity.id] !== undefined || undefined} aria-describedby={timeInputs[activity.id] !== undefined ? 'time-error-' + activity.id : undefined} onChange={event => editActivityTime(activity, event.target.value)} /></label><label className="field-label name-field">地点<input maxLength={60} value={activity.title} onChange={event => editActivity(activity.id, { title: event.target.value })} /></label></div>
            {timeInputs[activity.id] !== undefined && <p className="inline-error editor-time-error" id={'time-error-' + activity.id} role="alert">请按 15 分钟设置时间。此时间尚未保存，关闭后保留原时间；也可以恢复为 {activity.time}。</p>}
            {trip && <div className="field-label duration-field"><label htmlFor={'duration-' + activity.id}>停留时长</label><select id={'duration-' + activity.id} value={activityMinutes(activity)} onChange={event => editActivity(activity.id, { duration: '约 ' + event.target.value + ' 分钟' })}>{Array.from(new Set([activityMinutes(activity), ...Array.from({ length: 48 }, (_, index) => (index + 1) * 15)])).sort((a, b) => a - b).map(minutes => <option key={minutes} value={minutes}>{minutes} 分钟{minutes % 15 ? '（原值）' : ''}</option>)}</select></div>}
            {trip && <div className="editor-location"><span>{activity.place ? activity.place.address || '已定位' : '地点名称已改，请重新定位。'}</span><div className="editor-place-actions"><button id={'replace-trip-place-' + activity.id} type="button" className="text-button" disabled={!ready || hasInvalidTime} onClick={() => void openPlacePicker(activity)}>替换地点</button><button type="button" className="text-button" disabled={!activity.title.trim() || !ready} onClick={() => void locateActivity(activity)}>{locating === activity.id ? '定位中…' : '重新定位'}</button></div></div>}
            {trip && <><TripLeg previous={day.activities[index - 1]} activity={activity} mode={trip.transportMode ?? 'walking'} /><label className="field-label cost-field">活动费用（元）<input aria-label="活动费用（元）" type="number" min="0" max="1000000" step="0.01" inputMode="decimal" placeholder="未知，请留空" value={activity.cost == null ? '' : activity.cost.amountFen / 100} onChange={event => {
              const value = event.target.value
              if (value === '') { editActivity(activity.id, { cost: null }); return }
              if (!event.target.validity.valid) { notify('费用请填写 0～1000000 元，最多两位小数；未知时留空。', true); return }
              editActivity(activity.id, { cost: { amountFen: Math.round(Number(value) * 100), currency: 'CNY', source: 'user' } })
            }} /><span>按用户填写计入小计；确认免费填 0。</span></label></>}
            <div className="field-label"><label htmlFor={`notes-${activity.id}`}>备注</label><textarea id={`notes-${activity.id}`} rows={2} maxLength={500} value={activity.description} onChange={event => editActivity(activity.id, { description: event.target.value })} /></div>
            {trip && <div className="editor-activity-actions"><button id={'park-trip-activity-' + activity.id} type="button" className="text-button" disabled={!ready || hasInvalidTime} onClick={() => void parkActivity(activity).catch(error => setPlaceError(describeError(error)))}><BookmarkPlus size={15} />暂时不去，移入候选</button><button id={'move-trip-activity-' + activity.id} type="button" className="text-button" disabled={!ready || hasInvalidTime} onClick={() => void openActivityAction('move', activity)}><ArrowRight size={15} />移动活动</button><button id={'delete-trip-activity-' + activity.id} type="button" className="text-button danger-text" disabled={!ready || hasInvalidTime} onClick={() => void openActivityAction('delete', activity)}><Trash2 size={15} />删除活动</button></div>}
          </div>
          <div className="reorder-buttons"><button className="icon-button" aria-label={`上移${activity.title}`} disabled={index === 0} onClick={() => moveActivity(activity.id, -1)}><ArrowUp size={16} /></button><button className="icon-button" aria-label={`下移${activity.title}`} disabled={index === day.activities.length - 1} onClick={() => moveActivity(activity.id, 1)}><ArrowDown size={16} /></button></div>
        </div>)}
        <p className="muted-note">{trip ? '改地点后需重新定位，费用可留空；应用时核查新接驳路线。查不到的耗时会标为未知，请自行确认。' : '当前编辑的是示例行程，地点位置不作为真实旅行依据。'}</p>
        {routeConflict && <p className="inline-error" role="alert">{routeConflict} 请调整活动时间后再应用。</p>}
        <div className="dialog-actions"><button className="text-button" onClick={() => void discardDraft()}>放弃修改</button><button className="primary-button" disabled={!ready || saving || !!recovering || !!routeConflict || hasInvalidTime} onClick={() => void applyDraft()}><Check size={17} />{applying ? '正在核查并应用…' : '应用修改'}</button></div>
      </fieldset></>}
    </div></Dialog>}
    {panel === 'candidates' && <Dialog title={placePicker ? '搜索加入候选' : candidateAction ? candidateAction.kind === 'insert' ? '安排候选' : '删除候选' : '候选池'} onClose={() => { if (placePicker || candidateAction) closeEditorAction(); else setPanel(null) }} wide busy={applying || preparingPlace || placeSaving}><div className="candidate-content">
      {placePicker ? <PlacePicker candidate conversation={placePicker.conversation} day={placePicker.day} destination={trip!.destination} initialTime={placePicker.initialTime} onSelect={savePlaceSelection} onCancel={closeEditorAction} /> : candidateAction ? <CandidateAction kind={candidateAction.kind} conversation={candidateAction.conversation} candidate={candidateAction.candidate} days={days} selectedDay={workspace.selectedDay} onSave={saveCandidateAction} onCancel={closeEditorAction} /> : <>
        <div className="candidate-version" role="status">{hasDraft ? '正在查看候选草稿 · 应用后更新正式候选池' : '正式候选池'} · {candidates.length} 个地点</div>
        <CandidatePool candidates={candidates} favorites={workspace.favorites} focusedId={focusedCandidate} disabled={!ready || !trip || agentWorking || sending || applying || preparingPlace || hasInvalidTime} onSearch={() => void openCandidateFlow('search')} onArrange={candidate => void openCandidateFlow('insert', candidate)} onDelete={candidate => void openCandidateFlow('delete', candidate)} onFavorite={favorite} />
        {!trip && <p className="muted-note">生成真实行程后，可以搜索候选并检查可插入时段。</p>}
        {agentWorking && <p className="muted-note">请等待任务结束或先取消任务，再修改候选。</p>}
        {hasInvalidTime && <p className="inline-error" role="alert">日程中还有未保存的时间，请返回编辑修正后再操作候选。</p>}
        {preparingPlace && <p className="activity-impact-status" role="status"><LoaderCircle size={17} className="spin" />正在保存当前修改…</p>}
        {placeError && <p className="inline-error" role="alert">{placeError}</p>}
        {hasDraft && <><p className="muted-note">应用或放弃会同时处理日程与候选草稿。</p>{routeConflict && <p className="inline-error" role="alert">{routeConflict} 请在编辑行程中调整时间。</p>}<div className="dialog-actions"><button className="text-button" disabled={applying || preparingPlace || agentWorking || sending} onClick={() => void discardDraft()}>放弃修改</button><button className="primary-button" disabled={!ready || saving || applying || preparingPlace || agentWorking || sending || !!routeConflict || hasInvalidTime} onClick={() => void applyDraft()}><Check size={17} />{applying ? '正在核查并应用…' : '应用修改'}</button></div></>}
      </>}
    </div></Dialog>}
    {panel === 'photos' && <Dialog title="旅行中的片刻" onClose={() => setPanel(null)} wide><div className="photo-gallery"><figure><Photo /><figcaption>西湖桥畔的日落 · DXR</figcaption></figure><figure><Photo src={PHOTO_TWO} /><figcaption>西湖暮色 · Tyler Valentine</figcaption></figure><p>西湖风景参考，不代表当前行程的地点或实时实拍。授权及来源见“设置与更新”。</p></div></Dialog>}
    {panel === 'favorites' && <Dialog title="收藏的地点" onClose={() => setPanel(null)}><div className="favorites-list">{favoriteActivities.length === 0 ? <div className="empty-state"><Heart size={34} /><h3>把心动的地方留下</h3><p>点击地点卡片上的爱心，收藏你想去的地方。</p></div> : favoriteActivities.map(activity => <button className="favorite-item" key={activity.id} onClick={() => { const targetDay = days.find(item => item.activities.some(candidate => candidate.id === activity.id)); if (targetDay) { selectDay(targetDay.day); setSelected(activity.id); setPanel('detail') } else { setFocusedCandidate(activity.id); setPlaceError(''); setPanel('candidates') } }}><MapPin size={21} /><span><strong>{activity.title}</strong><small>{candidates.some(candidate => candidate.id === activity.id) ? '候选 · ' : ''}{activity.description}</small></span><ChevronRight size={17} /></button>)}</div></Dialog>}
    {panel === 'detail' && selectedActivity && <Dialog title={selectedActivity.title} onClose={() => setPanel(null)}><div className="detail-content">{isExample && <Photo />}<p>{selectedActivity.description}</p><dl><div><dt>建议时间</dt><dd>{selectedActivity.time}</dd></div><div><dt>预计停留</dt><dd>{selectedActivity.duration}</dd></div>{selectedActivity.place && <div><dt>地点地址</dt><dd>{selectedActivity.place.address || '服务未提供地址'}</dd></div>}<div><dt>信息状态</dt><dd>{isExample ? '示例 · 实际开放与预约待确认' : selectedActivity.place ? '地点已查询 · 开放与预约待确认' : '地点已改，等待重新定位'}</dd></div></dl><p className="muted-note">{isExample ? '照片为西湖风景参考，地图位置仅供界面演示，不作为导航或预订依据。' : '时间安排为旅行建议。地图用于查看地点，出行前请确认开放、预约和交通情况。'}</p><button className="secondary-button" onClick={() => favorite(selectedActivity.id)}><Heart size={17} />{workspace.favorites.includes(selectedActivity.id) ? '取消收藏' : '收藏这个地方'}</button></div></Dialog>}
  </div>
}
