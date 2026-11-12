import type { Activity, TripRoute } from '../types'
import { formatCost, routeDetails, transitFare, transitSegment } from '../tripDetails'

export function TripLeg({ previous, activity, mode }: { previous?: Activity; activity: Activity; mode?: TripRoute['mode'] }) {
  const leg = routeDetails(previous, activity, mode)
  if (!leg) return null
  const { transit, taxiEstimates } = leg
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
    {taxiEstimates !== undefined && <details className="taxi-details">
      <summary>出租车参考价<span>{taxiEstimates?.length === 1 ? `约 ${formatCost(taxiEstimates[0].amountFen)}` : taxiEstimates?.length ? `${taxiEstimates.length} 项估算` : '暂无估算'}{taxiEstimates?.length ? '（仅供参考，以实际结果为准）' : ''}</span></summary>
      {taxiEstimates?.length ? <ol>{taxiEstimates.map((estimate, index) => <li key={index}>
        {estimate.label || '时段未注明'}：约 {formatCost(estimate.amountFen)}
        <p>{estimate.note || '供应商未提供计价条件，请出发前确认。'}</p>
      </li>)}</ol> : <p>{taxiEstimates == null ? '出租车参考价未查询；下次重新规划或更新路线时补充。' : '本次查询暂无可用出租车估算。'}</p>}
      <p className="trip-leg-note">{leg.detail}</p>
      <p className="trip-leg-note">出租车为另一种出行选择，估算不是订单报价，不与公交票价相加，也不计入活动费用小计。</p>
    </details>}
  </div>
}
