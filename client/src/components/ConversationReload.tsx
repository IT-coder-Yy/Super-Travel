import { useEffect, useRef, useState } from 'react'

type Props = {
  message?: string
  hasUnsaved: boolean
  onReload: (signal: AbortSignal) => Promise<void>
  onBack?: () => void
}

export function ConversationReload({ message, hasUnsaved, onReload, onBack }: Props) {
  const [phase, setPhase] = useState<'idle' | 'confirm' | 'loading'>('idle')
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])

  function cancel() {
    request.current?.abort()
    request.current = null
    setError('')
    setPhase('idle')
  }

  async function reload() {
    if (request.current) return
    if (hasUnsaved && phase !== 'confirm') { setPhase('confirm'); return }
    const controller = new AbortController()
    request.current = controller
    setPhase('loading')
    setError('')
    try { await onReload(controller.signal) }
    catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '读取失败，请重试。')
    } finally {
      if (!controller.signal.aborted) { request.current = null; setPhase('idle') }
    }
  }

  return <div>
    <p className="inline-error" role="alert">{error || message || '会话已被更新，请重新读取后继续编辑。'}</p>
    {phase === 'confirm' && <p role="status">重新读取会替换本地未保存的输入。取消可保留这些输入。</p>}
    <div className="dialog-actions">
      {phase !== 'idle' ? <button type="button" className="text-button" onClick={cancel}>{phase === 'loading' ? '取消读取' : '保留本地输入'}</button>
        : onBack && <button type="button" className="text-button" onClick={onBack}>返回编辑</button>}
      <button type="button" className="primary-button" autoFocus disabled={phase === 'loading'} onClick={() => void reload()}>
        {phase === 'loading' ? '正在读取…' : phase === 'confirm' ? '确认重新读取会话' : '重新读取会话'}
      </button>
    </div>
  </div>
}
