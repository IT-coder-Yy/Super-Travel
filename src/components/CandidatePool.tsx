import { useEffect } from 'react'
import { CalendarPlus, Heart, MapPin, Plus, Trash2 } from 'lucide-react'
import { formatCost } from '../tripDetails'
import type { Candidate } from '../types'

type Props = {
  candidates: Candidate[]
  favorites: string[]
  disabled: boolean
  focusedId: string | null
  onSearch: () => void
  onArrange: (candidate: Candidate) => void
  onDelete: (candidate: Candidate) => void
  onFavorite: (id: string) => void
}

export function CandidatePool({ candidates, favorites, disabled, focusedId, onSearch, onArrange, onDelete, onFavorite }: Props) {
  useEffect(() => {
    if (!focusedId) return
    // Wait until the parent dialog has opened and finished its native autofocus.
    const frame = requestAnimationFrame(() => document.getElementById('candidate-' + focusedId)?.focus())
    return () => cancelAnimationFrame(frame)
  }, [focusedId])
  return <>
    <div className="candidate-toolbar"><p>想去、还没排进日程的地点。候选不占用时间，不计入行程费用；收藏仍单独表示喜欢。</p><button id="add-trip-candidate" className="secondary-button" disabled={disabled} onClick={onSearch}><Plus size={17} />搜索加入候选</button></div>
    {candidates.length === 0 ? <div className="empty-state candidate-empty"><MapPin size={30} /><h3>先把想去的地方留下</h3><p>搜索真实地点加入候选，或在编辑行程时把活动移入这里，稍后再安排。</p></div> : <ul className="candidate-list">{candidates.map(candidate => <li key={candidate.id} aria-label={candidate.title}>
      <div className="candidate-heading"><h3 id={'candidate-' + candidate.id} tabIndex={-1}>{candidate.title}</h3><button className="icon-button candidate-favorite" disabled={disabled} aria-label={(favorites.includes(candidate.id) ? '取消收藏' : '收藏') + candidate.title} aria-pressed={favorites.includes(candidate.id)} onClick={() => onFavorite(candidate.id)}><Heart size={20} /></button></div>
      <p className="candidate-address"><MapPin size={14} />{candidate.place.address || '服务未提供地址'}</p>
      <p className="candidate-source">{candidate.destination} · {candidate.place.provider === 'baidu' ? '百度地图' : '高德地图'} · 真实地点 · 开放与预约待确认</p>
      <p className="candidate-note">{candidate.description || '暂无备注'}</p>
      <p className="candidate-meta"><span>{candidate.duration}</span><span>{candidate.cost ? formatCost(candidate.cost.amountFen) + ' · 用户填写' : '活动费用未知'}</span></p>
      <div className="candidate-row-actions"><button id={'insert-trip-candidate-' + candidate.id} className="secondary-button" disabled={disabled} onClick={() => onArrange(candidate)}><CalendarPlus size={16} />排入草稿</button><button id={'delete-trip-candidate-' + candidate.id} className="text-button danger-text" disabled={disabled} onClick={() => onDelete(candidate)}><Trash2 size={15} />删除候选</button></div>
    </li>)}</ul>}
  </>
}
