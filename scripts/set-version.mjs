import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const version = process.argv[2];
if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version || '')) throw new Error('用法：node scripts/set-version.mjs 0.1.1（首版使用稳定 SemVer）');
for (const file of ['package.json', 'client/package.json', 'client/src-tauri/tauri.conf.json', 'package-lock.json']) {
  const target=path.join(root,file);
  if (!existsSync(target)) continue;
  const json=JSON.parse(readFileSync(target,'utf8'));
  json.version=version;
  if (json.packages?.['']) json.packages[''].version=version;
  if (json.packages?.client) json.packages.client.version=version;
  writeFileSync(target,JSON.stringify(json,null,2)+'\n');
}
const cargo=path.join(root,'client/src-tauri/Cargo.toml');
const text=readFileSync(cargo,'utf8');
writeFileSync(cargo,text.replace(/(\[package\][\s\S]*?\nversion\s*=\s*)"[^"]+"/,`$1"${version}"`));
const lock=path.join(root,'client/src-tauri/Cargo.lock');
if(existsSync(lock)) writeFileSync(lock,readFileSync(lock,'utf8').replace(/(name = "supertravel-desktop"\nversion = )"[^"]+"/,`$1"${version}"`));
const pythonProject = path.join(root, 'server/pyproject.toml');
writeFileSync(pythonProject, readFileSync(pythonProject, 'utf8').replace(/(^version = )"[^"]+"/m, `$1"${version}"`));
const pythonLock = path.join(root, 'server/uv.lock');
if (existsSync(pythonLock)) writeFileSync(pythonLock, readFileSync(pythonLock, 'utf8').replace(/(name = "supertravel-server"\nversion = )"[^"]+"/, `$1"${version}"`));
console.log(`Version set to ${version}; commit all version files together before tagging v${version}.`);
