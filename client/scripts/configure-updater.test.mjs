import test from 'node:test';
import assert from 'node:assert/strict';
import { updaterConfig } from './configure-updater.mjs';
const pubkey=Buffer.from('untrusted comment: test key\nRWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==\n').toString('base64');
test('unconfigured app disables update artifacts instead of claiming up-to-date',()=>{
  assert.deepEqual(updaterConfig({}),{bundle:{createUpdaterArtifacts:false}});
});
test('production update configuration enforces version-bound signatures and HTTPS',()=>{
  const config=updaterConfig({STA_UPDATE_ENDPOINT:'https://github.com/owner/repo/releases/latest/download/latest.json',STA_UPDATER_PUBLIC_KEY:pubkey});
  assert.equal(config.plugins.updater.requireSignedVersion,true);
  assert.equal(config.plugins.updater.allowDowngrades,false);
  assert.equal(config.plugins.updater.dangerousInsecureTransportProtocol,false);
  assert.deepEqual(config.plugins.updater.endpoints,[
    'https://github.com/owner/repo/releases/latest/download/latest.json',
    'https://gh-proxy.com/https://github.com/owner/repo/releases/latest/download/latest.json',
  ]);
});

test('only public GitHub latest.json gets a mirror fallback',()=>{
  for (const endpoint of [
    'https://example.com/latest.json',
    'https://github.com/owner/repo/releases/latest/download/other.json',
    'https://github.com/owner/repo/releases/latest/download/latest.json?token=private',
    'https://github.com/owner/repo/releases/latest/download/latest.json#fragment',
    'https://github.com.evil.example/owner/repo/releases/latest/download/latest.json',
  ]) assert.deepEqual(updaterConfig({STA_UPDATE_ENDPOINT:endpoint,STA_UPDATER_PUBLIC_KEY:pubkey}).plugins.updater.endpoints,[endpoint]);
});

test('mirror may be disabled or configured, but never insecure or credentialed',()=>{
  const env={STA_UPDATE_ENDPOINT:'https://github.com/owner/repo/releases/latest/download/latest.json',STA_UPDATER_PUBLIC_KEY:pubkey};
  assert.equal(updaterConfig({...env,STA_UPDATE_MIRROR:''}).plugins.updater.endpoints.length,1);
  assert.equal(updaterConfig({...env,STA_UPDATE_MIRROR:'https://mirror.example/path/'}).plugins.updater.endpoints[1],`https://mirror.example/path/${env.STA_UPDATE_ENDPOINT}`);
  for (const mirror of ['http://mirror.example/','https://user:secret@mirror.example/','https://mirror.example/?token=secret','https://mirror.example/#fragment']) assert.throws(()=>updaterConfig({...env,STA_UPDATE_MIRROR:mirror}));
});
test('missing pair, remote HTTP and credentials are rejected',()=>{
  assert.throws(()=>updaterConfig({STA_UPDATE_ENDPOINT:'https://example.com'}));
  assert.throws(()=>updaterConfig({STA_UPDATE_ENDPOINT:'http://example.com',STA_UPDATER_PUBLIC_KEY:pubkey}));
  assert.throws(()=>updaterConfig({STA_UPDATE_ENDPOINT:'https://user:secret@example.com',STA_UPDATER_PUBLIC_KEY:pubkey}));
});
test('insecure transport is only available for explicit localhost rehearsal',()=>{
  assert.throws(()=>updaterConfig({STA_UPDATE_ENDPOINT:'http://127.0.0.1:17891/latest.json',STA_UPDATER_PUBLIC_KEY:pubkey}));
  assert.equal(updaterConfig({STA_UPDATE_ENDPOINT:'http://127.0.0.1:17891/latest.json',STA_UPDATER_PUBLIC_KEY:pubkey,STA_ALLOW_LOCAL_UPDATES:'1'}).plugins.updater.dangerousInsecureTransportProtocol,true);
  assert.throws(()=>updaterConfig({STA_UPDATE_ENDPOINT:'http://evil.example/latest.json',STA_UPDATER_PUBLIC_KEY:pubkey,STA_ALLOW_LOCAL_UPDATES:'1'}));
});
