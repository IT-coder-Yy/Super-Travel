import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

export function Dialog({ title, children, onClose, wide = false, busy = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => { if (dialog?.open) dialog.close() } }, [])
  return <dialog ref={ref} aria-label={title} className={`dialog ${wide ? 'dialog--wide' : ''}`} onCancel={event => { event.preventDefault(); if (!busy) onClose() }} onClick={event => { if (!busy && event.target === event.currentTarget) onClose() }}>
    <div className="dialog-header"><h2>{title}</h2><button className="icon-button" onClick={onClose} disabled={busy} aria-label="关闭窗口"><X size={21} /></button></div>
    {children}
  </dialog>
}
