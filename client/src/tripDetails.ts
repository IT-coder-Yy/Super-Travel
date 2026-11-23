import type { Activity, TripDay, TripRoute, TransitDetails, TransitSegment } from './types'

export function tripOverview(days: TripDay[]) {
  const entries = days.map(day => day.activities.length ? `第 ${day.day} 天 ${day.activities.length} 项活动` : `第 ${day.day} 天自由安排`)
  return `${days.length} 天行程；${entries.join('；')}。`
}

export const transportModes = { walking: '步行', driving: '驾车', transit: '公共交通' }
const minutes = (time: string) => Number(time.slice(0, 2)) * 60 + Number(time.slice(3))
export const formatCost = (amountFen: number) => `¥${(amountFen / 100).toFixed(2)}`

export function legTransportMode(activity: Activity, defaultMode?: TripRoute['mode'] | null) {
  return activity.incomingTransportOverride?.mode ?? defaultMode ?? 'walking'
}

export function routeDetails(previous: Activity | undefined, activity: Activity, defaultMode?: TripRoute['mode']) {
  if (!previous) return null
  const route = activity.incomingRoute
  if (!previous.place || !activity.place) return { text: '接驳耗时待查询', conflict: '', detail: '请先定位这段接驳的两个地点。' }
  if (!route || route.fromPlaceId !== previous.place.id || route.toPlaceId !== activity.place.id || route.mode !== legTransportMode(activity, defaultMode)
      || (route.provider && (route.provider !== previous.place.provider || route.provider !== activity.place.provider))) {
    return { text: '接驳耗时待查询', conflict: '', detail: '应用修改时查询；也可请旅伴重新规划。' }
  }
  if (route.status !== 'verified' || route.durationMinutes == null) {
    return { text: `${transportModes[route.mode]}耗时未知`, conflict: '', detail: route.reason || '暂未查到可用路线，请出发前确认。' }
  }
  const age = route.queriedAt ? Date.now() - Date.parse(route.queriedAt) : NaN
  if (!Number.isFinite(age) || age < 0 || age > 24 * 60 * 60 * 1000) {
    if (route.selection === 'user') return { text: '用户选用方案已过期', conflict: '请重新比较并选择公交方案。', detail: '旧方案保留在草稿中；重新选择后才能应用，不会自动换成首方案。' }
    return { text: '接驳耗时已过期，待重新查询', conflict: '', detail: '应用修改时重新查询，不用过期数据判断时间冲突。' }
  }
  const gap = minutes(activity.time) - minutes(previous.time) - Number(previous.duration.match(/^约 (\d+) 分钟$/)?.[1])
  const conflict = Number.isFinite(gap) && gap < route.durationMinutes
    ? `接驳时间不足：仅留 ${Math.max(0, gap)} 分钟，${transportModes[route.mode]}需约 ${route.durationMinutes} 分钟。` : ''
  const queried = route.queriedAt ? new Date(route.queriedAt).toLocaleString('zh-CN') : ''
  return { text: `${transportModes[route.mode]}约 ${route.durationMinutes} 分钟${route.distanceMeters == null ? '' : ` · ${(route.distanceMeters / 1000).toFixed(1)} 公里`}`,
    transit: route.mode === 'transit' ? route.transit ?? null : undefined,
    taxiEstimates: route.mode !== 'walking' ? route.taxiEstimates ?? null : undefined,
    conflict, detail: `${route.provider === 'baidu' ? '百度地图' : route.provider === 'amap' ? '高德地图' : '地图查询'}${queried ? ` · ${queried}` : ''}；耗时为查询估计。` }
}

export function firstRouteConflict(days: TripDay[], mode?: TripRoute['mode']) {
  for (const day of days) {
    for (const [index, activity] of day.activities.entries()) {
      const message = routeDetails(day.activities[index - 1], activity, mode)?.conflict
      if (message) return { activityId: activity.id, message: `第 ${day.day} 天 · 前往${activity.title}：${message}` }
    }
  }
}

export function clearInvalidRoutes(days: TripDay[], previousDays?: TripDay[]) {
  // Compare the full stored places independently of JSON property order.
  const placeFields = ['id', 'name', 'address', 'latitude', 'longitude', 'coordinateSystem', 'provider', 'sourceLocation']
  const context = (day: TripDay, index: number) => {
    const previous = day.activities[index - 1]
    return JSON.stringify([day.day, previous?.id, previous?.place, day.activities[index].place], placeFields)
  }
  let lostChoice = false
  const before = new Map(previousDays?.flatMap(day => day.activities.map((activity, index) => [activity.id, context(day, index)])))
  for (const day of days) day.activities.forEach((activity, index) => {
    const origin = day.activities[index - 1]?.place
    if (!origin || !activity.place || (previousDays && before.get(activity.id) !== context(day, index))) {
      if (activity.incomingTransportOverride || activity.incomingRoute?.selection === 'user') lostChoice = true
      delete activity.incomingTransportOverride
      delete activity.incomingRoute
    } else if (activity.incomingRoute && (activity.incomingRoute.fromPlaceId !== origin.id || activity.incomingRoute.toPlaceId !== activity.place.id)) {
      delete activity.incomingRoute
    }
  })
  return lostChoice
}

export function knownCosts(days: TripDay[]) {
  const activities = days.flatMap(day => day.activities)
  return { amountFen: activities.reduce((sum, activity) => sum + (activity.cost?.amountFen ?? 0), 0), unknown: activities.filter(activity => activity.cost == null).length }
}

export function transitFare(details: TransitDetails | null) {
  const fare = details?.fare
  return fare ? `参考总票价 ${formatCost(fare.amountFen)}（仅供参考，以实际结果为准）` : '参考总票价未知'
}

export function transitSegment(segment: TransitSegment) {
  if (segment.mode === 'walking') return '步行接驳'
  if (segment.mode === 'other') return '此段交通详情待确认'
  return `${segment.lineName || '线路待确认'}：${segment.departureStop || '上车站待确认'} 上车 → ${segment.arrivalStop || '下车站待确认'} 下车`
}
