import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseEnv } from 'node:util';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const keyNames = ['AMAP_MAPS_API_KEY', 'BAIDU_MAP_API_KEY', 'TAVILY_API_KEY', 'SERPER_API_KEY'];

export function toolEnvironment(env, file = {}) {
  const result = { ...env };
  // The service needs provider credentials only; never load the whole development .env.
  for (const name of ['STA_MODEL_API_KEY', 'STA_MODEL_BASE_URL', 'STA_MODEL_NAME', 'SAGE_API_KEY', 'SAGE_BASE_URL', 'SAGE_MODEL_NAME', 'XHS_COOKIE', 'STA_BACKEND_TOKEN']) delete result[name];
  for (const name of keyNames) result[name] = file[name] || env[name] || '';
  return result;
}

export function toolPort(value = '8080') {
  if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 65535) throw new Error('STA_TOOLS_PORT 必须是 1～65535 的端口。');
  return String(Number(value));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const file = process.env.STA_DEV_ENV_FILE ? parseEnv(readFileSync(process.env.STA_DEV_ENV_FILE, 'utf8')) : {};
    const env = toolEnvironment(process.env, file);
    if (!keyNames.some(name => env[name])) throw new Error('请通过 STA_DEV_ENV_FILE 或环境变量配置工具服务密钥。');
    const port = toolPort(process.env.STA_TOOLS_PORT);
    const venv = resolve(root, 'backend/.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    const child = spawn(process.env.STA_PYTHON || (existsSync(venv) ? venv : 'python'), [
      '-m', 'uvicorn', 'backend.shared_tools_server:create_app', '--factory', '--host', '127.0.0.1', '--port', port,
      '--workers', '1', '--limit-concurrency', '32', '--no-access-log', '--no-proxy-headers',
    ], { cwd: root, env, stdio: 'inherit', windowsHide: true });
    console.log(`工具服务地址：http://127.0.0.1:${port}；密钥仅供本机工具服务使用。`);
    child.once('error', () => { console.error('无法启动工具服务，请检查本项目 Python 环境。'); process.exitCode = 1; });
    child.once('exit', code => { process.exitCode = code ?? 1; });
    for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill());
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
