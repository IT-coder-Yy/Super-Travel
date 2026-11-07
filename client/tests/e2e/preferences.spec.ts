import { readFile, writeFile, readdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect } from '../fixtures/local-app';

const defaults = { schema: 1, enabled: false, budgetAmountFen: null, pace: null, dietaryNotes: '', companionMode: null };
const chosen = { ...defaults, enabled: true, budgetAmountFen: 300000, pace: 'relaxed', dietaryNotes: '清淡，不吃辣', companionMode: 'family' };
const section = (page: Page) => page.getByRole('region', { name: '常用旅行偏好', exact: true });
async function open(page: Page) {
  await page.getByRole('button', { name: '设置与更新', exact: true }).click();
  await expect(section(page).getByLabel('启用常用旅行偏好')).toBeEnabled();
}
async function save(page: Page) {
  const response = page.waitForResponse(r => r.url().endsWith('/travel-preferences') && r.request().method() === 'PUT');
  await section(page).getByRole('button', { name: '保存旅行偏好', exact: true }).click();
  expect((await response).ok()).toBe(true);
  await expect(section(page).getByRole('status')).toContainText('旅行偏好已保存');
}

test.use({ reducedMotion: 'reduce' });
test.beforeEach(async ({ page, localApp }) => {
  await localApp.reset();
  await localApp.request('/travel-preferences', 'PUT', defaults);
  await page.goto('/');
  await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
});
test.afterEach(async ({ localApp }) => {
  await localApp.reset();
  await localApp.request('/travel-preferences', 'PUT', defaults);
});

test('偏好显式保存，重开重启保留；停用与确认清空，桌面窄窗可用', async ({ page, localApp }) => {
  await open(page);
  const form = section(page);
  await expect(form.getByLabel('全程预算目标（元）')).toHaveValue('');
  await expect(form.getByLabel('旅行节奏', { exact: true })).toHaveValue('');
  await expect(form.getByLabel('启用常用旅行偏好')).not.toBeChecked();
  await form.getByLabel('全程预算目标（元）').fill('3000');
  expect((await localApp.request('/travel-preferences')).preferences).toEqual(defaults);
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await open(page);
  await expect(form.getByLabel('全程预算目标（元）')).toHaveValue('');
  await form.getByLabel('全程预算目标（元）').fill('3000');
  await form.getByLabel('启用常用旅行偏好').check();
  await form.getByLabel('旅行节奏', { exact: true }).selectOption('relaxed');
  await form.getByLabel('同行方式', { exact: true }).selectOption('family');
  await form.getByLabel('饮食偏好', { exact: true }).fill(chosen.dietaryNotes);
  await save(page);
  expect((await localApp.request('/travel-preferences')).preferences).toEqual(chosen);
  expect(JSON.parse(await readFile(resolve(localApp.dataDir, 'travel-preferences.json'), 'utf8'))).toEqual(chosen);
  await form.getByRole('heading').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'artifacts/b5-preferences-1440.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await form.getByRole('heading').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'artifacts/b5-preferences-390.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await form.getByRole('button', { name: '清空旅行偏好', exact: true }).click();
  await expect(form.getByRole('button', { name: '取消清空', exact: true })).toBeFocused();
  await page.screenshot({ path: 'artifacts/b5-preferences-actions-390.png', fullPage: true });
  await form.getByRole('button', { name: '取消清空', exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 960 });
  await localApp.restart();
  await page.reload();
  await open(page);
  await expect(form.getByLabel('全程预算目标（元）')).toHaveValue('3000');
  await expect(form.getByLabel('饮食偏好', { exact: true })).toHaveValue(chosen.dietaryNotes);
  await form.getByLabel('启用常用旅行偏好').uncheck();
  expect((await localApp.request('/travel-preferences')).preferences.enabled).toBe(true);
  await save(page);
  expect((await localApp.request('/travel-preferences')).preferences).toEqual({ ...chosen, enabled: false });
  await form.getByRole('button', { name: '清空旅行偏好', exact: true }).click();
  await expect(form.getByRole('button', { name: '取消清空', exact: true })).toBeFocused();
  await form.getByRole('button', { name: '取消清空', exact: true }).click();
  expect((await localApp.request('/travel-preferences')).preferences.budgetAmountFen).toBe(300000);
  await form.getByRole('button', { name: '清空旅行偏好', exact: true }).click();
  await form.getByRole('button', { name: '确认清空偏好', exact: true }).click();
  await expect(form.getByRole('status')).toContainText('已清空并停用');
  expect((await localApp.request('/travel-preferences')).preferences).toEqual(defaults);
  await expect(form.getByRole('button', { name: '清空旅行偏好', exact: true })).toBeFocused();
});

test('金额校验、零预算与精确分；保存失败保留输入和旧配置并可重试', async ({ page, localApp }) => {
  await open(page);
  const form = section(page), budget = form.getByLabel('全程预算目标（元）');
  for (const invalid of ['-1', '1.234', '1e3', '1000000.01']) {
    await budget.fill(invalid);
    await form.getByRole('button', { name: '保存旅行偏好', exact: true }).click();
    await expect(budget).toHaveAttribute('aria-invalid', 'true');
    await expect(budget).toBeFocused();
    expect((await localApp.request('/travel-preferences')).preferences).toEqual(defaults);
  }
  await budget.fill('0'); await save(page);
  expect((await localApp.request('/travel-preferences')).preferences.budgetAmountFen).toBe(0);
  await budget.fill('12.34');
  await page.route('**/api/travel-preferences', route => route.request().method() === 'PUT' ? route.fulfill({ status: 503, json: { detail: { message: '固定测试：磁盘暂不可写' } } }) : route.continue());
  await form.getByRole('button', { name: '保存旅行偏好', exact: true }).click();
  await expect(form.getByRole('alert')).toContainText('输入仍保留');
  await expect(budget).toHaveValue('12.34');
  expect((await localApp.request('/travel-preferences')).preferences.budgetAmountFen).toBe(0);
  await page.unroute('**/api/travel-preferences');
  await save(page);
  expect((await localApp.request('/travel-preferences')).preferences.budgetAmountFen).toBe(1234);
});

test('读取失败可重试，损坏文件明确提示且显式修复保留原件', async ({ page, localApp }) => {
  await page.route('**/api/travel-preferences', route => route.fulfill({ status: 503, json: { detail: { message: '固定测试读取失败' } } }));
  await page.getByRole('button', { name: '设置与更新', exact: true }).click();
  await expect(section(page).getByRole('button', { name: '重新读取旅行偏好', exact: true })).toBeVisible();
  await page.unroute('**/api/travel-preferences');
  await section(page).getByRole('button', { name: '重新读取旅行偏好', exact: true }).click();
  await expect(section(page).getByLabel('启用常用旅行偏好')).toBeEnabled();
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  const path = resolve(localApp.dataDir, 'travel-preferences.json');
  const corrupt = '{固定测试损坏的偏好原件';
  await writeFile(path, corrupt, 'utf8');
  await open(page);
  await expect(section(page).getByRole('alert')).toContainText('原件已保留');
  expect(await readFile(path, 'utf8')).toBe(corrupt);
  await section(page).getByLabel('饮食偏好', { exact: true }).fill('清淡');
  await save(page);
  expect((await localApp.request('/travel-preferences')).preferences.dietaryNotes).toBe('清淡');
  const originals = (await readdir(localApp.dataDir)).filter(name => name.startsWith('travel-preferences.json.corrupt-'));
  expect(originals.length).toBeGreaterThan(0);
  expect(await Promise.all(originals.map(name => readFile(resolve(localApp.dataDir, name), 'utf8')))).toContain(corrupt);
});

test('运行中保存偏好不会中断任务或改变旧摘要，下个任务采用新快照', async ({ page, localApp }) => {
  await localApp.request('/travel-preferences', 'PUT', chosen);
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const document = await localApp.request('/conversations', 'POST', { title: '偏好快照固定验收', data: { schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [] } });
  await page.reload();
  await expect(page.locator('.trip-title')).toHaveText(document.title);
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '下一站，想去哪里？' })).toBeVisible();
  await page.getByLabel('旅行对话输入').fill('偏好验收，查询杭州天气');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  const progress = page.getByLabel('Agent 任务进度');
  await expect(progress).toContainText('本次规划偏好：预算目标：3000元（常用偏好）；节奏：慢节奏（常用偏好）');
  await expect.poll(async () => (await localApp.request('/test-fixture/state')).pending).toBe(1);
  const before = await localApp.request('/conversations/' + document.id);
  await open(page);
  await section(page).getByLabel('全程预算目标（元）').fill('5000');
  await section(page).getByLabel('旅行节奏', { exact: true }).selectOption('packed');
  await save(page);
  const after = await localApp.request('/conversations/' + document.id);
  expect(after.data.agent.run_id).toBe(before.data.agent.run_id);
  expect(after.data.agent.status).toBe('running');
  expect(after.data.agent.preference_summary).toBe(before.data.agent.preference_summary);
  expect(after.data.days).toEqual(before.data.days);
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await expect(progress).toContainText('预算目标：3000元（常用偏好）；节奏：慢节奏（常用偏好）');
  await progress.getByRole('button', { name: '取消任务', exact: true }).click();
  await localApp.request('/test-fixture/release', 'POST');
  await expect(progress).toContainText('任务已取消');
  await page.getByLabel('旅行对话输入').fill('偏好验收，再次查询杭州天气');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(progress).toContainText('本次规划偏好：预算目标：5000元（常用偏好）；节奏：紧凑（常用偏好）');
  await expect(page.getByRole('heading', { name: '下一站，想去哪里？' })).toHaveCount(0);
  await page.screenshot({ path: 'artifacts/b5-task-preferences.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(progress).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'artifacts/b5-task-preferences-390.png', fullPage: true });
  await progress.getByRole('button', { name: '取消任务', exact: true }).click();
  await localApp.request('/test-fixture/release', 'POST');
});
