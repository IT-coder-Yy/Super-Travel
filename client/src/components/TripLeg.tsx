import type { Activity, TripRoute } from '../types'
import { routeDetails } from '../tripDetails'

export function TripLeg({ previous, activity, mode }: { previous?: Activity; activity: Activity; mode?: TripRoute['mode'] }) {
  const leg = routeDetails(previous, activity, mode)
  if (!leg) return null
  return <span className={`trip-leg ${leg.conflict ? 'trip-leg--conflict' : ''}`} title={leg.detail}>
    <span>上一站至此 · {leg.text}</span>{leg.conflict && <span>{leg.conflict}</span>}
    {!activity.incomingRoute || activity.incomingRoute.status === 'unknown' ? <span className="trip-leg-note">{leg.detail}</span> : null}
  </span>
}
