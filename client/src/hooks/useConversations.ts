import { useEffect, useRef, useState } from 'react'
import { createConversation, listConversations, listRecoveryOptions, readConversation, recoverConversation, saveConversation } from '../api'
import { getBackendConnection } from '../desktop'
import { createWorkspace, isWorkspaceData, type ConversationSummary, type RecoveryOption, type WorkspaceData } from '../types'

const describeError = (error: unknown) => error instanceof Error ? error.message : '操作没有完成，请重试。'

export function useConversations(notify: (message: string, error?: boolean) => void, onRecovered: () => void) {
  const [workspace, setWorkspace] = useState<WorkspaceData>(createWorkspace)
  const dataRef = useRef(workspace)
  const currentRef = useRef<ConversationSummary | null>(null)
  const queueRef = useRef<Promise<unknown>>(Promise.resolve())
  const dirtyRef = useRef(false)
  const [current, setCurrent] = useState<ConversationSummary | null>(null)
  const [history, setHistory] = useState<ConversationSummary[]>([])
  const [recoveryOptions, setRecoveryOptions] = useState<RecoveryOption[]>([])
  const [scanning, setScanning] = useState(false)
  const [recovering, setRecovering] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [loading, setLoading] = useState(true)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [connectionError, setConnectionError] = useState('')
  const [dataDir, setDataDir] = useState('连接后显示本地数据目录')

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
      onRecovered()
      notify('会话已从备份恢复。损坏原件保留在数据目录的 recovered-originals 文件夹。')
    } catch (error) { notify(describeError(error), true); await scanRecovery() }
    finally { setRecovering(null) }
  }
  useEffect(() => { void connect() }, [])

  return {
    workspace, dataRef, currentRef, queueRef, dirtyRef,
    current, history, setHistory, recoveryOptions, scanning, recovering,
    ready, loading, dirty, saving, dataDir, connectionError,
    change, setDocument, persist, connect, scanRecovery, restoreConversation
  }
}
