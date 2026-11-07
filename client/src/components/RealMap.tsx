import { useEffect, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import type { Activity } from '../types'
import './RealMap.css'

type Props = { activities: Activity[]; selected: string; zoom: number; layers: boolean; onSelect: (id: string) => void; fitKey?: string }

export function RealMap({ activities, selected, zoom, layers, onSelect, fitKey }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<L.Map | null>(null)
  const tiles = useRef<L.TileLayer | null>(null)
  const markers = useRef<L.LayerGroup | null>(null)
  const select = useRef(onSelect)
  const previousZoom = useRef(zoom)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  select.current = onSelect
  const located = activities.filter(activity => activity.place?.coordinateSystem === 'wgs84' &&
    Number.isFinite(activity.place.latitude) && Math.abs(activity.place.latitude) <= 85 &&
    Number.isFinite(activity.place.longitude) && Math.abs(activity.place.longitude) <= 180)
  const locations = useRef(located)
  locations.current = located
  const boundsKey = located.map(activity => `${activity.id}:${activity.place!.latitude}:${activity.place!.longitude}`).join('|')

  function padding() {
    const size = map.current!.getSize()
    // Leave room for the existing itinerary, day tabs and conversation overlays.
    return size.x < 700
      ? { paddingTopLeft: L.point(30, 225), paddingBottomRight: L.point(175, 155) }
      : { paddingTopLeft: L.point(Math.min(430, size.x * .39), 95), paddingBottomRight: L.point(290, Math.min(300, size.y * .35)) }
  }

  function fit() {
    const instance = map.current
    if (!instance) return
    const points = locations.current.map(activity => L.latLng(activity.place!.latitude, activity.place!.longitude))
    if (points.length) {
      instance.fitBounds(L.latLngBounds(points), { ...padding(), maxZoom: 15, animate: false })
      if (tiles.current && !instance.hasLayer(tiles.current)) tiles.current.addTo(instance)
    } else {
      if (tiles.current && instance.hasLayer(tiles.current)) instance.removeLayer(tiles.current)
      instance.setView([35, 105], 4, { animate: false })
      setStatus('ready')
    }
  }

  useEffect(() => {
    const animate = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const instance = L.map(container.current!, { zoomControl: false, minZoom: 3, maxZoom: 19, attributionControl: true,
      fadeAnimation: animate, zoomAnimation: animate, markerZoomAnimation: animate })
    map.current = instance
    instance.attributionControl.setPrefix(false)
    const layer = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
      maxZoom: 19, noWrap: true, keepBuffer: 0, updateWhenIdle: true, updateWhenZooming: false,
      referrerPolicy: 'strict-origin-when-cross-origin',
    })
    let failed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    layer.on('loading', () => {
      failed = false
      setStatus('loading')
      clearTimeout(timer)
      timer = setTimeout(() => { failed = true; setStatus('error') }, 15_000)
    })
    layer.on('tileerror', () => { failed = true; setStatus('error') })
    layer.on('load', () => { clearTimeout(timer); setStatus(failed ? 'error' : 'ready') })
    tiles.current = layer
    markers.current = L.layerGroup().addTo(instance)
    fit()
    const observer = new ResizeObserver(() => { instance.invalidateSize({ pan: false }); fit() })
    observer.observe(container.current!)
    return () => {
      clearTimeout(timer)
      observer.disconnect()
      layer.off()
      instance.remove()
      map.current = null; tiles.current = null; markers.current = null
    }
  }, [])

  useEffect(() => {
    const group = markers.current
    if (!group) return
    group.clearLayers()
    located.forEach(activity => {
      const index = activities.findIndex(item => item.id === activity.id)
      const icon = document.createElement('div')
      const number = document.createElement('span')
      number.className = 'real-map-marker-number'
      number.textContent = String(index + 1)
      icon.append(number)
      if (layers) {
        const label = document.createElement('span')
        label.className = 'real-map-marker-label'
        label.textContent = activity.title
        icon.append(label)
      }
      const marker = L.marker([activity.place!.latitude, activity.place!.longitude], {
        icon: L.divIcon({ html: icon, className: `real-map-marker ${activity.id === selected ? 'is-selected' : ''}`, iconSize: [34, 34], iconAnchor: [17, 17] }),
        title: activity.title, keyboard: true, zIndexOffset: activity.id === selected ? 1000 : 0,
      }).addTo(group).on('click', () => select.current(activity.id))
      const element = marker.getElement()!
      element.setAttribute('aria-label', `查看${activity.title}`)
      element.addEventListener('keydown', event => {
        if (event.key === ' ') { event.preventDefault(); select.current(activity.id) }
      })
    })
  }, [activities, selected, layers])

  useEffect(() => { fit() }, [boundsKey, fitKey])
  useEffect(() => {
    const instance = map.current
    if (!instance || previousZoom.current === zoom) return
    if (zoom === 1) fit()
    else instance.setZoom(instance.getZoom() + Math.sign(zoom - previousZoom.current), { animate: false })
    previousZoom.current = zoom
  }, [zoom])
  useEffect(() => {
    const point = locations.current.find(activity => activity.id === selected)?.place
    if (point && map.current) map.current.panInside([point.latitude, point.longitude], { ...padding(), animate: false })
  }, [selected, boundsKey])

  return <div className="map-canvas real-map">
    <div ref={container} className="real-map-surface" role="region" aria-label="真实地理地图，可拖动缩放并选择行程地点" />
    {status !== 'ready' && <div className={`real-map-status ${status === 'error' ? 'is-error' : ''}`} role="status">
      {status === 'error' ? <>底图暂时无法完整加载，已定位的行程地点仍可查看。<button onClick={() => tiles.current?.redraw()}>重试地图</button></> : '正在加载地图…'}
    </div>}
    {activities.length > located.length && <p className="real-map-unlocated" role="status">{activities.length - located.length} 个地点待定位，暂未显示在地图上。</p>}
  </div>
}
