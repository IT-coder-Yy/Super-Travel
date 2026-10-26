export type Activity = { id: string; title: string; time: string; duration: string; description: string; x: number; y: number }
export type TripDay = { day: number; title: string; subtitle: string; activities: Activity[] }
export type Message = { id: string; role: 'user' | 'assistant'; content: string }
export type WorkspaceData = { schema: 1; days: TripDay[]; selectedDay: number; messages: Message[]; favorites: string[]; draft: TripDay[] | null }
export type ConversationSummary = { id: string; title: string; revision: number; updated_at: string }
export type Conversation = ConversationSummary & { data: WorkspaceData }
export type RecoveryOption = { id: string; title: string; message: string; backup_updated_at: string | null; token: string | null }

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

function validDays(value: unknown): value is TripDay[] {
  return Array.isArray(value) && value.length > 0 && value.every(day => day && Number.isInteger(day.day) &&
    typeof day.title === 'string' && typeof day.subtitle === 'string' && Array.isArray(day.activities) &&
    day.activities.every((activity: Activity) => activity && typeof activity.id === 'string' && typeof activity.title === 'string' &&
      typeof activity.time === 'string' && typeof activity.duration === 'string' && typeof activity.description === 'string' &&
      Number.isFinite(activity.x) && Number.isFinite(activity.y)))
}

export function isWorkspaceData(value: unknown): value is WorkspaceData {
  if (!value || typeof value !== 'object') return false
  const data = value as Partial<WorkspaceData>
  return data.schema === 1 && validDays(data.days) && typeof data.selectedDay === 'number' &&
    Array.isArray(data.favorites) && data.favorites.every(item => typeof item === 'string') &&
    Array.isArray(data.messages) && data.messages.every(message => message && typeof message.id === 'string' && ['user', 'assistant'].includes(message.role) && typeof message.content === 'string') &&
    (data.draft === null || validDays(data.draft))
}
