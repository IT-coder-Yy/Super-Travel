import { useEffect, useRef, useState } from 'react'
import { checkTools, getToolSettings, getToolStatus, saveToolSettings, type ToolProviderStatus, type ToolSettings } from '../api'

const callLabels = { ok: '成功', empty: '没有找到结果', quota: '额度不足', auth: '认证失败', error: '未完成' }

export function ToolConnections({ disabled }: { disabled: boolean }) {
  const [providers, setProviders] = useState<ToolProviderStatus[]>([])
  const [settings, setSettings] = useState<ToolSettings | null>(null)
  const [baseUrl, setBaseUrl] = useState('')
  const [settingsError, setSettingsError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'saving' | 'checking' | null>(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const mounted = useRef(false)
  const checking = useRef<AbortController | null>(null)

  useEffect(() => {
    mounted.current = true
    const preferences = new AbortController()
    void getToolSettings(preferences.signal).then(value => {
      if (!preferences.signal.aborted) { setSettings(value); setBaseUrl(value.baseUrl); setSettingsError(value.error || '') }
    }).catch(reason => {
      if (!preferences.signal.aborted) setSettingsError(reason instanceof Error ? reason.message : '无法读取工具服务地址，请重新打开设置。')
    })
    const request = new AbortController()
    checking.current = request
    void getToolStatus(request.signal).then(result => {
      if (!request.signal.aborted) setProviders(result.providers)
    }).catch(() => {
      if (!request.signal.aborted) setError('暂时无法读取工具服务状态。请启动本机工具服务，或检查服务地址和网络。')
    }).finally(() => {
      if (checking.current === request) checking.current = null
      if (!request.signal.aborted) setLoading(false)
    })
    return () => { mounted.current = false; preferences.abort(); request.abort(); checking.current?.abort() }
  }, [])

  async function save() {
    if (disabled || busy || !settings || !baseUrl.trim()) return
    // A late response from the previous address must not replace the newly saved status.
    checking.current?.abort()
    const request = new AbortController()
    checking.current = request
    setBusy('saving'); setLoading(false); setNotice(''); setSettingsError(''); setError('')
    try {
      const value = await saveToolSettings(baseUrl.trim(), request.signal)
      if (!mounted.current || request.signal.aborted) return
      setSettings(value); setBaseUrl(value.baseUrl); setProviders([])
      setSettingsError(value.error || '')
      setNotice('工具服务地址已保存，立即生效。可检查连接。')
    } catch (reason) {
      if (mounted.current && !request.signal.aborted) setSettingsError(reason instanceof Error ? reason.message : '保存失败，请重试。')
    } finally {
      if (checking.current === request) checking.current = null
      if (mounted.current) setBusy(null)
    }
  }

  async function check() {
    if (disabled || busy || checking.current) return
    const request = new AbortController()
    checking.current = request
    setBusy('checking'); setNotice(''); setError('')
    try {
      const result = await checkTools(request.signal)
      // Older backends return only the total; keep their successful check usable.
      const status = result.providers ? { providers: result.providers } : await getToolStatus(request.signal).catch(() => null)
      if (!mounted.current || request.signal.aborted) return
      if (status) setProviders(status.providers)
      setNotice(result.connected ? `工具服务已连接，可用工具 ${result.toolCount} 个。` : '当前没有可用的工具服务，请查看各项状态。')
    } catch (reason) {
      if (mounted.current && !request.signal.aborted) setError(reason instanceof Error ? reason.message : '检查未完成，请稍后重试。')
    } finally {
      checking.current = null
      if (mounted.current) setBusy(null)
    }
  }

  return <section className="tool-connections"><h3>外部服务</h3><p>地图与搜索服务由开发者统一配置，无需填写额外密钥。</p>
    <fieldset className="model-fields" disabled={disabled || !!busy || !settings}>
      <label className="field-label">工具服务地址<input type="url" value={baseUrl} onChange={event => setBaseUrl(event.target.value)} placeholder="http://127.0.0.1:8080" autoComplete="off" spellCheck={false} /></label>
      <p className="muted-note">开发时连接本机工具服务；上线后可改为 HTTPS 地址。保存立即生效，并中断正在执行的任务。{settings && settings.mode !== 'shared' && '当前使用开发环境的工具连接，保存后切换到此地址。'}</p>
      <div className="button-row"><button className="secondary-button" disabled={!baseUrl.trim()} onClick={() => void save()}>{busy === 'saving' ? '正在保存…' : '保存工具服务地址'}</button></div>
    </fieldset>
    {settingsError && <p className="inline-error" role="alert">{settingsError}</p>}
    {loading ? <p role="status">正在读取服务状态…</p> : providers.length > 0 ? <ul className="tool-providers" aria-label="外部服务状态">
      {providers.map(provider => <li key={provider.id}>
        <div className="tool-provider-heading"><strong>{provider.label}</strong><span>{provider.configured ? '已配置' : '未配置'}</span></div>
        <p>{provider.kind === 'public_search' && '公开搜索索引 · '}{provider.connection === 'connected' ? `已连接 · ${provider.toolCount} 个工具` : provider.connection === 'error' ? '连接未完成' : '尚未检查连接'}</p>
        {provider.message && <p>{provider.message}</p>}
        {provider.lastCall && <p className={['quota', 'auth', 'error'].includes(provider.lastCall.status) ? 'tool-call-warning' : ''}>最近调用：{callLabels[provider.lastCall.status]}{provider.lastCall.message ? `。${provider.lastCall.message}` : ''}</p>}
      </li>)}
    </ul> : !error && <p>尚无服务状态记录，检查后可查看连接情况。</p>}
    <button className="text-button" disabled={disabled || !!busy || loading} onClick={() => void check()}>检查工具服务</button>
    {busy === 'checking' && <p role="status">正在检查外部服务…</p>}{notice && <p role="status">{notice}</p>}{error && <p className="inline-error" role="alert">{error}</p>}
  </section>
}
