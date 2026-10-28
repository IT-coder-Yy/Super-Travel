import { spawnSync } from 'node:child_process';
import { appendFileSync, cpSync, mkdirSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { sharedToolsUrl, verifySharedTools } from './configure-tools.mjs';
const [target, bundles] = process.argv.slice(2);
if (!target || !bundles) throw new Error('Usage: node scripts/ci-build.mjs <target-triple> <bundles>');
const version = JSON.parse(readFileSync('package.json', 'utf8')).version;
const release = (process.env.GITHUB_REF || '').startsWith('refs/tags/');
const signed = Boolean(process.env.TAURI_SIGNING_PRIVATE_KEY && process.env.STA_UPDATER_PUBLIC_KEY && process.env.STA_UPDATE_ENDPOINT);
if (release && (!signed || process.env.GITHUB_REF_NAME !== `v${version}`)) throw new Error('Tag release requires matching package version, signing secret, public key and endpoint.');
const env = { ...process.env };
env.STA_SHARED_TOOLS_URL = sharedToolsUrl(env);
if (release) await verifySharedTools(env.STA_SHARED_TOOLS_URL);
if (!signed) { delete env.STA_UPDATER_PUBLIC_KEY; delete env.STA_UPDATE_ENDPOINT; }
function run(command,args) {
  const result=spawnSync(command,args,{stdio:'inherit',env});
  if(result.error) throw result.error;
  if(result.status!==0) process.exit(result.status||1);
}
run(process.execPath,['scripts/desktop-build.mjs','--target',target,'--bundles',bundles]);
const extension = target.includes('windows') ? '.exe' : '';
run('python',['scripts/smoke-sidecar.py',`src-tauri/binaries/supertravel-backend-${target}${extension}`,'--version',version]);
const bundleDir=`src-tauri/target/${target}/release/bundle`;
const out=`artifacts/${target}`;
if(process.platform==='darwin' || process.platform==='linux') {
  const mac=process.platform==='darwin';
  const folder=path.join(bundleDir,mac?'macos':'appimage');
  const artifacts=readdirSync(folder,{withFileTypes:true}).filter(entry=>mac ? entry.isDirectory() && entry.name.endsWith('.app') : entry.isFile() && entry.name.endsWith('.AppImage'));
  if(artifacts.length!==1) throw new Error(`Expected one native application in ${folder}, found ${artifacts.length}`);
  const smoke=['scripts/smoke-native.py',path.join(folder,artifacts[0].name),'--version',version];
  if(mac) run('python',smoke);
  else run('xvfb-run',['-a','dbus-run-session','--','python',...smoke]);
}
if(signed) {
  const repository=process.env.GITHUB_REPOSITORY;
  if(!repository || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) throw new Error('Invalid GITHUB_REPOSITORY');
  run('python',['scripts/prepare-release.py','--target',target,'--bundle-dir',bundleDir,'--base-url',`https://github.com/${repository}/releases/download/v${version}`,'--output',out]);
} else {
  mkdirSync(out,{recursive:true});
  cpSync(bundleDir,path.join(out,'unsigned-bundle'),{recursive:true});
}
if(process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT,`signed=${signed}\n`);
