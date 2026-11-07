import { useState } from 'react'
import { Image as ImageIcon, MapPin } from 'lucide-react'

export const PHOTO = 'https://thumb.wikimedia.org/wikipedia/commons/thumb/d/dc/Sunset_over_the_Zigzag_Bridge%2C_West_Lake%2C_Hangzhou_20090909_1.jpg/960px-Sunset_over_the_Zigzag_Bridge%2C_West_Lake%2C_Hangzhou_20090909_1.jpg'
export const PHOTO_TWO = 'https://thumb.wikimedia.org/wikipedia/commons/thumb/6/6e/West_lake_twilight.jpg/500px-West_lake_twilight.jpg'

export function Photo({ src = PHOTO, className = '', alt = '杭州西湖风景参考照片' }: { src?: string; className?: string; alt?: string }) {
  const [failed, setFailed] = useState(false)
  const [loaded, setLoaded] = useState(false)
  return <div className={`photo ${loaded ? 'photo--loaded' : ''} ${className}`} aria-busy={!loaded && !failed}>
    {!failed && <img src={src} alt={alt} referrerPolicy="no-referrer" onLoad={event => { void event.currentTarget.decode().then(() => setLoaded(true)).catch(() => setFailed(true)) }} onError={() => setFailed(true)} />}
    {!loaded && !failed && <div className="photo-loading"><ImageIcon size={20} /><span>照片加载中</span></div>}
    {failed && <div className="photo-fallback"><MapPin size={24} /><span>西湖 · 山水之间</span></div>}
  </div>
}
