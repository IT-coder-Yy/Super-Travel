import { invoke, isTauri } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';
import { getCurrentWindow } from '@tauri-apps/api/window';

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
  return isTauri() ? invoke<string>('app_version') : (import.meta.env.VITE_APP_VERSION || '0.1.0');
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
