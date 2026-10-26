import { getBackendConnection } from './desktop'
import { invoke, isTauri } from '@tauri-apps/api/core'
import type { Conversation, ConversationSummary, RecoveryOption, WorkspaceData } from './types'

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const connection = await getBackendConnection()
  const response = await fetch(`${connection.baseUrl}/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${connection.token}`, ...init.headers },
    signal: init.signal ?? AbortSignal.timeout(15_000),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail?.message || (typeof body?.detail === 'string' ? body.detail : `本地服务请求失败（${response.status}）`))
  }
  return response.json() as Promise<T>
}

export const listConversations = () => request<ConversationSummary[]>('/conversations')
export const listRecoveryOptions = () => request<RecoveryOption[]>('/conversations/recovery')
export const recoverConversation = (id: string, token: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}/recover`, { method: 'POST', body: JSON.stringify({ token }) })
export const readConversation = (id: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}`)
export const sendAgentMessage = (id: string, content: string, messageId: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}/messages`, { method: 'POST', body: JSON.stringify({ message_id: messageId, content }) })
export const cancelAgent = (id: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
export type ModelSettings = { baseUrl: string; model: string; hasKey: boolean; storageAvailable: boolean; syncError?: string; toolsConfigured?: boolean }
export const getModelSettings = () => isTauri() ? invoke<ModelSettings>('get_model_settings') : request<ModelSettings>('/model/settings')
export async function saveModelSettings(settings: { baseUrl: string; model: string; apiKey: string }): Promise<ModelSettings> {
  if (isTauri()) return invoke<ModelSettings>('save_model_settings', settings)
  await request('/model/configure', { method: 'POST', body: JSON.stringify({ base_url: settings.baseUrl, model: settings.model, api_key: settings.apiKey || null }) })
  return getModelSettings()
}
export async function deleteModelKey(): Promise<ModelSettings> {
  if (isTauri()) return invoke<ModelSettings>('delete_model_key')
  const current = await getModelSettings()
  await request('/model/configure', { method: 'POST', body: JSON.stringify({ base_url: current.baseUrl, model: current.model, api_key: '' }) })
  return getModelSettings()
}
export const checkModel = () => request<{ connected: boolean }>('/model/check', { method: 'POST', signal: AbortSignal.timeout(75_000) })
export type ToolProviderStatus = {
  id: string; label: string; kind: 'mcp' | 'api' | 'public_search'; configured: boolean
  connection: 'unchecked' | 'connected' | 'error'; toolCount: number; message: string
  lastCall: { status: 'ok' | 'empty' | 'quota' | 'auth' | 'error'; message: string } | null
}
export const getToolStatus = (signal?: AbortSignal) => request<{ providers: ToolProviderStatus[] }>('/tools/status', { signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15_000)]) : undefined })
export const checkTools = (signal?: AbortSignal) => request<{ connected: boolean; toolCount: number; providers?: ToolProviderStatus[] }>('/tools/check', { method: 'POST', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(45_000)]) : AbortSignal.timeout(45_000) })
export const createConversation = (title: string, data: WorkspaceData) => request<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({ title, data }) })
export const saveConversation = (current: ConversationSummary, data: WorkspaceData) => request<Conversation>(`/conversations/${encodeURIComponent(current.id)}`, {
  method: 'PUT', body: JSON.stringify({ title: current.title, data, expected_revision: current.revision }),
})
