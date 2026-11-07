import { invoke, isTauri } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { version as packageVersion } from '../package.json';

export interface BackendConnection { baseUrl: string; token: string; dataDir: string }
export interface UpdateCheck { configured: boolean; version?: string | null; body?: string | null }
export type UpdatePhase = 'downloading' | 'installing';

export async function getBackendConnection(): Promise<BackendConnection> {
  if (!isTauri()) {
    const baseUrl = import.meta.env.VITE_API_BASE;
    const token = import.meta.env.VITE_API_TOKEN;
    if (!baseUrl || !token) throw new Error('本地服务未启动。请使用 npm run dev 或桌面客户端。');
    return { baseUrl, token, dataDir: import.meta.env.VITE_DATA_DIR || '本地开发数据目录' };
  }
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { return await invoke<BackendConnection>('backend_connection'); }
    catch (error) {
      if (String(error) !== 'BACKEND_STARTING') throw new Error(String(error));
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  throw new Error('本地服务启动超时，请重新启动应用。');
}

export async function getAppVersion(): Promise<string> {
  return isTauri() ? invoke<string>('app_version') : (import.meta.env.VITE_APP_VERSION || packageVersion);
}

export async function checkForUpdate(): Promise<UpdateCheck> {
  return isTauri() ? invoke<UpdateCheck>('check_update') : { configured: false };
}

/** The caller must finish saving the current conversation before calling this function. */
export async function installUpdate(onProgress: (percent: number | null, phase: UpdatePhase) => void): Promise<void> {
  if (!isTauri()) throw new Error('请在桌面客户端中安装更新。');
  const unlisten = await listen<{ percent: number | null; phase: UpdatePhase }>('supertravel-update', (event) => onProgress(event.payload.percent, event.payload.phase));
  try { await invoke('install_update'); } finally { unlisten(); }
}

export async function cancelUpdate(): Promise<void> {
  if (isTauri()) await invoke('cancel_update');
}

/** Returns the selected folder, saved for the next launch. Existing data is never moved. */
export async function chooseDataDirectory(): Promise<string | null> {
  if (!isTauri()) throw new Error('数据目录设置仅在桌面客户端中可用。');
  const selected = await open({ directory: true, multiple: false, title: '选择数据目录（重启后生效，原文件保留）' });
  if (!selected || Array.isArray(selected)) return null;
  return invoke<string>('set_data_directory', { path: selected });
}

/** Finish pending JSON writes before the last native window disappears. */
export async function onBeforeClose(save: () => Promise<void>, onError: (message: string) => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  const window = getCurrentWindow();
  let closing = false;
  return window.onCloseRequested(async (event) => {
    event.preventDefault();
    if (closing) return;
    closing = true;
    try { await save(); await window.destroy(); }
    catch (error) { closing = false; onError(`保存失败，窗口仍保持打开：${String(error)}`); }
  });
}

type ExportSaveResult = { status: 'saved' | 'downloaded' | 'cancelled'; path?: string };
const MAX_EXPORT_BYTES = 10 * 1024 * 1024;

function validateExport(filename: string, size: number): void {
  if (!filename.trim() || new TextEncoder().encode(filename).length > 240
    || /[<>:"/\\|?*\u0000-\u001f\u007f-\u009f]/.test(filename) || /[. ]$/.test(filename)
    || /^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/i.test(filename)) {
    throw new Error('导出文件名无效，请使用不含路径或特殊字符的文件名。');
  }
  if (size > MAX_EXPORT_BYTES) throw new Error('导出内容超过 10 MiB，请减少行程内容后重试。');
}

function downloadExport(filename: string, blob: Blob): ExportSaveResult {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  try {
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    return { status: 'downloaded' };
  } finally {
    anchor.remove();
    // Let the browser consume the URL before releasing it.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

/** Desktop confirms a completed write; browser can only confirm download initiation. */
export async function saveMarkdown(filename: string, markdown: string): Promise<ExportSaveResult> {
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
  validateExport(filename, blob.size);
  return isTauri() ? invoke('save_markdown', { filename, markdown }) : downloadExport(filename, blob);
}

export async function savePdf(filename: string, pdfBase64: string): Promise<ExportSaveResult> {
  if (pdfBase64.length > Math.ceil(MAX_EXPORT_BYTES / 3) * 4) throw new Error('导出内容超过 10 MiB，请减少行程内容后重试。');
  let decoded: string;
  try { decoded = atob(pdfBase64); }
  catch { throw new Error('PDF 内容无效，请重新生成后重试。'); }
  if (!decoded.startsWith('%PDF-')) throw new Error('PDF 内容无效，请重新生成后重试。');
  const bytes = Uint8Array.from(decoded, character => character.charCodeAt(0));
  validateExport(filename, bytes.byteLength);
  if (!filename.toLowerCase().endsWith('.pdf')) throw new Error('PDF 文件名必须以 .pdf 结尾。');
  return isTauri() ? invoke('save_pdf', { filename, bytes: Array.from(bytes) }) : downloadExport(filename, new Blob([bytes], { type: 'application/pdf' }));
}
