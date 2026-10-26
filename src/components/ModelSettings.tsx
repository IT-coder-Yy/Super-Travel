import { useEffect, useState } from 'react'
import { isTauri } from '@tauri-apps/api/core'
import { checkModel, checkTools, deleteModelKey, getModelSettings, saveModelSettings, type ModelSettings as Settings } from '../api'

export function ModelSettings({ disabled }: { disabled: boolean }) {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const apply = (value: Settings) => { setSettings(value); setBaseUrl(value.baseUrl); setModel(value.model); setApiKey(''); setError(value.syncError || '') }
  useEffect(() => { let disposed = false; void getModelSettings().then(value => { if (!disposed) apply(value) }).catch(reason => { if (!disposed) setError(String(reason)) }); return () => { disposed = true } }, [])
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await action() } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setBusy(false) }
  }
  return <section><h3>模型与工具</h3><p>规划 Agent 负责对话、澄清和计划；执行 Agent 调用开发者提供的工具。模型调用使用你的 API Key。</p>
    <fieldset className="model-fields" disabled={disabled || busy || !settings}>
      <label className="field-label">模型服务地址<input type="url" value={baseUrl} onChange={event => setBaseUrl(event.target.value)} placeholder="https://…/v1" autoComplete="off" /></label>
      <label className="field-label">模型名称<input value={model} onChange={event => setModel(event.target.value)} placeholder="填写服务中可用的模型名" autoComplete="off" /></label>
      <label className="field-label">模型 API Key<input type="password" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder={settings?.hasKey ? '已配置；地址不变时留空保留' : '填写你的 API Key'} autoComplete="new-password" /></label>
      <p className="muted-note">{isTauri() ? settings?.storageAvailable ? '密钥保存在操作系统凭据库，不写入会话 JSON。保存设置会中断当前任务。' : '系统凭据库暂不可用，请解锁或启用凭据服务后重试。' : '浏览器开发模式只在本次后端运行期间保留密钥，重启后需重新配置。'}</p>
      <div className="button-row"><button className="secondary-button" disabled={!baseUrl.trim() || !model.trim() || isTauri() && !settings?.storageAvailable} onClick={() => void perform(async () => { apply(await saveModelSettings({ baseUrl: baseUrl.trim(), model: model.trim(), apiKey: apiKey.trim() })); setNotice('模型配置已保存。') })}>保存模型设置</button><button className="text-button" disabled={!settings?.hasKey} onClick={() => void perform(async () => { await checkModel(); setNotice('模型连接成功。') })}>测试模型连接</button><button className="text-button" disabled={!settings?.hasKey} onClick={() => void perform(async () => { apply(await deleteModelKey()); setNotice('API Key 已删除。') })}>删除 Key</button></div>
      <button className="text-button" onClick={() => void perform(async () => { const result = await checkTools(); setNotice(`工具服务已连接，可用工具 ${result.toolCount} 个。`) })}>检查工具服务</button>
    </fieldset>
    {busy && <p role="status">正在处理…</p>}{notice && <p role="status">{notice}</p>}{error && <p className="inline-error" role="alert">{error}</p>}
  </section>
}
