import type { Page } from '@playwright/test';
import { createWorkspace } from '../../src/types';
import { test, expect, type LocalApp } from '../fixtures/local-app';

test.use({ reducedMotion: 'reduce' });
const deferred = () => {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
};
async function setup(localApp: LocalApp, title = '启动时应加载的会话') {
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  return localApp.request('/conversations', 'POST', { title, data: createWorkspace() });
}
function writes(page: Page) {
  const captured: { method: string; path: string }[] = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/conversations') && ['POST', 'PUT'].includes(request.method())) captured.push({ method: request.method(), path });
  });
  return captured;
}

test('慢加载期间鼠标、Enter 和提交处理器均不另建会话，加载后显式发送到原会话', async ({ page, localApp }) => {
  const original = await setup(localApp);
  const started = deferred(), release = deferred();
  let reads = 0;
  const mutations = writes(page);
  await page.route('**/api/conversations/' + original.id, async route => {
    if (route.request().method() !== 'GET') return route.continue();
    reads++;
    const response = await route.fetch();
    started.resolve();
    await release.promise;
    await route.fulfill({ response });
  });
  try {
    await page.goto('/');
    await expect(page.locator('.startup-intro')).toHaveCount(0); await started.promise;
    const input = page.getByLabel('旅行对话输入');
    await input.fill('你好，加载后再发送');
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeDisabled();
    await input.press('Enter');
    await page.locator('form.chat-composer').dispatchEvent('submit');
    await page.getByRole('button', { name: '旅程', exact: true }).click();
    await page.getByLabel('会话名称').fill('加载中不能创建');
    await page.getByLabel('会话名称').press('Enter');
    await expect(page.getByRole('button', { name: '创建本地会话' })).toBeDisabled();
    await page.getByRole('button', { name: '关闭窗口' }).click();
    expect(mutations).toEqual([]);
    release.resolve();
    await expect(page.locator('.trip-title')).toHaveText(original.title);
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled();
    await expect(input).toHaveValue('你好，加载后再发送');
    expect(mutations).toEqual([]);
    expect(await localApp.request('/conversations/' + original.id)).toEqual(original);
    expect(reads).toBe(1); // StrictMode effect replay shares the same in-flight connection.
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
    await expect(page.locator('.message--assistant').last()).toContainText('你好');
    expect(mutations).toEqual([{ method: 'POST', path: '/api/conversations/' + original.id + '/messages' }]);
    const saved = await localApp.request('/conversations/' + original.id);
    expect(saved.data.messages.filter((message: { role: string }) => message.role === 'user')).toHaveLength(1);
    await page.screenshot({ path: 'artifacts/d3-startup-loaded.png', fullPage: true });
  } finally { release.resolve(); }
});

for (const failure of ['read', 'format'] as const) test(`首次会话 ${failure} 失败不当作空白，重试完成才发送且保留输入`, async ({ page, localApp }) => {
  const original = await setup(localApp);
  let failing = true, recovered = false;
  const retryStarted = deferred(), release = deferred();
  const mutations = writes(page);
  await page.route('**/api/conversations/' + original.id, async route => {
    if (route.request().method() !== 'GET') return route.continue();
    if (recovered) return route.continue();
    if (failing) return route.fulfill(failure === 'read'
      ? { status: 503, json: { detail: { message: '固定离线读取失败，请重试。' } } }
      : { json: { ...original, data: { ...original.data, schema: 99 } } });
    retryStarted.resolve(); await release.promise;
    recovered = true;
    await route.fulfill({ json: original });
  });
  try {
    await page.goto('/');
    await expect(page.locator('.startup-intro')).toHaveCount(0);
    const input = page.getByLabel('旅行对话输入');
    await input.fill('你好，重试后仍保留这条输入');
    await page.getByRole('button', { name: '设置与更新' }).click();
    await expect(page.getByRole('dialog')).toContainText(failure === 'read' ? '固定离线读取失败' : '本地会话格式不兼容');
    await page.getByRole('button', { name: '关闭窗口' }).click();
    await input.press('Enter');
    await page.locator('form.chat-composer').dispatchEvent('submit');
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeDisabled();
    expect(mutations).toEqual([]);
    await page.getByRole('button', { name: '设置与更新' }).click();
    failing = false;
    await page.getByRole('button', { name: '重新连接服务', exact: true }).click();
    await retryStarted.promise;
    await page.getByRole('button', { name: '关闭窗口' }).click();
    await expect(input).toHaveValue('你好，重试后仍保留这条输入');
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeDisabled();
    await page.locator('form.chat-composer').dispatchEvent('submit');
    expect(mutations).toEqual([]);
    release.resolve();
    await expect(page.locator('.trip-title')).toHaveText(original.title);
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled();
    await input.press('Enter');
    await expect(page.locator('.message--assistant').last()).toContainText('你好');
    expect(mutations).toEqual([{ method: 'POST', path: '/api/conversations/' + original.id + '/messages' }]);
  } finally { release.resolve(); }
});

test('会话列表为空时等待加载完成，首次发送只创建一个会话', async ({ page, localApp }) => {
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const started = deferred(), release = deferred();
  const mutations = writes(page);
  await page.route('**/api/conversations', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    started.resolve(); await release.promise;
    await route.fulfill({ json: [] });
  });
  try {
    await page.goto('/');
    await expect(page.locator('.startup-intro')).toHaveCount(0); await started.promise;
    const input = page.getByLabel('旅行对话输入');
    await input.fill('你好，新会话');
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeDisabled();
    await page.locator('form.chat-composer').dispatchEvent('submit');
    expect(mutations).toEqual([]);
    release.resolve();
    await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled();
    await expect(input).toHaveValue('你好，新会话');
    await input.press('Enter');
    await expect(page.locator('.message--assistant').last()).toContainText('你好');
    expect(mutations).toHaveLength(2);
    expect(mutations[0]).toEqual({ method: 'POST', path: '/api/conversations' });
    expect(mutations[1].path).toMatch(/^\/api\/conversations\/[^/]+\/messages$/);
  } finally { release.resolve(); }
});

test('启动迟到响应不覆盖用户已选中的历史会话', async ({ page, localApp }) => {
  const chosen = await setup(localApp, '用户明确选中的会话');
  const latest = await localApp.request('/conversations', 'POST', { title: '启动默认最近会话', data: createWorkspace() });
  const started = deferred(), release = deferred(), delivered = deferred();
  const mutations = writes(page);
  await page.route('**/api/conversations/' + latest.id, async route => {
    started.resolve(); await release.promise;
    await route.fulfill({ json: latest }); delivered.resolve();
  });
  try {
    await page.goto('/');
    await expect(page.locator('.startup-intro')).toHaveCount(0); await started.promise;
    await page.getByRole('button', { name: '历史旅行', exact: true }).click();
    await page.getByRole('button', { name: new RegExp(chosen.title) }).click();
    await expect(page.locator('.trip-title')).toHaveText(chosen.title);
    release.resolve(); await delivered.promise;
    await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
    await expect(page.locator('.trip-title')).toHaveText(chosen.title);
    expect(mutations).toEqual([]);
  } finally { release.resolve(); }
});
