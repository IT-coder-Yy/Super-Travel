import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sharedToolsUrl, verifySharedTools } from './configure-tools.mjs';

test('正式发布缺少共享服务地址时拒绝构建，开发构建可不配置', () => {
  assert.equal(sharedToolsUrl({}), '');
  assert.throws(() => sharedToolsUrl({ GITHUB_REF: 'refs/tags/v0.1.2' }));
  assert.equal(sharedToolsUrl({ STA_SHARED_TOOLS_URL: 'https://tools.example.test/' }), 'https://tools.example.test');
});

test('共享密钥和非 HTTPS 地址不能进入客户端配置', () => {
  for (const url of ['http://tools.example.test', 'https://user:secret@tools.example.test', 'https://tools.example.test/?key=secret', 'https://tools.example.test/#secret', 'https://tools.example.test/a b']) {
    assert.throws(() => sharedToolsUrl({ STA_SHARED_TOOLS_URL: url }));
  }
});

test('正式发布要求服务已配置并提供地点工具，单独健康检查不算通过', async () => {
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
});
