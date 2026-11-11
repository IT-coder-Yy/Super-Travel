import { useEffect, useRef, useState, type PointerEvent } from 'react'

type Drag = { activityId: string; x: number; y: number; moving: boolean; beforeId?: string | null }

export function useActivityDrag(scope: string, disabled: boolean, onDrop: (activityId: string, beforeId: string | null) => void) {
  const listRef = useRef<HTMLFieldSetElement>(null)
  const cancelRef = useRef<(() => void) | null>(null)
  const [drag, setDrag] = useState<Drag | null>(null)

  useEffect(() => () => cancelRef.current?.(), [scope, disabled])

  function start(event: PointerEvent<HTMLButtonElement>, activityId: string) {
    if (disabled || !event.isPrimary || event.button !== 0 || cancelRef.current) return
    const list = listRef.current
    const scroller = list?.closest('dialog')
    if (!list || !scroller) return
    const handle = event.currentTarget
    const pointerId = event.pointerId
    const startX = event.clientX, startY = event.clientY
    let x = startX, y = startY, moving = false, frame = 0
    let beforeId: string | null | undefined
    handle.focus()
    handle.setPointerCapture(pointerId)
    setDrag({ activityId, x, y, moving })

    function update(scroll = false) {
      if (!moving) return
      const bounds = scroller!.getBoundingClientRect()
      const top = scroller!.querySelector('.dialog-header')!.getBoundingClientRect().bottom
      const inside = x >= bounds.left && x <= bounds.right && y >= top && y <= bounds.bottom
      if (scroll && inside) scroller!.scrollTop += y < top + 36 ? -12 : y > bounds.bottom - 36 ? 12 : 0
      const rows = [...list!.querySelectorAll<HTMLElement>('[data-activity-id]')]
      const remaining = rows.filter(row => row.dataset.activityId !== activityId)
      const first = rows[0]?.getBoundingClientRect(), last = rows.at(-1)?.getBoundingClientRect()
      beforeId = undefined
      if (inside && first && last && x >= first.left && x <= first.right && y >= first.top && y <= last.bottom) {
        const next = remaining.find(row => { const box = row.getBoundingClientRect(); return y < box.top + box.height / 2 })
        const position = next ? remaining.indexOf(next) : remaining.length
        if (position !== rows.findIndex(row => row.dataset.activityId === activityId)) beforeId = next?.dataset.activityId ?? null
      }
      setDrag(previous => previous?.x === x && previous.y === y && previous.beforeId === beforeId && previous.moving === moving
        ? previous : { activityId, x, y, moving, beforeId })
    }
    function tick() { update(true); frame = requestAnimationFrame(tick) }
    function move(event: globalThis.PointerEvent) {
      if (event.pointerId !== pointerId) return
      x = event.clientX; y = event.clientY
      moving ||= Math.hypot(x - startX, y - startY) >= 8
      update()
    }
    function finish(apply = false) {
      cancelRef.current = null
      cancelAnimationFrame(frame)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', cancelPointer)
      window.removeEventListener('blur', cancel)
      window.removeEventListener('keydown', key, true)
      handle.removeEventListener('lostpointercapture', cancel)
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId)
      setDrag(null)
      if (apply && moving && beforeId !== undefined) onDrop(activityId, beforeId)
    }
    function up(event: globalThis.PointerEvent) {
      if (event.pointerId !== pointerId) return
      x = event.clientX; y = event.clientY
      update()
      finish(true)
    }
    function cancel() { finish() }
    function cancelPointer(event: globalThis.PointerEvent) { if (event.pointerId === pointerId) cancel() }
    function key(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      event.preventDefault(); event.stopPropagation(); cancel()
    }
    cancelRef.current = cancel
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', cancelPointer)
    window.addEventListener('blur', cancel)
    window.addEventListener('keydown', key, true)
    handle.addEventListener('lostpointercapture', cancel)
    frame = requestAnimationFrame(tick)
  }

  return { listRef, drag, start }
}
