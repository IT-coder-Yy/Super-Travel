import type { Page } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { test, expect, type LocalApp } from './fixtures/local-app';

const KEY = 'fixture-key-not-a-real-model-credential';
async function configure(page: Page, localApp: LocalApp) {
  await page.getByRole('button', { name: '设置与更新', exact: true }).click();
  await page.getByLabel('模型服务地址', { exact: true }).fill('https://fixture.invalid/v1');
  await page.getByLabel('模型名称', { exact: true }).fill('fixture-model');
  const key = page.getByLabel('模型 API Key', { exact: true });
  await expect(key).toHaveAttribute('type', 'password');
  await key.fill(KEY);
  await page.getByRole('button', { name: '保存模型设置', exact: true }).click();
  await expect(page.getByText('模型配置已保存。', { exact: true })).toBeVisible();
  await expect(key).toHaveValue('');
  const settings = await localApp.request('/model/settings');
  expect(settings.hasKey).toBe(true);
  expect(JSON.stringify(settings)).not.toContain(KEY);
}
async function send(page: Page, text: string) {
  await page.getByLabel('旅行对话输入').fill(text);
  const [response] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/messages') && response.request().method() === 'POST'),
    page.getByRole('button', { name: '发送消息', exact: true }).click(),
  ]);
  expect(response.status()).toBe(202);
  await expect(page.getByLabel('旅行对话输入')).toHaveValue('');
  return response.json();
}

test.describe('双 Agent UI（本地固定假模型/工具，无真实API费用）', () => {
  test.use({ reducedMotion: 'reduce' });
  test.beforeEach(async ({ page, localApp }, info) => {
    await localApp.reset();
    await page.route('https://thumb.wikimedia.org/**/960px-Sunset_over_the_Zigzag_Bridge*.jpg', route => route.fulfill({ path: fileURLToPath(new URL('./fixtures/west-lake-sunset.jpg', import.meta.url)), contentType: 'image/jpeg' }));
    await page.route('https://thumb.wikimedia.org/**/500px-West_lake_twilight.jpg', route => route.fulfill({ path: fileURLToPath(new URL('./fixtures/west-lake-twilight.jpg', import.meta.url)), contentType: 'image/jpeg' }));
    await page.goto('/');
    await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '旅程', exact: true }).click();
    const title = `固定Agent测试 ${info.testId.slice(-6)} ${Date.now()}`;
    await page.getByLabel('会话名称').fill(title);
    await page.getByRole('button', { name: '创建本地会话', exact: true }).click();
    await expect(page.locator('.trip-title')).toHaveText(title);
    await page.getByRole('button', { name: '关闭提示', exact: true }).click();
  });

  test('显示计划，普通聊天不打断，取消后迟到工具结果不会覆盖', async ({ page, localApp }, info) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await configure(page, localApp);
    await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
    const document = await send(page, '查询杭州天气');
    const progress = page.getByLabel('Agent 任务进度');
    await expect(progress).toContainText('执行 Agent 正在查询');
    await expect.poll(() => localApp.request('/test-fixture/state')).toMatchObject({ pending: 1 });
    await progress.getByText(/查看计划/).click();
    await expect(progress.getByRole('listitem')).toHaveText('查询杭州天气（固定测试工具）');
    const runId = (await localApp.request(`/conversations/${document.id}`)).data.agent.run_id;
    await send(page, '你好');
    await expect(page.getByText('固定测试回复：你好，旅行者。', { exact: true })).toBeVisible();
    await expect(progress).toContainText('执行 Agent 正在查询');
    expect((await localApp.request(`/conversations/${document.id}`)).data.agent.run_id).toBe(runId);
    expect((await localApp.request('/test-fixture/state')).pending).toBe(1);
    await page.screenshot({ path: info.outputPath('active-plan-and-chat.png') });

    await page.getByRole('button', { name: '取消任务', exact: true }).click();
    await expect.poll(async () => (await localApp.request('/test-fixture/state')).cancelled).toBeGreaterThan(0);
    await localApp.request('/test-fixture/release', 'POST');
    await expect(progress).toContainText('任务已取消');
    await expect.poll(async () => (await localApp.request('/test-fixture/state')).finished).toBeGreaterThan(0);
    const saved = await localApp.request(`/conversations/${document.id}`);
    expect(saved.data.agent.status).toBe('cancelled');
    expect(saved.data.agent.run_id).toBe(runId);
    expect(JSON.stringify(saved)).not.toContain('FIXTURE_LATE_RESULT_MUST_NOT_APPEAR');
    expect(saved.data.messages.filter((message: { content: string }) => message.content === '你好')).toHaveLength(1);
    await page.reload();
    await expect(page.getByLabel('Agent 任务进度')).toContainText('任务已取消');
    await page.getByRole('button', { name: /旅伴对话/ }).click();
    await expect(page.getByText('固定测试回复：你好，旅行者。', { exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('后端重启后刷新保留消息、显示中断且不自动续跑', async ({ page, localApp }, info) => {
    await configure(page, localApp);
    await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
    const document = await send(page, '查询杭州天气，等待测试重启');
    await expect.poll(() => localApp.request('/test-fixture/state')).toMatchObject({ pending: 1 });
    await expect(page.getByLabel('Agent 任务进度')).toContainText('执行 Agent 正在查询');
    await localApp.restart();
    await page.reload();
    await expect(page.getByLabel('Agent 任务进度')).toContainText('上次任务已中断');
    await expect(page.getByText('可重新发送需求继续，已保存的内容仍然保留。', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: /旅伴对话/ }).click();
    await expect(page.getByText('查询杭州天气，等待测试重启', { exact: true })).toBeVisible();
    const saved = await localApp.request(`/conversations/${document.id}`);
    expect(saved.data.agent.status).toBe('interrupted');
    expect(saved.data.agentPending).toBe(false);
    expect((await localApp.request('/test-fixture/state')).pending).toBe(0);
    expect((await localApp.request('/model/settings')).hasKey).toBe(false);
    expect(await readFile(resolve(localApp.dataDir, 'conversations', `${document.id}.json`), 'utf8')).not.toContain(KEY);
    await page.screenshot({ path: info.outputPath('interrupted-after-restart.png') });
  });

  test('模型设置不回显密钥，紧凑窗口可滚动且无横向溢出', async ({ page, localApp }, info) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await configure(page, localApp);
    await page.getByRole('button', { name: '测试模型连接', exact: true }).click();
    await expect(page.getByText('模型连接成功。', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '检查工具服务', exact: true }).click();
    await expect(page.getByText('工具服务已连接，可用工具 1 个。', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
    await page.getByRole('button', { name: '设置与更新', exact: true }).click();
    const key = page.getByLabel('模型 API Key', { exact: true });
    await expect(key).toHaveValue('');
    await expect(key).toHaveAttribute('placeholder', '已配置；地址不变时留空保留');
    await key.scrollIntoViewIfNeeded();
    const bounds = await page.getByRole('dialog').evaluate(dialog => ({ width: dialog.clientWidth, scrollWidth: dialog.scrollWidth, right: dialog.getBoundingClientRect().right, viewport: innerWidth, pageWidth: document.documentElement.scrollWidth }));
    expect(bounds.scrollWidth).toBeLessThanOrEqual(bounds.width);
    expect(bounds.right).toBeLessThanOrEqual(bounds.viewport);
    expect(bounds.pageWidth).toBeLessThanOrEqual(bounds.viewport);
    await expect(page.getByRole('button', { name: '保存模型设置', exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath('model-settings-compact.png') });
  });
});
