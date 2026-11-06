import { useEffect, useRef, useState } from 'react'
import { Download, FileText, LoaderCircle, RotateCcw } from 'lucide-react'
import { ApiError } from '../api'
import { saveMarkdown, savePdf } from '../desktop'
import type { TripExportFormat, TripExportSnapshot } from '../types'

type Props = {
  hasDraft: boolean
  onPrepare: (applyDraft: boolean, signal: AbortSignal, format: TripExportFormat) => Promise<TripExportSnapshot>
  onReload: (signal: AbortSignal) => Promise<void>
  onSaving: (saving: boolean) => void
}

export function TripExport({ hasDraft, onPrepare, onReload, onSaving }: Props) {
  const [format, setFormat] = useState<TripExportFormat>('markdown')
  const [snapshot, setSnapshot] = useState<TripExportSnapshot | null>(null)
  const [pending, setPending] = useState<'preview' | 'apply' | 'reload' | 'save' | null>(null)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [needsReload, setNeedsReload] = useState(false)
  const requestRef = useRef<AbortController | null>(null)
  const previewRef = useRef<HTMLPreElement>(null)
  const formatLabel = format === 'pdf' ? 'PDF' : 'Markdown'

  useEffect(() => () => { requestRef.current?.abort() }, [])
  useEffect(() => { if (snapshot) previewRef.current?.focus() }, [snapshot])

  async function run(action: 'preview' | 'apply' | 'reload' | 'save') {
    if (requestRef.current) return
    const controller = new AbortController()
    requestRef.current = controller
    setPending(action); setError(''); setStatus('')
    if (action === 'save') onSaving(true)
    try {
      if (action === 'save') {
        if (!snapshot) return
        const result = format === 'pdf' ? await savePdf(snapshot.filename, snapshot.pdfBase64!) : await saveMarkdown(snapshot.filename, snapshot.markdown)
        if (controller.signal.aborted) return
        setStatus(result.status === 'cancelled' ? '已取消保存，预览仍保留，可重新选择保存位置。' : result.status === 'downloaded' ? '已开始下载，请在浏览器下载列表中查看。' : `${formatLabel} 已保存${result.path ? `：${result.path}` : '到所选位置。'}`)
      } else if (action === 'reload') {
        await onReload(controller.signal)
        if (controller.signal.aborted) return
        setSnapshot(null); setNeedsReload(false)
        setStatus('已重新读取会话。请重新选择导出版本，核对预览后再保存。')
      } else {
        const result = await onPrepare(action === 'apply', controller.signal, format)
        if (format === 'pdf' && (!result.pdfBase64 || !result.previewText)) throw new Error('PDF 文件没有生成，请重试或改用 Markdown。')
        if (!controller.signal.aborted) setSnapshot(result)
      }
    } catch (reason) {
      if (!controller.signal.aborted) {
        const conflict = reason instanceof ApiError && reason.status === 409
        setNeedsReload(conflict || action === 'reload')
        setError(conflict ? '行程版本已变化，请重新读取会话并确认导出版本。重新读取会替换当前未保存的修改。' : reason instanceof Error ? reason.message : typeof reason === 'string' ? reason : '操作没有完成，请重试。')
      }
    } finally {
      if (action === 'save') onSaving(false)
      if (!controller.signal.aborted) { requestRef.current = null; setPending(null) }
    }
  }

  return <div className="trip-export" aria-busy={!!pending}>
    {snapshot ? <>
      <div className="export-snapshot"><FileText size={19} /><div><strong>{snapshot.filename}</strong><p>{formatLabel} · 正式行程 · 快照会话版本 {snapshot.revision}</p></div></div>
      <p className="export-note">{format === 'pdf' ? '下方仅预览文字内容；PDF 会自动分页，排版请以保存后的文件为准。预览后发生的修改不会加入此文件。' : '保存内容与下方预览完全一致，预览后发生的修改不会加入此文件。'}</p>
      <pre ref={previewRef} className="markdown-preview" aria-label={format === 'pdf' ? 'PDF 内容预览' : 'Markdown 预览'} tabIndex={0}>{format === 'pdf' ? snapshot.previewText : snapshot.markdown}</pre>
    </> : <>
      <div className="field-label export-format"><label htmlFor="trip-export-format">导出格式</label><select id="trip-export-format" value={format} disabled={!!pending || needsReload} onChange={event => { setFormat(event.target.value as TripExportFormat); setError(''); setStatus('') }}><option value="markdown">Markdown</option><option value="pdf">PDF</option></select></div>
      <p>{hasDraft ? '当前有未应用的日程或候选草稿。导出已保存正式版不包含这些草稿；先应用会将草稿保存为正式内容。' : '导出当前正式行程。预览前会等待保存完成，再读取正式内容。'}</p>
      {!needsReload && <div className="export-choices">
        <button type="button" className={hasDraft ? 'secondary-button' : 'primary-button'} disabled={!!pending} onClick={() => void run('preview')}><FileText size={17} />{hasDraft ? '导出已保存正式版' : '预览正式行程'}</button>
        {hasDraft && <button type="button" className="primary-button" disabled={!!pending} onClick={() => void run('apply')}>先应用草稿再导出</button>}
      </div>}
    </>}
    {pending && <p className="export-status" role="status"><LoaderCircle size={17} className="spin" />{pending === 'apply' ? '正在保存、核查并应用草稿…' : pending === 'preview' ? '正在完成保存并读取正式行程…' : pending === 'reload' ? '正在重新读取会话…' : `正在保存 ${formatLabel}…`}</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {status && <p className="export-status" role="status">{status}</p>}
    {needsReload && <button type="button" className="secondary-button export-reload" disabled={!!pending} onClick={() => void run('reload')}><RotateCcw size={16} />确认重新读取会话</button>}
    {snapshot && <div className="dialog-actions export-actions"><span>{format === 'pdf' ? 'PDF · 自动分页' : 'UTF-8 · .md'}</span><button type="button" className="primary-button" disabled={!!pending} onClick={() => void run('save')}><Download size={17} />保存 {formatLabel}</button></div>}
  </div>
}
