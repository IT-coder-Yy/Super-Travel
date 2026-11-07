import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sharedToolsUrl, verifySharedTools } from './configure-tools.mjs';

test('开发和正式构建默认本机服务，并允许显式回环或 HTTPS 地址', () => {
  assert.equal(sharedToolsUrl({}), 'http://127.0.0.1:8080');
  assert.equal(sharedToolsUrl({ GITHUB_REF: 'refs/tags/v0.1.2', STA_SHARED_TOOLS_URL: ' ' }), 'http://127.0.0.1:8080');
  for (const endpoint of ['http://127.0.0.1:8080', 'http://127.2.3.4:8080', 'http://localhost:8080', 'http://[::1]:8080']) {
    assert.equal(sharedToolsUrl({ STA_SHARED_TOOLS_URL: endpoint + '/' }), endpoint);
  }
  assert.equal(sharedToolsUrl({ STA_SHARED_TOOLS_URL: 'https://tools.example.test/' }), 'https://tools.example.test');
});

test('拒绝共享密钥、非回环 HTTP 和 query/fragment', () => {
  for (const url of ['http://tools.example.test', 'http://192.168.1.2:8080', 'http://127.evil.test', 'http://localhost.evil.test', 'http://[::2]:8080', 'ftp://127.0.0.1', 'https://user:secret@tools.example.test', 'http://user:secret@127.0.0.1', 'https://tools.example.test/?key=secret', 'https://tools.example.test/#secret', 'http://127.0.0.1:8080/?', 'http://127.0.0.1:8080/#', 'https://tools.example.test/a b', 'https://tools.example.test/' + 'a'.repeat(2048)]) {
    assert.throws(() => sharedToolsUrl({ STA_SHARED_TOOLS_URL: url }));
  }
  for (const url of ['http:\\127.0.0.1:8080', 'https://tools.example.test\\path', '\nhttp://127.0.0.1:8080', 'https://tools.exa\tmple.test', 'https://tools.example.test/\0', 'https://tools.example.test/\x7f']) {
    assert.throws(() => sharedToolsUrl({ STA_SHARED_TOOLS_URL: url }));
  }
  assert.throws(() => sharedToolsUrl({ STA_SHARED_TOOLS_URL: 'https://tools.example.test/' + '旅'.repeat(300) }));
});

test('本机模式发布无需 CI 启动工具服务，仍拒绝远程 HTTP', async () => {
  const request = async () => { throw new Error('本机模式不应发起网络请求'); };
  for (const endpoint of ['http://127.0.0.1:8080', 'http://localhost:8080', 'http://[::1]:8080']) {
    await verifySharedTools(endpoint, request);
  }
  await assert.rejects(verifySharedTools('http://tools.example.test', request));
});

test('显式 HTTPS 发布仍要求服务已配置并提供地点工具，单独健康检查不算通过', async () => {
  const ready = { providers: ['amap', 'baidu', 'tavily', 'serper', 'xhs'].map(id => ({ id, configured: true })), tools: [{ name: 'baidu_map_search_places' }] };
  await verifySharedTools('https://relay.example.test', async (url, options) => {
    assert.equal(url, 'https://relay.example.test/v1/tools');
    assert.equal(options.redirect, 'error');
    return Response.json(ready);
  });
  for (const data of [{ ok: true }, { ...ready, tools: [] }, { ...ready, providers: [] }]) {
    await assert.rejects(verifySharedTools('https://relay.example.test', async () => Response.json(data)));
  }
  await assert.rejects(verifySharedTools('https://relay.example.test', async () => new Response('', { status: 503 })));
  await assert.rejects(verifySharedTools('https://127.0.0.1:8080', async () => new Response('', { status: 503 })));
});
