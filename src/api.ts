import { getBackendConnection } from './desktop'
import type { Conversation, ConversationSummary, RecoveryOption, WorkspaceData } from './types'

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const connection = await getBackendConnection()
  const response = await fetch(`${connection.baseUrl}/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${connection.token}`, ...init.headers },
    signal: AbortSignal.timeout(15_000),
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
export const createConversation = (title: string, data: WorkspaceData) => request<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({ title, data }) })
export const saveConversation = (current: ConversationSummary, data: WorkspaceData) => request<Conversation>(`/conversations/${encodeURIComponent(current.id)}`, {
  method: 'PUT', body: JSON.stringify({ title: current.title, data, expected_revision: current.revision }),
})
