import type { Activity, TripDay, TripRoute } from './types'

const modes = { walking: '步行', driving: '驾车', transit: '公共交通' }
const minutes = (time: string) => Number(time.slice(0, 2)) * 60 + Number(time.slice(3))
export const formatCost = (amountFen: number) => `¥${(amountFen / 100).toFixed(2)}`

export function routeDetails(previous: Activity | undefined, activity: Activity, mode?: TripRoute['mode']) {
  if (!previous) return null
  const route = activity.incomingRoute
  if (!route || route.fromPlaceId !== previous.place?.id || route.toPlaceId !== activity.place?.id || mode && route.mode !== mode) {
    return { text: '接驳耗时待查询', conflict: '', detail: '应用修改时查询；也可请旅伴重新规划。' }
  }
  if (route.status !== 'verified' || route.durationMinutes == null) {
    return { text: `${modes[route.mode]}耗时未知`, conflict: '', detail: route.reason || '暂未查到可用路线，请出发前确认。' }
  }
  const age = route.queriedAt ? Date.now() - Date.parse(route.queriedAt) : NaN
  if (!Number.isFinite(age) || age < 0 || age > 24 * 60 * 60 * 1000) {
    return { text: '接驳耗时已过期，待重新查询', conflict: '', detail: '应用修改时重新查询，不用过期数据判断时间冲突。' }
  }
  const gap = minutes(activity.time) - minutes(previous.time) - Number(previous.duration.match(/^约 (\d+) 分钟$/)?.[1])
  const conflict = Number.isFinite(gap) && gap < route.durationMinutes
    ? `接驳时间不足：仅留 ${Math.max(0, gap)} 分钟，${modes[route.mode]}需约 ${route.durationMinutes} 分钟。` : ''
  const queried = route.queriedAt ? new Date(route.queriedAt).toLocaleString('zh-CN') : ''
  return { text: `${modes[route.mode]}约 ${route.durationMinutes} 分钟${route.distanceMeters == null ? '' : ` · ${(route.distanceMeters / 1000).toFixed(1)} 公里`}`,
    conflict, detail: `${route.provider === 'baidu' ? '百度地图' : route.provider === 'amap' ? '高德地图' : '地图查询'}${queried ? ` · ${queried}` : ''}；耗时为查询估计。` }
}

export function clearInvalidRoutes(days: TripDay[]) {
  for (const day of days) day.activities.forEach((activity, index) => {
    if (activity.incomingRoute && (activity.incomingRoute.fromPlaceId !== day.activities[index - 1]?.place?.id || activity.incomingRoute.toPlaceId !== activity.place?.id)) delete activity.incomingRoute
  })
}

export function knownCosts(days: TripDay[]) {
  const activities = days.flatMap(day => day.activities)
  return { amountFen: activities.reduce((sum, activity) => sum + (activity.cost?.amountFen ?? 0), 0), unknown: activities.filter(activity => activity.cost == null).length }
}
