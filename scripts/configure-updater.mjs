import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function manifestEndpoints(url, env) {
  const endpoints = [url.href];
  // Only public GitHub metadata may leave the canonical host; query strings may contain credentials.
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.search || url.hash || !/^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/releases\/latest\/download\/latest\.json$/.test(url.pathname)) return endpoints;
  const prefix = (env.STA_UPDATE_MIRROR ?? 'https://gh-proxy.com/').trim();
  if (!prefix) return endpoints;
  const mirror = new URL(prefix);
  if (mirror.protocol !== 'https:' || mirror.username || mirror.password || mirror.search || mirror.hash) throw new Error('更新镜像必须是无凭据、无查询参数的 HTTPS 地址。');
  endpoints.push(`${mirror.href.replace(/\/$/, '')}/${url.href}`);
  return endpoints;
}

export function updaterConfig(env) {
  const endpoint = (env.STA_UPDATE_ENDPOINT || '').trim();
  const pubkey = (env.STA_UPDATER_PUBLIC_KEY || '').trim();
  if (!endpoint && !pubkey) return { bundle: { createUpdaterArtifacts: false } };
  if (!endpoint || !pubkey) throw new Error('STA_UPDATE_ENDPOINT 与 STA_UPDATER_PUBLIC_KEY 必须同时配置。');
  const url = new URL(endpoint);
  if (url.username || url.password) throw new Error('更新地址不能包含账号凭据。');
  const local = url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname) && env.STA_ALLOW_LOCAL_UPDATES === '1';
  if (url.protocol !== 'https:' && !local) throw new Error('发行更新地址必须使用 HTTPS；本机演练需要显式 STA_ALLOW_LOCAL_UPDATES=1。');
  const publicText = Buffer.from(pubkey, 'base64').toString('utf8');
  if (!publicText.startsWith('untrusted comment:') || !publicText.split(/\r?\n/)[1]) throw new Error('更新公钥不是 Tauri signer 生成的公钥。');
  return {
    bundle: { createUpdaterArtifacts: true },
    plugins: { updater: { pubkey, endpoints: manifestEndpoints(url, env), requireSignedVersion: true, allowDowngrades: false, dangerousInsecureTransportProtocol: local, windows: { installMode: 'passive' } } },
  };
}

export async function configureUpdater(env = process.env) {
  const config = updaterConfig(env);
  const out = path.join(root, '.local', 'updater.config.json');
  await mkdir(path.dirname(out), { recursive: true });
  await writeFile(out, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
  return out;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(`Updater config: ${await configureUpdater()}`); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
