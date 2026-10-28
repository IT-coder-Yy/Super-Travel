import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toolEnvironment, toolPort } from './tools-dev.mjs';

test('本机工具服务仅加载指定服务凭据，不继承模型与会话凭据', () => {
  const env = toolEnvironment({ PATH: 'runtime', STA_MODEL_API_KEY: 'model', STA_BACKEND_TOKEN: 'session', BAIDU_MAP_API_KEY: 'previous' }, {
    BAIDU_MAP_API_KEY: 'provider', SAGE_API_KEY: 'private-model', XHS_COOKIE: 'cookie', UNRELATED: 'private',
  });
  assert.equal(env.PATH, 'runtime');
  assert.equal(env.BAIDU_MAP_API_KEY, 'provider');
  for (const value of ['model', 'session', 'private-model', 'cookie', 'private']) assert.ok(!Object.values(env).includes(value));
});

test('默认工具端口与用户配置保持一致，拒绝无效端口', () => {
  assert.equal(toolPort(), '8080');
  assert.equal(toolPort('19090'), '19090');
  for (const value of ['0', '65536', '-1', '8080/path', '']) assert.throws(() => toolPort(value));
});
