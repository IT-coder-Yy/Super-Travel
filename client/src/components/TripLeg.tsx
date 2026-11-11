import type { Activity, TripRoute } from '../types'
import { routeDetails, transitFare, transitSegment } from '../tripDetails'

export function TripLeg({ previous, activity, mode }: { previous?: Activity; activity: Activity; mode?: TripRoute['mode'] }) {
  const leg = routeDetails(previous, activity, mode)
  if (!leg) return null
  const transit = leg.transit
  return <div className={`trip-leg ${leg.conflict ? 'trip-leg--conflict' : ''}`} title={leg.detail}>
    <span>上一站至此 · {leg.text}</span>{leg.conflict && <span>{leg.conflict}</span>}
    {transit !== undefined ? <details className="transit-details">
      <summary>公交方案<span>{transitFare(transit)}</span></summary>
      {transit ? <>
        <p>{transit.transferCount == null ? '换乘次数待确认' : `换乘 ${transit.transferCount} 次（按乘车段计算）`}</p>
        {transit.segments.length ? <ol>{transit.segments.map((segment, index) => <li key={index}>{transitSegment(segment)}</li>)}</ol> : <p>公交分段详情待确认</p>}
      </> : <p>公交详情未查询；下次重新规划或更新路线时补充。</p>}
      <p className="trip-leg-note">{leg.detail}</p>
      <p className="trip-leg-note">供应商首方案，出发前请确认。交通参考价不计入活动费用小计。</p>
    </details> : <span className="trip-leg-note">{leg.detail}</span>}
  </div>
}
