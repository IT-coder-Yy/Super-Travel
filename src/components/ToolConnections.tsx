import { useEffect, useRef, useState } from 'react'
import { checkTools, getToolStatus, type ToolProviderStatus } from '../api'

const callLabels = { ok: '成功', empty: '没有找到结果', quota: '额度不足', auth: '认证失败', error: '未完成' }

export function ToolConnections({ disabled }: { disabled: boolean }) {
  const [providers, setProviders] = useState<ToolProviderStatus[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const mounted = useRef(false)
  const checking = useRef<AbortController | null>(null)

  useEffect(() => {
    mounted.current = true
    const request = new AbortController()
    void getToolStatus(request.signal).then(result => {
      if (!request.signal.aborted) setProviders(result.providers)
    }).catch(() => {
      if (!request.signal.aborted) setError('暂时无法读取服务状态，请稍后检查工具服务。')
    }).finally(() => { if (!request.signal.aborted) setLoading(false) })
    return () => { mounted.current = false; request.abort(); checking.current?.abort() }
  }, [])

  async function check() {
    if (disabled || checking.current) return
    const request = new AbortController()
    checking.current = request
    setBusy(true); setNotice(''); setError('')
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
      if (mounted.current) setBusy(false)
    }
  }

  return <section className="tool-connections"><h3>外部服务</h3><p>地图与搜索服务由开发者统一配置，无需填写额外密钥。</p>
    {loading ? <p role="status">正在读取服务状态…</p> : providers.length > 0 ? <ul className="tool-providers" aria-label="外部服务状态">
      {providers.map(provider => <li key={provider.id}>
        <div className="tool-provider-heading"><strong>{provider.label}</strong><span>{provider.configured ? '已配置' : '未配置'}</span></div>
        <p>{provider.kind === 'public_search' && '公开搜索索引 · '}{provider.connection === 'connected' ? `已连接 · ${provider.toolCount} 个工具` : provider.connection === 'error' ? '连接未完成' : '尚未检查连接'}</p>
        {provider.message && <p>{provider.message}</p>}
        {provider.lastCall && <p className={['quota', 'auth', 'error'].includes(provider.lastCall.status) ? 'tool-call-warning' : ''}>最近调用：{callLabels[provider.lastCall.status]}{provider.lastCall.message ? `。${provider.lastCall.message}` : ''}</p>}
      </li>)}
    </ul> : !error && <p>尚无服务状态记录，检查后可查看连接情况。</p>}
    <button className="text-button" disabled={disabled || busy || loading} onClick={() => void check()}>检查工具服务</button>
    {busy && <p role="status">正在检查外部服务…</p>}{notice && <p role="status">{notice}</p>}{error && <p className="inline-error" role="alert">{error}</p>}
  </section>
}
