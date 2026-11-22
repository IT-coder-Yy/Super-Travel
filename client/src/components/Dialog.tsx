import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

export function Dialog({ title, children, onClose, wide = false, busy = false, className = '' }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean; busy?: boolean; className?: string }) {
  const ref = useRef<HTMLDialogElement>(null)
  useLayoutEffect(() => {
    const dialog = ref.current
    dialog?.showModal()
    // Close while the dialog is still attached so the browser restores keyboard focus.
    return () => { if (dialog?.open) dialog.close() }
  }, [])
  return <dialog ref={ref} aria-label={title} className={`dialog ${wide ? 'dialog--wide' : ''} ${className}`} onCancel={event => { event.preventDefault(); if (!busy) onClose() }} onClick={event => { if (!busy && event.target === event.currentTarget) onClose() }}>
    <div className="dialog-header"><h2>{title}</h2><button className="icon-button" onClick={onClose} disabled={busy} aria-label="关闭窗口"><X size={21} /></button></div>
    {children}
  </dialog>
}
