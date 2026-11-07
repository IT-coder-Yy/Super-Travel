import { useEffect, useState } from 'react'
import { isTauri } from '@tauri-apps/api/core'
import { checkForUpdate, installUpdate, type UpdateCheck, type UpdatePhase } from '../desktop'

let startupUpdateCheck: Promise<UpdateCheck> | null = null
const describeError = (error: unknown) => error instanceof Error ? error.message : '操作没有完成，请重试。'

export function useAppUpdate(persist: () => Promise<void>, notify: (message: string, error?: boolean) => void) {
  const [update, setUpdate] = useState<UpdateCheck | null>(null)
  const [checking, setChecking] = useState(false)
  const [installing, setInstalling] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [updatePhase, setUpdatePhase] = useState<UpdatePhase>('downloading')

  useEffect(() => {
    if (!isTauri()) return
    let disposed = false
    startupUpdateCheck ??= checkForUpdate()
    void startupUpdateCheck.then(result => { if (!disposed) setUpdate(result) }).catch(() => undefined)
    return () => { disposed = true }
  }, [])

  async function checkUpdate() {
    setChecking(true); setUpdate(null)
    try { setUpdate(await checkForUpdate()) } catch (error) { notify(describeError(error), true) }
    finally { setChecking(false) }
  }
  async function runUpdate() {
    setInstalling(true); setProgress(null); setUpdatePhase('downloading')
    try { await persist(); await installUpdate((percent, phase) => { setProgress(percent); setUpdatePhase(phase) }) } catch (error) { notify(describeError(error), true); setInstalling(false) }
  }
  return { update, checking, installing, progress, updatePhase, checkUpdate, runUpdate }
}
