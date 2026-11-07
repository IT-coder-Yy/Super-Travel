import { getBackendConnection } from './desktop'
import { invoke, isTauri } from '@tauri-apps/api/core'
import type { ActivityDeletePreview, ActivityMove, CandidatePlaceSelection, CandidateSlots, Conversation, ConversationSummary, PlaceCategory, PlaceSearchResult, PlaceSelection, RecoveryOption, TravelPreferences, TravelPreferencesResponse, TripExportFormat, TripExportSnapshot, WorkspaceData } from './types'

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const connection = await getBackendConnection()
  const response = await fetch(`${connection.baseUrl}/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${connection.token}`, ...init.headers },
    signal: init.signal ?? AbortSignal.timeout(15_000),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new ApiError(body?.detail?.message || (typeof body?.detail === 'string' ? body.detail : `本地服务请求失败（${response.status}）`), response.status)
  }
  return response.json() as Promise<T>
}

export const listConversations = () => request<ConversationSummary[]>('/conversations')
export const listRecoveryOptions = () => request<RecoveryOption[]>('/conversations/recovery')
export const recoverConversation = (id: string, token: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}/recover`, { method: 'POST', body: JSON.stringify({ token }) })
export const readConversation = (id: string, signal?: AbortSignal) => request<Conversation>(`/conversations/${encodeURIComponent(id)}`, { signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15_000)]) : undefined })
export const sendAgentMessage = (id: string, content: string, messageId: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}/messages`, { method: 'POST', body: JSON.stringify({ message_id: messageId, content }) })
export const cancelAgent = (id: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
export const applyTripDraft = (current: ConversationSummary) => request<Conversation>(`/conversations/${encodeURIComponent(current.id)}/trip/apply`, { method: 'POST', signal: AbortSignal.timeout(45_000), body: JSON.stringify({ expected_revision: current.revision }) })
export const exportTrip = (current: ConversationSummary, signal: AbortSignal, format: TripExportFormat = 'markdown') => request<TripExportSnapshot>(`/conversations/${encodeURIComponent(current.id)}/trip/export`, { method: 'POST', signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]), body: JSON.stringify({ expected_revision: current.revision, format }) })
export const restorePreviousTrip = (current: ConversationSummary) => request<Conversation>(`/conversations/${encodeURIComponent(current.id)}/trip/restore`, { method: 'POST', body: JSON.stringify({ expected_revision: current.revision }) })
export const resolveTripPlace = (current: ConversationSummary, activityId: string, query: string) => request<Conversation>(`/conversations/${encodeURIComponent(current.id)}/resolve-place`, { method: 'POST', signal: AbortSignal.timeout(75_000), body: JSON.stringify({ expected_revision: current.revision, activity_id: activityId, query }) })
export const searchTripPlaces = (current: ConversationSummary, query: string, category: PlaceCategory | null, signal: AbortSignal) => request<{ results: PlaceSearchResult[]; revision: number }>(
  `/conversations/${encodeURIComponent(current.id)}/trip/places/search`, { method: 'POST', signal: AbortSignal.any([signal, AbortSignal.timeout(75_000)]), body: JSON.stringify({ expected_revision: current.revision, query, category }) })
export const selectTripPlace = (current: ConversationSummary, selection: PlaceSelection) => request<Conversation>(
  `/conversations/${encodeURIComponent(current.id)}/trip/places/select`, { method: 'POST', body: JSON.stringify({ expected_revision: current.revision, ...selection }) })
export const moveTripActivity = (current: ConversationSummary, move: ActivityMove) => request<Conversation>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/activities/move', { method: 'POST', body: JSON.stringify({ expected_revision: current.revision, ...move }) })
export const previewTripActivityDeletion = (current: ConversationSummary, activityId: string, signal: AbortSignal) => request<ActivityDeletePreview>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/activities/delete-preview', { method: 'POST', signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]), body: JSON.stringify({ expected_revision: current.revision, activity_id: activityId }) })
export const deleteTripActivity = (current: ConversationSummary, activityId: string) => request<Conversation>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/activities/delete', { method: 'POST', body: JSON.stringify({ expected_revision: current.revision, activity_id: activityId }) })
export const parkTripActivity = (current: ConversationSummary, activityId: string) => request<Conversation>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/candidates/park', { method: 'POST', body: JSON.stringify({ expected_revision: current.revision, activity_id: activityId }) })
export const addTripCandidate = (current: ConversationSummary, selection: CandidatePlaceSelection) => request<Conversation>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/candidates/from-place', { method: 'POST', body: JSON.stringify({ expected_revision: current.revision, ...selection }) })
export const deleteTripCandidate = (current: ConversationSummary, candidateId: string) => request<Conversation>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/candidates/delete', { method: 'POST', body: JSON.stringify({ expected_revision: current.revision, candidate_id: candidateId }) })
export const findCandidateSlots = (current: ConversationSummary, candidateId: string, day: number, durationMinutes: number, signal: AbortSignal) => request<CandidateSlots>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/candidates/slots', { method: 'POST', signal: AbortSignal.any([signal, AbortSignal.timeout(75_000)]), body: JSON.stringify({ expected_revision: current.revision, candidate_id: candidateId, day, duration_minutes: durationMinutes }) })
export const insertTripCandidate = (current: ConversationSummary, optionId: string, time: string) => request<Conversation>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/candidates/insert', { method: 'POST', signal: AbortSignal.timeout(75_000), body: JSON.stringify({ expected_revision: current.revision, option_id: optionId, time }) })
export const discardTripDraft = (current: ConversationSummary) => request<Conversation>(
  '/conversations/' + encodeURIComponent(current.id) + '/trip/discard', { method: 'POST', body: JSON.stringify({ expected_revision: current.revision }) })
export const getTravelPreferences = (signal: AbortSignal) => request<TravelPreferencesResponse>('/travel-preferences', { signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]) })
export const saveTravelPreferences = (preferences: TravelPreferences, signal: AbortSignal) => request<TravelPreferencesResponse>('/travel-preferences', { method: 'PUT', body: JSON.stringify(preferences), signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]) })
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
export type ToolSettings = { baseUrl: string; mode: 'shared' | 'direct' | 'mcp'; error?: string }
export const getToolSettings = (signal?: AbortSignal) => request<ToolSettings>('/tools/settings', { signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15_000)]) : undefined })
export const saveToolSettings = (baseUrl: string, signal?: AbortSignal) => request<ToolSettings>('/tools/configure', { method: 'POST', body: JSON.stringify({ base_url: baseUrl }), signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15_000)]) : undefined })
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
