export type Place = { id: string; name: string; address: string; latitude: number; longitude: number; coordinateSystem: 'wgs84'; provider: 'baidu' | 'amap'; sourceLocation?: { latitude: number; longitude: number; coordinateSystem: 'bd09ll' | 'gcj02' } | null }
export type PlaceCategory = '景点' | '餐饮' | '购物'
export type PlaceSearchResult = { selection_id: string; name: string; address: string; provider: Place['provider'] }
export type PlaceSelection = { selection_id: string; day: number; activity_id: string | null; time: string; duration_minutes: number; description: string }
export type ActivityMove = { activity_id: string; target_day: number; target_index: number; time: string }
export type ActivityDeletePreview = { revision: number; activity_id: string; title: string; day: number; has_note: boolean; is_favorite: boolean; has_map_point: boolean; affected_routes: number }
export type TripMeta = { destination: string; summary: string; generatedAt: string; transportMode?: 'walking' | 'driving' | 'transit' | null }
export type TransitSegment = { mode: 'walking' | 'bus' | 'subway' | 'other'; lineName?: string | null; departureStop?: string | null; arrivalStop?: string | null }
export type TransitDetails = { segments: TransitSegment[]; transferCount?: number | null; fare?: { amountFen: number; currency: 'CNY' } | null }
export type TaxiEstimate = { amountFen: number; currency: 'CNY'; kind: 'estimate'; label?: string | null; note?: string | null }
export type TripRoute = { fromPlaceId: string; toPlaceId: string; mode: 'walking' | 'driving' | 'transit'; status: 'verified' | 'unknown'; durationMinutes?: number | null; distanceMeters?: number | null; provider?: 'baidu' | 'amap' | null; queriedAt?: string | null; reason?: string | null; transit?: TransitDetails | null; taxiEstimates?: TaxiEstimate[] | null }
export type Activity = { id: string; title: string; time: string; duration: string; description: string; x: number; y: number; place?: Place; incomingRoute?: TripRoute | null; cost?: { amountFen: number; currency: 'CNY'; source: 'user' } | null }
export type Candidate = Omit<Activity, 'time' | 'x' | 'y' | 'incomingRoute' | 'place'> & { place: Place; destination: string }
export type CandidatePlaceSelection = Pick<PlaceSelection, 'selection_id' | 'duration_minutes' | 'description'>
export type CandidateSlot = { option_id: string; earliest_start: string; latest_start: string; position: number; warnings: string[] }
export type CandidateSlots = { revision: number; options: CandidateSlot[]; reason?: string }
export type TripDay = { day: number; title: string; subtitle: string; activities: Activity[] }
export type Message = { id: string; role: 'user' | 'assistant'; content: string }
export type AgentState = { preference_summary?: string; persisted?: boolean; run_id?: string | null; status: 'idle' | 'running' | 'replanning' | 'completed' | 'failed' | 'cancelled' | 'needs_input' | 'interrupted'; plan?: string[]; results?: { step: string; content: string }[]; content?: string; error?: string | null; tool_calls?: number }
export type WorkspaceData = { schema: 1; days: TripDay[]; selectedDay: number; messages: Message[]; favorites: string[]; draft: TripDay[] | null; candidates?: Candidate[]; draftCandidates?: Candidate[] | null; trip?: TripMeta; draftTrip?: TripMeta | null; previousTrip?: { days: TripDay[]; trip: TripMeta; candidates?: Candidate[]; removedFavorites?: string[] } | null; agent?: AgentState; agentPending?: boolean }
export type ConversationSummary = { id: string; title: string; revision: number; updated_at: string }
export type Conversation = ConversationSummary & { data: WorkspaceData }
export type TravelPreferences = { schema: 1; enabled: boolean; budgetAmountFen: number | null; pace: 'relaxed' | 'balanced' | 'packed' | null; dietaryNotes: string; companionMode: 'solo' | 'couple' | 'family' | 'friends' | null }
export type TravelPreferencesResponse = { preferences: TravelPreferences; error?: string }
export type TripExportFormat = 'markdown' | 'pdf'
export type TripExportSnapshot = { revision: number; filename: string; markdown: string; pdfBase64?: string; previewText?: string }
export type RecoveryOption = { id: string; title: string; message: string; backup_updated_at: string | null; token: string | null }

export function createWorkspace(): WorkspaceData {
  return { schema: 1, selectedDay: 1, favorites: [], messages: [], draft: null, candidates: [], draftCandidates: null, days: [] }
}

export function createExample(): WorkspaceData {
  return { schema: 1, selectedDay: 1, favorites: [], messages: [], draft: null, candidates: [], draftCandidates: null, days: [
    { day: 1, title: '把今天，交给西湖。', subtitle: '沿着湖岸，慢慢发现杭州', activities: [
      { id: 'bridge', title: '断桥残雪', time: '09:30', duration: '约 1.5 小时', description: '从这里开始，沿着白堤漫步，感受西湖的诗意。', x: 1010, y: 235 },
      { id: 'lunch', title: '湖滨午餐', time: '12:00', duration: '约 1.5 小时', description: '在湖滨寻一处小馆，尝尝杭帮菜，感受烟火气。', x: 1080, y: 430 },
      { id: 'lotus', title: '曲院风荷', time: '15:00', duration: '约 2 小时', description: '漫步荷塘，欣赏古典园林的雅致，把时间留给自己。', x: 725, y: 200 },
    ] },
    { day: 2, title: '去山里，听一阵风。', subtitle: '茶香与山林之间的闲适一天', activities: [
      { id: 'lingyin', title: '灵隐寺', time: '09:00', duration: '约 2 小时', description: '沿着山间小径，感受古寺与树林的安静。', x: 470, y: 330 },
      { id: 'tea', title: '龙井问茶', time: '12:30', duration: '约 2 小时', description: '在茶园边坐一坐，让一杯茶慢下来。', x: 560, y: 630 },
      { id: 'garden', title: '杭州植物园', time: '15:30', duration: '约 1.5 小时', description: '寻找树荫，走一段不赶时间的路。', x: 635, y: 380 },
    ] },
    { day: 3, title: '慢一点，再告别。', subtitle: '最后一天，把美好带回家', activities: [
      { id: 'pagoda', title: '雷峰塔', time: '09:30', duration: '约 1.5 小时', description: '从湖的另一面，再看一次山水相依。', x: 860, y: 700 },
      { id: 'fish', title: '花港观鱼', time: '12:00', duration: '约 1 小时', description: '在水边停留，和这段旅程轻轻道别。', x: 720, y: 590 },
      { id: 'station', title: '杭州站返程', time: '15:30', duration: '时间待确认', description: '为返程留出余量，实际车次请查询后确认。', x: 1270, y: 300 },
    ] },
  ] }
}

export const hasTripDraft = (data: WorkspaceData) => data.draft != null || data.draftCandidates != null
export const currentCandidates = (data: WorkspaceData): Candidate[] => hasTripDraft(data) ? data.draftCandidates ?? data.candidates ?? [] : data.candidates ?? []
export const beginTripDraft = (data: WorkspaceData): WorkspaceData => ({ ...data, draft: data.draft ?? structuredClone(data.days), draftTrip: data.draftTrip ?? data.trip ?? null, draftCandidates: data.draftCandidates ?? structuredClone(data.candidates ?? []) })

function validPlace(value: unknown): value is Place {
  if (!value || typeof value !== 'object') return false
  const place = value as Partial<Place>
  return typeof place.id === 'string' && typeof place.name === 'string' && typeof place.address === 'string' &&
    Number.isFinite(place.latitude) && Math.abs(place.latitude!) <= 90 && Number.isFinite(place.longitude) && Math.abs(place.longitude!) <= 180 &&
    place.coordinateSystem === 'wgs84' && ['baidu', 'amap'].includes(place.provider ?? '')
}

function validTrip(value: unknown): value is TripMeta {
  if (!value || typeof value !== 'object') return false
  const trip = value as Partial<TripMeta>
  return typeof trip.destination === 'string' && typeof trip.summary === 'string' && typeof trip.generatedAt === 'string'
}

function validDays(value: unknown): value is TripDay[] {
  return Array.isArray(value) && value.length <= 7 && value.every(day => day && Number.isInteger(day.day) && day.day >= 1 && day.day <= 7 &&
    typeof day.title === 'string' && typeof day.subtitle === 'string' && Array.isArray(day.activities) &&
    day.activities.every((activity: Activity) => activity && typeof activity.id === 'string' && typeof activity.title === 'string' &&
      typeof activity.time === 'string' && typeof activity.duration === 'string' && typeof activity.description === 'string' &&
      Number.isFinite(activity.x) && Number.isFinite(activity.y) && (activity.place === undefined || validPlace(activity.place)) &&
      (activity.cost == null || Number.isSafeInteger(activity.cost.amountFen) && activity.cost.amountFen >= 0 && activity.cost.amountFen <= 100000000 && activity.cost.currency === 'CNY' && activity.cost.source === 'user') &&
      (activity.incomingRoute == null || validRoute(activity.incomingRoute))))
}

function validTransit(value: TransitDetails): boolean {
  const text = (item: unknown) => item == null || typeof item === 'string' && item.trim().length > 0 && item.length <= 200
  return Array.isArray(value.segments) && value.segments.length <= 64 && value.segments.every(segment => segment &&
    ['walking', 'bus', 'subway', 'other'].includes(segment.mode) && text(segment.lineName) && text(segment.departureStop) && text(segment.arrivalStop)) &&
    (value.transferCount == null || Number.isSafeInteger(value.transferCount) && value.transferCount >= 0 && value.transferCount <= 63) &&
    (value.fare == null || Number.isSafeInteger(value.fare.amountFen) && value.fare.amountFen >= 0 && value.fare.amountFen <= 100000000 && value.fare.currency === 'CNY')
}

function validTaxi(value: TaxiEstimate[]): boolean {
  const text = (item: unknown, limit: number) => item == null || typeof item === 'string' && item.trim().length > 0 && item.length <= limit
  return Array.isArray(value) && value.length <= 8 && value.every(item => item && Number.isSafeInteger(item.amountFen) &&
    item.amountFen >= 0 && item.amountFen <= 100000000 && item.currency === 'CNY' && item.kind === 'estimate' && text(item.label, 100) && text(item.note, 500))
}

function validRoute(route: TripRoute): boolean {
  return typeof route.fromPlaceId === 'string' && typeof route.toPlaceId === 'string' &&
    ['walking', 'driving', 'transit'].includes(route.mode) && ['verified', 'unknown'].includes(route.status) &&
    (route.durationMinutes == null || Number.isSafeInteger(route.durationMinutes) && route.durationMinutes >= 0) &&
    (route.distanceMeters == null || Number.isSafeInteger(route.distanceMeters) && route.distanceMeters >= 0) &&
    (route.provider == null || ['baidu', 'amap'].includes(route.provider)) &&
    (route.queriedAt == null || typeof route.queriedAt === 'string') && (route.reason == null || typeof route.reason === 'string') &&
    (route.status !== 'verified' || route.durationMinutes != null) &&
    (route.transit == null || route.mode === 'transit' && route.status === 'verified' && validTransit(route.transit)) &&
    (route.taxiEstimates == null || route.mode !== 'walking' && route.status === 'verified' && validTaxi(route.taxiEstimates))
}

function validCandidates(value: unknown): value is Candidate[] {
  return Array.isArray(value) && value.every(candidate => candidate && typeof candidate.id === 'string' &&
    typeof candidate.title === 'string' && typeof candidate.duration === 'string' && typeof candidate.description === 'string' && typeof candidate.destination === 'string' && validPlace(candidate.place) &&
    candidate.time === undefined && candidate.x === undefined && candidate.y === undefined && candidate.incomingRoute === undefined &&
    (candidate.cost == null || Number.isSafeInteger(candidate.cost.amountFen) && candidate.cost.amountFen >= 0 && candidate.cost.amountFen <= 100000000 && candidate.cost.currency === 'CNY' && candidate.cost.source === 'user'))
}

export function isWorkspaceData(value: unknown): value is WorkspaceData {
  if (!value || typeof value !== 'object') return false
  const data = value as Partial<WorkspaceData>
  return data.schema === 1 && validDays(data.days) && typeof data.selectedDay === 'number' &&
    (data.trip === undefined || validTrip(data.trip)) && (data.draftTrip == null || validTrip(data.draftTrip)) &&
    (data.previousTrip == null || validTrip(data.previousTrip.trip) && validDays(data.previousTrip.days) && (data.previousTrip.candidates === undefined || validCandidates(data.previousTrip.candidates)) && (data.previousTrip.removedFavorites === undefined || Array.isArray(data.previousTrip.removedFavorites) && data.previousTrip.removedFavorites.every(id => typeof id === 'string'))) &&
    (data.agentPending === undefined || typeof data.agentPending === 'boolean') &&
    (data.agent === undefined || !!data.agent && typeof data.agent === 'object' && ['idle', 'running', 'replanning', 'completed', 'failed', 'cancelled', 'needs_input', 'interrupted'].includes(data.agent.status) && (data.agent.plan === undefined || Array.isArray(data.agent.plan) && data.agent.plan.every(item => typeof item === 'string')) && (data.agent.content === undefined || typeof data.agent.content === 'string') && (data.agent.error == null || typeof data.agent.error === 'string') && (data.agent.preference_summary === undefined || typeof data.agent.preference_summary === 'string')) &&
    Array.isArray(data.favorites) && data.favorites.every(item => typeof item === 'string') &&
    Array.isArray(data.messages) && data.messages.every(message => message && typeof message.id === 'string' && ['user', 'assistant'].includes(message.role) && typeof message.content === 'string') &&
    (data.draft === null || validDays(data.draft)) && (data.candidates === undefined || validCandidates(data.candidates)) && (data.draftCandidates == null || validCandidates(data.draftCandidates))
}
