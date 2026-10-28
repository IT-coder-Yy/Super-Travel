import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { configureUpdater } from './configure-updater.mjs';
import { sharedToolsUrl } from './configure-tools.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
process.chdir(root);
const venv = path.join(root, 'backend', '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.STA_PYTHON || (existsSync(venv) ? venv : 'python');
const args = process.argv.slice(2);
const isDev = args[0] === '--dev';
if (isDev) args.shift();
const targetIndex = args.indexOf('--target');
const targetArgs = targetIndex >= 0 ? ['--target', args[targetIndex + 1]] : [];

function run(command, params) {
  const result = spawnSync(command, params, { cwd: root, stdio: 'inherit', env: process.env });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}

try {
  process.env.STA_SHARED_TOOLS_URL = sharedToolsUrl(process.env);
  const config = await configureUpdater();
  run(python, ['scripts/generate-icons.py']);
  run(python, ['scripts/build-sidecar.py', ...targetArgs]);
  run(process.execPath, ['node_modules/@tauri-apps/cli/tauri.js', isDev ? 'dev' : 'build', '--config', config, ...args]);
} catch (error) { console.error(error.message); process.exitCode = 1; }
