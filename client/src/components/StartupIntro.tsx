import { useEffect, useRef, useState, type ReactNode } from 'react'
import appIcon from '../assets/app-icon.svg'
import './StartupIntro.css'

const clamp = (value: number) => Math.max(0, Math.min(1, value))
const span = (value: number, from: number, to: number) => clamp((value - from) / (to - from))
const ease = (value: number) => 1 - Math.pow(1 - clamp(value), 3)
const smooth = (value: number) => { const p = clamp(value); return p * p * (3 - 2 * p) }
const mix = (a: number, b: number, p: number) => a + (b - a) * p
const random = (n: number) => { const value = Math.sin(n * 78.233 + 14.129) * 43758.5453; return value - Math.floor(value) }
const sphere = Array.from({ length: 1850 }, (_, i) => {
  const y = 1 - i / 1849 * 2, radius = Math.sqrt(1 - y * y), angle = i * 2.3999632297
  return { x: Math.cos(angle) * radius, y, z: Math.sin(angle) * radius, seed: random(i) }
})

function drawOrbit(ctx: CanvasRenderingContext2D, time: number) {
  const circle = (x: number, y: number, radius: number, color: string) => {
    if (radius <= 0) return
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.fill()
  }
  const gather = ease(span(time, 0, 760))
  const collapse = smooth(span(time, 1280, 1770))
  const alpha = 1 - span(time, 1510, 1840)
  const radius = mix(225, 37, collapse), centerY = mix(393, 340, collapse)
  const background = ctx.createRadialGradient(640, 390, 10, 640, 400, 800)
  background.addColorStop(0, '#102543'); background.addColorStop(.55, '#071323'); background.addColorStop(1, '#03080f')
  ctx.fillStyle = background; ctx.fillRect(0, 0, 1280, 800)
  for (let i = 0; i < 75; i++) circle(random(i + 2000) * 1280, random(i + 4000) * 800, .7, `rgba(141,175,220,${.2 * (1 - collapse)})`)
  if (alpha > 0) {
    const angle = .4 + time / 1200
    const points = sphere.map(point => {
      const x = point.x * Math.cos(angle) - point.z * Math.sin(angle)
      const z = point.x * Math.sin(angle) + point.z * Math.cos(angle)
      const depth = 3.5 / (3.5 - z)
      const targetX = 640 + x * radius * depth, targetY = centerY + (point.y * .96 + z * .24) * radius * depth
      const direction = Math.atan2(targetY - centerY, targetX - 640) + (1 - gather) * 1.1
      const distance = 700 + point.seed * 500
      return { x: mix(640 + Math.cos(direction) * distance, targetX, gather), y: mix(400 + Math.sin(direction) * distance, targetY, gather), z }
    }).sort((a, b) => a.z - b.z)
    for (const point of points) {
      const front = (point.z + 1) / 2
      circle(point.x, point.y, (1.2 + front * 1.9) * mix(1, .7, collapse), `rgba(${front > .62 ? '168,211,255' : '61,114,191'},${alpha * (.17 + front * .7)})`)
    }
    const ringRadius = radius * 1.45, tilt = -.4
    const ringPoint = (angle: number): [number, number] => {
      const x = Math.cos(angle) * ringRadius, y = Math.sin(angle) * ringRadius * .34
      return [640 + x * Math.cos(tilt) - y * Math.sin(tilt), centerY + x * Math.sin(tilt) + y * Math.cos(tilt)]
    }
    const opacity = alpha * span(time, 420, 670), sweep = ease(span(time, 600, 1280))
    ctx.lineWidth = 1; ctx.strokeStyle = `rgba(106,151,223,${opacity * .24})`; ctx.beginPath()
    for (let i = 0; i <= 150; i++) { const point = ringPoint(i / 150 * Math.PI * 2); if (!i) ctx.moveTo(...point); else ctx.lineTo(...point) }
    ctx.stroke()
    ctx.lineWidth = 2.7; ctx.strokeStyle = `rgba(173,211,255,${opacity})`; ctx.beginPath()
    for (let i = 0; i <= 110; i++) { const point = ringPoint(-.7 + sweep * 6.3 - .9 + i / 110 * .9); if (!i) ctx.moveTo(...point); else ctx.lineTo(...point) }
    ctx.stroke()
    const point = ringPoint(-.7 + sweep * 6.3)
    circle(point[0], point[1], 10, `rgba(53,135,255,${opacity * .15})`)
    circle(point[0], point[1], 4, `rgba(241,249,255,${opacity})`)
    ctx.strokeStyle = `rgba(129,179,245,${alpha * .1})`; ctx.lineWidth = 1
    ctx.beginPath(); ctx.ellipse(640, centerY, radius * .7, radius * 1.1, .37, 0, Math.PI * 2); ctx.stroke()
  }
  const portal = ease(span(time, 2300, 3000))
  if (portal > 0) {
    const radius = portal * 850
    for (let i = 0; i < 30; i++) {
      const angle = i / 30 * Math.PI * 2 + .13, inner = radius + 10, outer = radius + 40 + random(i) * 100
      ctx.strokeStyle = `rgba(130,183,249,${(1 - portal) * .3})`; ctx.beginPath()
      ctx.moveTo(640 + Math.cos(angle) * inner, 400 + Math.sin(angle) * inner)
      ctx.lineTo(640 + Math.cos(angle) * outer, 400 + Math.sin(angle) * outer); ctx.stroke()
    }
    ctx.globalCompositeOperation = 'destination-out'; circle(640, 400, radius, '#000'); ctx.globalCompositeOperation = 'source-over'
  }
}

export function StartupIntro({ children }: { children: ReactNode }) {
  const [active, setActive] = useState(true)
  const overlayRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const brandRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const restoreFocus = useRef(false)
  const finish = () => {
    restoreFocus.current = overlayRef.current?.contains(document.activeElement) ?? false
    setActive(false)
  }

  useEffect(() => {
    if (!active) {
      if (restoreFocus.current && document.hasFocus()) contentRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus({ preventScroll: true })
      return
    }
    const overlay = overlayRef.current!, canvas = canvasRef.current!, brand = brandRef.current!
    const ctx = canvas.getContext('2d')
    if (!ctx) { finish(); return }
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
    const duration = reduced.matches ? 300 : 3000
    const startedAt = performance.now()
    let frame = 0
    const render = (now: number) => {
      const time = Math.min(duration, now - startedAt)
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height)
      const scale = Math.max(canvas.width / 1280, canvas.height / 800)
      ctx.setTransform(scale, 0, 0, scale, (canvas.width - 1280 * scale) / 2, (canvas.height - 800 * scale) / 2)
      if (reduced.matches) {
        ctx.fillStyle = `rgba(7,19,35,${1 - time / duration})`; ctx.fillRect(0, 0, 1280, 800)
        brand.style.opacity = String(1 - time / duration)
      } else {
        drawOrbit(ctx, time)
        const intro = ease(span(time, 1470, 1770)), outro = span(time, 2210, 2440)
        brand.style.opacity = String(intro * (1 - outro))
        brand.style.transform = `translate(-50%, -50%) scale(${mix(.87, 1, intro) + outro * .13})`
        brand.style.filter = `blur(${(1 - intro) * 5 + outro * 4}px)`
      }
      overlay.style.background = 'transparent'
    }
    const fit = () => {
      const bounds = overlay.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 1.5)
      canvas.width = Math.round(bounds.width * dpr); canvas.height = Math.round(bounds.height * dpr)
      render(performance.now())
    }
    const tick = (now: number) => {
      if (now - startedAt >= duration) { finish(); return }
      render(now); frame = requestAnimationFrame(tick)
    }
    const onVisibility = () => { if (document.hidden) finish() }
    const observer = new ResizeObserver(fit)
    observer.observe(overlay); fit(); overlay.focus({ preventScroll: true })
    frame = requestAnimationFrame(tick)
    // 后台节流或低帧率也不能让开场挡住工作区。
    const timer = window.setTimeout(finish, duration)
    document.addEventListener('visibilitychange', onVisibility)
    reduced.addEventListener('change', finish)
    return () => {
      cancelAnimationFrame(frame); window.clearTimeout(timer); observer.disconnect()
      document.removeEventListener('visibilitychange', onVisibility); reduced.removeEventListener('change', finish)
    }
  }, [active])

  return <>
    <div ref={contentRef} inert={active}>{children}</div>
    {active && <div ref={overlayRef} className="startup-intro" role="dialog" aria-modal="true" aria-label="SuperTravel 开场动画" aria-describedby="startup-hint" tabIndex={-1} onKeyDown={event => {
      event.preventDefault(); event.stopPropagation(); finish()
    }}>
      <canvas ref={canvasRef} aria-hidden="true" />
      <div ref={brandRef} className="startup-brand" aria-hidden="true"><img src={appIcon} width={92} height={92} alt="" /><strong>SuperTravel</strong><span>开启下一程</span></div>
      <p id="startup-hint" className="startup-hint">按任意键跳过</p>
    </div>}
  </>
}
