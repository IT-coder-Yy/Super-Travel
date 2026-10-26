import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
import { parseEnv } from 'node:util';
import { readFileSync } from 'node:fs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const localPython = resolve(root, 'backend', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.STA_PYTHON || (existsSync(localPython) ? localPython : 'python');
const dataDir = resolve(root, '.local', 'dev-data');
mkdirSync(dataDir, { recursive: true });
const token = randomBytes(32).toString('hex');
const appVersion = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8')).version;
// Only the explicitly selected model fields enter the client; tool keys stay in the service.
const modelEnv = process.env.STA_DEV_ENV_FILE ? parseEnv(readFileSync(process.env.STA_DEV_ENV_FILE, 'utf8')) : {};
const backend = spawn(python, [resolve(root, 'backend/main.py'), '--host', '127.0.0.1', '--port', '0', '--data-dir', dataDir], {
  cwd: root,
  env: { ...process.env, STA_APP_VERSION: appVersion, STA_MODEL_API_KEY: modelEnv.SAGE_API_KEY || process.env.STA_MODEL_API_KEY || '', STA_MODEL_BASE_URL: modelEnv.SAGE_BASE_URL || process.env.STA_MODEL_BASE_URL || '', STA_MODEL_NAME: modelEnv.SAGE_MODEL_NAME || process.env.STA_MODEL_NAME || '', STA_BACKEND_TOKEN: token, PYTHONUNBUFFERED: '1' },
  stdio: ['pipe', 'pipe', 'pipe'],
  windowsHide: true,
});
let web;
let stopping = false;
const stop = (code = 0) => {
  if (stopping) return;
  stopping = true;
  clearTimeout(startupTimeout);
  backend.stdin.end();
  web?.kill();
  const fallback = setTimeout(() => { backend.kill(); process.exit(code); }, 6000);
  fallback.unref();
  backend.once('exit', () => process.exit(code));
  if (backend.exitCode !== null) process.exit(code);
};
const startupTimeout = setTimeout(() => {
  console.error('Python 服务启动超时，请检查后端依赖。');
  stop(1);
}, 30000);
backend.stderr.pipe(process.stderr);
backend.on('error', (error) => { console.error(`无法启动 Python：${error.message}`); stop(1); });
backend.on('exit', (code) => { if (!stopping) { console.error(`Python 服务退出：${code}`); stop(code || 1); } });
createInterface({ input: backend.stdout }).on('line', (line) => {
  let ready;
  try { ready = JSON.parse(line); } catch { console.log(`[Python] ${line}`); return; }
  if (ready.event !== 'ready' || web) return;
  clearTimeout(startupTimeout);
  web = spawn(process.execPath, [resolve(root, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', '1420', '--strictPort'], {
    cwd: root,
    env: { ...process.env, VITE_API_BASE: `http://127.0.0.1:${ready.port}`, VITE_API_TOKEN: token, VITE_DATA_DIR: dataDir },
    stdio: 'inherit',
    windowsHide: true,
  });
  web.on('error', (error) => { console.error(error.message); stop(1); });
  web.on('exit', (code) => stop(code || 0));
  console.log('本地 Python 服务已就绪；开发数据保存在 .local/dev-data。');
});
process.on('SIGINT', () => stop());
process.on('SIGTERM', () => stop());
