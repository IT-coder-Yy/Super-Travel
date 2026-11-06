import { useEffect, useRef, useState } from 'react'
import { getTravelPreferences, saveTravelPreferences } from '../api'
import type { TravelPreferences as Preferences, TravelPreferencesResponse } from '../types'

const defaults: Preferences = { schema: 1, enabled: false, budgetAmountFen: null, pace: null, dietaryNotes: '', companionMode: null }
const describeError = (reason: unknown) => reason instanceof Error ? reason.message : '操作没有完成，请重试。'

export function TravelPreferences({ disabled }: { disabled: boolean }) {
  const [preferences, setPreferences] = useState<Preferences>(defaults)
  const [budget, setBudget] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState<'load' | 'save' | 'clear' | null>('load')
  const [dirty, setDirty] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const [budgetError, setBudgetError] = useState('')
  const [sourceError, setSourceError] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const requestRef = useRef<AbortController | null>(null)
  const budgetRef = useRef<HTMLInputElement>(null)
  const clearRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const returnClearFocus = useRef(false)

  function accept(result: TravelPreferencesResponse) {
    setPreferences(result.preferences)
    setBudget(result.preferences.budgetAmountFen === null ? '' : String(result.preferences.budgetAmountFen / 100))
    setSourceError(result.error || ''); setLoaded(true); setDirty(false); setBudgetError('')
  }
  async function load() {
    if (requestRef.current) return
    const controller = new AbortController()
    requestRef.current = controller; setBusy('load'); setError('')
    try {
      const result = await getTravelPreferences(controller.signal)
      if (!controller.signal.aborted) accept(result)
    } catch (reason) {
      if (!controller.signal.aborted) setError(`读取旅行偏好失败：${describeError(reason)}`)
    } finally {
      if (requestRef.current === controller) requestRef.current = null
      if (!controller.signal.aborted) setBusy(null)
    }
  }
  useEffect(() => {
    void load()
    return () => { requestRef.current?.abort(); requestRef.current = null }
  }, [])
  useEffect(() => {
    if (confirmClear) cancelRef.current?.focus()
    else if (!busy && returnClearFocus.current) { clearRef.current?.focus(); returnClearFocus.current = false }
  }, [confirmClear, busy])

  function edit(patch: Partial<Preferences>) {
    setPreferences(value => ({ ...value, ...patch })); setDirty(true); setNotice(''); setError(''); setConfirmClear(false)
  }
  async function save(clear = false) {
    if (disabled || !loaded || busy || requestRef.current) return
    let next = defaults
    if (!clear) {
      const value = budget.trim()
      const [yuan, cents = ''] = value.split('.')
      const amount = value === '' ? null : Number(yuan) * 100 + Number(cents.padEnd(2, '0'))
      if (value !== '' && (!/^\d+(?:\.\d{1,2})?$/.test(value) || !Number.isSafeInteger(amount) || amount! > 100000000)) {
        setBudgetError('预算请填写 0～1000000 元，最多两位小数；未确定时请留空。'); setNotice(''); budgetRef.current?.focus(); return
      }
      next = { ...preferences, budgetAmountFen: amount }
    }
    const controller = new AbortController()
    requestRef.current = controller; setBusy(clear ? 'clear' : 'save'); setError(''); setNotice(''); setBudgetError('')
    try {
      const result = await saveTravelPreferences(next, controller.signal)
      if (controller.signal.aborted) return
      accept(result)
      if (clear) { returnClearFocus.current = true; setConfirmClear(false) }
      if (!result.error) setNotice(clear ? '旅行偏好已清空并停用。仅影响之后开始的任务。' : result.preferences.enabled ? '旅行偏好已保存并启用。仅影响之后开始的任务。' : '旅行偏好已保存，当前已停用。仅影响之后开始的任务。')
    } catch (reason) {
      if (!controller.signal.aborted) setError(`旅行偏好未保存：${describeError(reason)} 输入仍保留，请重试。`)
    } finally {
      if (requestRef.current === controller) requestRef.current = null
      if (!controller.signal.aborted) setBusy(null)
    }
  }

  return <section className="travel-preferences" aria-labelledby="travel-preferences-heading">
    <h3 id="travel-preferences-heading">常用旅行偏好</h3>
    <p>保存并启用后，在后续规划缺少条件时提供默认偏好；本次明确要求和会话已确认条件优先。保存不会改变当前任务或旧行程。</p>
    <form onSubmit={event => { event.preventDefault(); if (!confirmClear) void save() }} noValidate>
      <fieldset className="model-fields" disabled={disabled || !!busy || !loaded} aria-label="常用旅行偏好表单" aria-busy={!!busy}>
        <label className="preference-enabled"><input type="checkbox" checked={preferences.enabled} onChange={event => edit({ enabled: event.target.checked })} />启用常用旅行偏好</label>
        <div className="field-label"><label htmlFor="travel-budget">全程预算目标（元）</label><input ref={budgetRef} id="travel-budget" inputMode="decimal" value={budget} placeholder="未确定请留空" aria-invalid={!!budgetError || undefined} aria-describedby={budgetError ? 'travel-budget-help travel-budget-error' : 'travel-budget-help'} onChange={event => { setBudget(event.target.value); setBudgetError(''); edit({}) }} /></div>
        <p id="travel-budget-help" className="preference-help">人民币全程总预算目标，可填写 0，最多两位小数；不是实际价格或费用达标证明。</p>
        {budgetError && <p id="travel-budget-error" className="inline-error" role="alert">{budgetError}</p>}
        <div className="preference-options">
          <div className="field-label"><label htmlFor="travel-pace">旅行节奏</label><select id="travel-pace" value={preferences.pace ?? ''} onChange={event => edit({ pace: event.target.value as Preferences['pace'] || null })}><option value="">未设置</option><option value="relaxed">慢节奏</option><option value="balanced">适中</option><option value="packed">紧凑</option></select></div>
          <div className="field-label"><label htmlFor="travel-companion">同行方式</label><select id="travel-companion" value={preferences.companionMode ?? ''} onChange={event => edit({ companionMode: event.target.value as Preferences['companionMode'] || null })}><option value="">未设置</option><option value="solo">独自</option><option value="couple">伴侣同行</option><option value="family">亲子</option><option value="friends">朋友同行</option></select></div>
        </div>
        <p className="preference-help">同行方式不代表已确认人数，需要时由旅伴继续询问。</p>
        <div className="field-label"><label htmlFor="travel-dietary">饮食偏好</label><textarea id="travel-dietary" rows={3} maxLength={1000} value={preferences.dietaryNotes} placeholder="例如：偏清淡、不吃辣；未确定请留空" aria-describedby="travel-dietary-help" onChange={event => edit({ dietaryNotes: event.target.value })} /></div>
        <p id="travel-dietary-help" className="preference-help">最多 1000 字。所有修改（包括启用或停用）均须保存后才生效。</p>
        <div className="button-row"><button type="submit" className="secondary-button" disabled={confirmClear}>保存旅行偏好</button><button ref={clearRef} type="button" className="text-button" onClick={() => { setConfirmClear(true); setNotice('') }}>清空旅行偏好</button></div>
        {confirmClear && <div className="preference-clear" role="group" aria-labelledby="clear-preferences-heading"><p id="clear-preferences-heading">确认清空全部常用旅行偏好并停用？此操作也会丢弃本表单尚未保存的输入。</p><div className="button-row"><button ref={cancelRef} type="button" className="secondary-button" onClick={() => { returnClearFocus.current = true; setConfirmClear(false) }}>取消清空</button><button type="button" className="text-button danger-text" onClick={() => void save(true)}>确认清空偏好</button></div></div>}
      </fieldset>
    </form>
    {busy && <p role="status">{busy === 'load' ? '正在读取旅行偏好…' : busy === 'clear' ? '正在清空旅行偏好…' : '正在保存旅行偏好…'}</p>}
    {loaded && dirty && !busy && <p role="status">有未保存的修改，保存后才生效。</p>}
    {notice && <p role="status">{notice}</p>}
    {sourceError && <p className="inline-error" role="alert">{sourceError}</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!loaded && !busy && <button type="button" className="secondary-button" disabled={disabled} onClick={() => void load()}>重新读取旅行偏好</button>}
  </section>
}
