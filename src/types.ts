export type Place = { id: string; name: string; address: string; latitude: number; longitude: number; coordinateSystem: 'wgs84'; provider: 'baidu' | 'amap'; sourceLocation?: { latitude: number; longitude: number; coordinateSystem: 'bd09ll' | 'gcj02' } | null }
export type TripMeta = { destination: string; summary: string; generatedAt: string; transportMode?: 'walking' | 'driving' | 'transit' | null }
export type TripRoute = { fromPlaceId: string; toPlaceId: string; mode: 'walking' | 'driving' | 'transit'; status: 'verified' | 'unknown'; durationMinutes?: number | null; distanceMeters?: number | null; provider?: 'baidu' | 'amap' | null; queriedAt?: string | null; reason?: string | null }
export type Activity = { id: string; title: string; time: string; duration: string; description: string; x: number; y: number; place?: Place; incomingRoute?: TripRoute | null; cost?: { amountFen: number; currency: 'CNY'; source: 'user' } | null }
export type TripDay = { day: number; title: string; subtitle: string; activities: Activity[] }
export type Message = { id: string; role: 'user' | 'assistant'; content: string }
export type AgentState = { persisted?: boolean; run_id?: string | null; status: 'idle' | 'running' | 'replanning' | 'completed' | 'failed' | 'cancelled' | 'needs_input' | 'interrupted'; plan?: string[]; results?: { step: string; content: string }[]; content?: string; error?: string | null; tool_calls?: number }
export type WorkspaceData = { schema: 1; days: TripDay[]; selectedDay: number; messages: Message[]; favorites: string[]; draft: TripDay[] | null; trip?: TripMeta; draftTrip?: TripMeta | null; previousTrip?: { days: TripDay[]; trip: TripMeta } | null; agent?: AgentState; agentPending?: boolean }
export type ConversationSummary = { id: string; title: string; revision: number; updated_at: string }
export type Conversation = ConversationSummary & { data: WorkspaceData }
export type RecoveryOption = { id: string; title: string; message: string; backup_updated_at: string | null; token: string | null }

export function createWorkspace(): WorkspaceData {
  return { schema: 1, selectedDay: 1, favorites: [], messages: [], draft: null, days: [] }
}

export function createExample(): WorkspaceData {
  return { schema: 1, selectedDay: 1, favorites: [], messages: [], draft: null, days: [
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

function validRoute(route: TripRoute): boolean {
  return typeof route.fromPlaceId === 'string' && typeof route.toPlaceId === 'string' &&
    ['walking', 'driving', 'transit'].includes(route.mode) && ['verified', 'unknown'].includes(route.status) &&
    (route.durationMinutes == null || Number.isSafeInteger(route.durationMinutes) && route.durationMinutes >= 0) &&
    (route.distanceMeters == null || Number.isSafeInteger(route.distanceMeters) && route.distanceMeters >= 0) &&
    (route.provider == null || ['baidu', 'amap'].includes(route.provider)) &&
    (route.queriedAt == null || typeof route.queriedAt === 'string') && (route.reason == null || typeof route.reason === 'string') &&
    (route.status !== 'verified' || route.durationMinutes != null)
}

export function isWorkspaceData(value: unknown): value is WorkspaceData {
  if (!value || typeof value !== 'object') return false
  const data = value as Partial<WorkspaceData>
  return data.schema === 1 && validDays(data.days) && typeof data.selectedDay === 'number' &&
    (data.trip === undefined || validTrip(data.trip)) && (data.draftTrip == null || validTrip(data.draftTrip)) &&
    (data.previousTrip == null || validTrip(data.previousTrip.trip) && validDays(data.previousTrip.days)) &&
    (data.agentPending === undefined || typeof data.agentPending === 'boolean') &&
    (data.agent === undefined || !!data.agent && typeof data.agent === 'object' && ['idle', 'running', 'replanning', 'completed', 'failed', 'cancelled', 'needs_input', 'interrupted'].includes(data.agent.status) && (data.agent.plan === undefined || Array.isArray(data.agent.plan) && data.agent.plan.every(item => typeof item === 'string')) && (data.agent.content === undefined || typeof data.agent.content === 'string') && (data.agent.error == null || typeof data.agent.error === 'string')) &&
    Array.isArray(data.favorites) && data.favorites.every(item => typeof item === 'string') &&
    Array.isArray(data.messages) && data.messages.every(message => message && typeof message.id === 'string' && ['user', 'assistant'].includes(message.role) && typeof message.content === 'string') &&
    (data.draft === null || validDays(data.draft))
}
