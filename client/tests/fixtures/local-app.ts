import { test as base, expect } from '@playwright/test';
import { execFile, spawn, type ChildProcess } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
import { promisify } from 'node:util';

export type LocalApp = {
  url: string;
  dataDir: string;
  request: (path: string, method?: string, body?: unknown) => Promise<any>;
  restart: () => Promise<void>;
  reset: () => Promise<void>;
};
const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const address = server.address();
  const port = typeof address === 'object' && address ? address.port : 0;
  await new Promise<void>(resolve => server.close(() => resolve()));
  return port;
}
async function exited(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise<void>(resolve => child.once('exit', () => resolve()));
}

export const test = base.extend<{}, { localApp: LocalApp }>({
  localApp: [async ({}, use) => {
    const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
    await mkdir(resolve(root, '.local'), { recursive: true });
    const dataDir = await mkdtemp(resolve(root, '.local/ui-fixture-'));
    const [apiPort, webPort] = await Promise.all([freePort(), freePort()]);
    const url = `http://127.0.0.1:${webPort}`;
    const apiBase = `http://127.0.0.1:${apiPort}`;
    const token = randomBytes(24).toString('hex');
    const localPython = resolve(root, 'server/.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    const python = process.env.STA_PYTHON || (existsSync(localPython) ? localPython : 'python');
    let backend: ChildProcess;
    let vite: ChildProcess | undefined;
    async function startBackend() {
      backend = spawn(python, ['client/tests/fixtures/agent-backend.py', '--port', String(apiPort), '--data-dir', dataDir], {
        cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, STA_BACKEND_TOKEN: token, STA_TEST_FRONTEND_ORIGIN: url, PYTHONUNBUFFERED: '1' },
      });
      await new Promise<void>((resolve, reject) => {
        let stderr = '';
        const timer = setTimeout(() => reject(new Error(`Fixture backend startup timeout: ${stderr}`)), 20_000);
        backend.stderr!.on('data', chunk => { stderr += String(chunk); });
        backend.once('error', error => { clearTimeout(timer); reject(error); });
        backend.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture backend exited (${code}): ${stderr}`)); });
        createInterface({ input: backend.stdout! }).on('line', line => {
          try { if (JSON.parse(line).event === 'ready') { clearTimeout(timer); resolve(); } } catch {}
        });
      });
    }
    const request = async (path: string, method = 'GET', body?: unknown) => {
      const response = await fetch(apiBase + '/api' + path, {
        method, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`Fixture API ${response.status}: ${await response.text()}`);
      return response.json();
    };
    try {
      await startBackend();
      const viteCli = resolve(root, 'node_modules/vite/bin/vite.js');
      const frontendEnv = { ...process.env, VITE_API_BASE: apiBase, VITE_API_TOKEN: token, VITE_DATA_DIR: dataDir };
      const production = process.env.STA_UI_BUILD === '1';
      const outDir = resolve(dataDir, 'frontend');
      if (production) {
        // Keep fixture credentials and generated bundles out of the distributable client/dist.
        const built = await promisify(execFile)(process.execPath, [viteCli, 'build', '--outDir', outDir, '--no-emptyOutDir'], {
          cwd: resolve(root, 'client'), env: frontendEnv, windowsHide: true, timeout: 60_000,
        });
        await writeFile(resolve(dataDir, 'frontend-build.log'), built.stdout + built.stderr, 'utf8');
        console.log('UI fixture: production build and preview (offline)');
      }
      vite = spawn(process.execPath, [viteCli, ...(production ? ['preview', '--outDir', outDir] : []), '--host', '127.0.0.1', '--port', String(webPort), '--strictPort'], {
        cwd: resolve(root, 'client'), windowsHide: true, stdio: 'ignore', env: frontendEnv,
      });
      let available = false;
      for (let attempt = 0; attempt < 60; attempt++) {
        try { if ((await fetch(url)).ok) { available = true; break; } } catch {}
        await wait(100);
      }
      if (!available) throw new Error('Fixture Vite did not start');
      await use({
        url, dataDir, request,
        restart: async () => { backend.kill('SIGKILL'); await exited(backend); await startBackend(); },
        reset: async () => {
          await request('/test-fixture/release', 'POST');
          await request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: '' });
        },
      });
    } finally {
      if (backend!) {
        await request('/test-fixture/release', 'POST').catch(() => undefined);
        backend.stdin?.end();
        await Promise.race([exited(backend), wait(5000)]);
        if (backend.exitCode === null && backend.signalCode === null) { backend.kill('SIGKILL'); await exited(backend); }
      }
      vite?.kill();
      if (vite) await exited(vite);
    }
  }, { scope: 'worker' }],
  // Default business regression is offline; keep the existing explicit photo opt-in.
  // A test's page.route fixture may supply a captured response instead.
  context: async ({ context }, use) => {
    await context.route('**/*', route => {
      const host = new URL(route.request().url()).hostname;
      const livePhoto = process.env.STA_LIVE_PHOTOS === '1' && ['thumb.wikimedia.org', 'upload.wikimedia.org'].includes(host);
      return ['127.0.0.1', 'localhost', '[::1]'].includes(host) || livePhoto ? route.continue() : route.abort('blockedbyclient');
    });
    await use(context);
  },
  baseURL: async ({ localApp }, use) => { await use(localApp.url); },
});
export { expect };
