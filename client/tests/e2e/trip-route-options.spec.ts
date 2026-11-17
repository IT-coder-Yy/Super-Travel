import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { isWorkspaceData } from '../../src/types';
import { clearInvalidRoutes } from '../../src/tripDetails';
import { test, expect, type LocalApp } from '../fixtures/local-app';

test.use({ reducedMotion: 'reduce' });
test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.fulfill({
    contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>',
  }));
});
test.afterEach(async ({ localApp }) => { await localApp.request('/test-fixture/routes', 'POST', { profile: null }); });

async function setup(page: Page, localApp: LocalApp) {
  await localApp.reset();
  await localApp.request('/test-fixture/routes', 'POST', { profile: null });
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const created = await localApp.request('/conversations', 'POST', { title: '公交多方案离线回归', data: { schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [] } });
  const read = () => localApp.request('/conversations/' + created.id);
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  // Wait for the intended conversation, not just the connection or the intro overlay.
  await expect(page.locator('.trip-title')).toHaveText('公交多方案离线回归');
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByRole('button', { name: '编辑当天行程', exact: true })).toBeEnabled();
  const original = await read();
  original.data.trip.transportMode = 'transit';
  for (const day of original.data.days) {
    day.activities[1].incomingRoute = { fromPlaceId: day.activities[0].place.id, toPlaceId: day.activities[1].place.id,
      mode: 'transit', status: 'verified', durationMinutes: 13, distanceMeters: 1200, provider: 'baidu', queriedAt: new Date().toISOString() };
  }
  const first = original.data.days[0].activities[0];
  first.cost = { amountFen: 4250, currency: 'CNY', source: 'user' };
  original.data.favorites = [first.id];
  await writeFile(resolve(localApp.dataDir, 'conversations', created.id + '.json'), JSON.stringify(original), 'utf8');
  await page.reload();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'options' });
  return { read, original, calls: async () => (await localApp.request('/test-fixture/state')).route_calls as unknown[] };
}

const editor = (page: Page) => page.getByRole('dialog', { name: '编辑当天行程', exact: true });
const comparison = (page: Page) => page.getByRole('dialog', { name: '比较公交方案', exact: true });
async function open(page: Page) {
  await page.getByRole('button', { name: /^(编辑当天行程|继续编辑草稿)$/ }).click();
  await editor(page).getByRole('button', { name: '比较公交方案', exact: true }).click();
  await expect(comparison(page)).toBeVisible();
}
async function compare(page: Page) {
  await page.getByRole('button', { name: '查询公交方案', exact: true }).click();
  await expect(comparison(page).getByRole('radio')).toHaveCount(3);
}

test('显式一次查询；键盘选第二方案、草稿应用、重启导出及恢复', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await setup(page, localApp);
  await open(page);
  expect(await trip.calls()).toHaveLength(0);
  await expect(page.getByRole('button', { name: '查询公交方案', exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(editor(page).getByRole('button', { name: '比较公交方案' })).toBeFocused();
  await editor(page).getByRole('button', { name: '比较公交方案' }).press('Enter');
  const before = await trip.read();
  await compare(page);
  await expect(comparison(page).getByRole('radio').first()).toBeFocused();
  expect(await trip.calls()).toHaveLength(1);
  expect(await trip.read()).toEqual(before);
  await page.keyboard.press('ArrowDown');
  await expect(comparison(page).getByRole('radio', { name: /方案 2/ })).toBeChecked();
  await expect(comparison(page)).toContainText('参考总票价 ¥0.00（仅供参考，以实际结果为准）');
  await expect(comparison(page).locator('.route-taxi-reference')).toHaveCount(1);
  await comparison(page).locator('.route-option').nth(1).getByText('线路与上下车站', { exact: true }).click();
  await expect(comparison(page).locator('.route-option').nth(1)).toContainText('离线方案2号线𠮷🏞️');
  for (const [width, height] of [[1440, 960], [960, 640], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await expect(page.getByRole('button', { name: '保存方案到草稿' })).toBeInViewport();
    await expect(comparison(page).getByRole('heading', { name: '比较公交方案' })).toBeInViewport();
    expect(await comparison(page).evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `artifacts/d2-options-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  await expect(editor(page).getByRole('button', { name: '比较公交方案' })).toBeFocused();
  const pending = await trip.read();
  expect(pending.data.days).toEqual(trip.original.data.days);
  expect(isWorkspaceData(pending.data)).toBe(true);
  for (const patch of [{ selection: 'first' }, { selection: true }, { mode: 'walking' }, { status: 'unknown' }]) {
    const bad = structuredClone(pending.data);
    Object.assign(bad.draft[0].activities[1].incomingRoute, patch);
    expect(isWorkspaceData(bad)).toBe(false);
  }
  const moved = structuredClone(pending.data.draft);
  moved[0].day = 3;
  clearInvalidRoutes(moved, pending.data.draft);
  expect(moved[0].activities[1].incomingRoute).toBeUndefined();
  expect(pending.data.draft[0].activities[1].incomingRoute).toMatchObject({ selection: 'user', durationMinutes: 20, transit: { fare: { amountFen: 0 } } });
  expect(await trip.calls()).toHaveLength(1);
  await page.getByLabel('备注', { exact: true }).first().fill('选用后保留的备注𠮷🏞️');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.activities .trip-leg').first()).toContainText('公共交通约 20 分钟');
  await expect(page.getByLabel('行程费用')).toContainText('¥42.50');
  const applied = await trip.read();
  expect(applied.data.days[0].activities[0].description).toBe('选用后保留的备注𠮷🏞️');
  expect(applied.data.favorites).toEqual(trip.original.data.favorites);
  expect(await trip.calls()).toHaveLength(1);
  const exported = await localApp.request('/conversations/' + applied.id + '/trip/export', 'POST', { expected_revision: applied.revision });
  expect(exported.markdown).toContain('用户选用方案');
  expect(exported.markdown).toContain('离线方案2号线𠮷🏞️');
  await localApp.restart();
  await page.reload();
  await expect(page.locator('.activities .trip-leg').first()).toContainText('公共交通约 20 分钟');
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect(page.locator('.activities .trip-leg').first()).toContainText('公共交通约 13 分钟');
  expect((await trip.read()).data.days).toEqual(trip.original.data.days);
});

test('一条、空结果、查询失败与保存失败均不伪造或覆盖草稿', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page);
  const before = await trip.read();
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'options-one' });
  await page.getByRole('button', { name: '查询公交方案', exact: true }).click();
  await expect(comparison(page).getByRole('radio')).toHaveCount(1);
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'options-empty' });
  await page.getByRole('button', { name: '重新查询方案' }).click();
  await expect(comparison(page)).toContainText('本次没有可选方案');
  await expect(page.getByRole('button', { name: '保存方案到草稿' })).toBeDisabled();
  await page.screenshot({ path: 'artifacts/d2-empty.png', fullPage: true });
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'quota' });
  await page.getByRole('button', { name: '重新查询方案' }).click();
  await expect(comparison(page)).toContainText('额度或权限不可用');
  expect(await trip.calls()).toHaveLength(1);
  expect(await trip.read()).toEqual(before);
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'options' });
  await page.getByRole('button', { name: '重新查询方案' }).click();
  await comparison(page).getByRole('radio', { name: /方案 3/ }).check();
  await page.route('**/trip/routes/select', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定离线保存失败，请重试。' } }) }), { times: 1 });
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(comparison(page).getByRole('alert')).toBeFocused();
  await expect(comparison(page).getByRole('radio', { name: /方案 3/ })).toBeChecked();
  expect(await trip.read()).toEqual(before);
  await page.screenshot({ path: 'artifacts/d2-save-failure.png', fullPage: true });
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  expect((await trip.read()).data.draft[0].activities[1].incomingRoute.durationMinutes).toBe(30);
  expect(await trip.calls()).toHaveLength(1);
});

test('比较票据在重新查询或并发修订后失效，保留新修订', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page); await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  const current = await trip.read();
  await localApp.request('/conversations/' + current.id + '/trip/routes/options', 'POST', { expected_revision: current.revision, activity_id: current.data.draft[0].activities[1].id });
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(comparison(page).getByRole('alert')).toContainText('比较结果已失效');
  await expect(comparison(page).getByRole('radio')).toHaveCount(0);
  expect(await trip.read()).toEqual(current);
  await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 3/ }).check();
  const data = structuredClone(current.data);
  data.draft[0].activities[0].description = '另一个窗口的新备注';
  const newer = await localApp.request('/conversations/' + current.id, 'PUT', { title: current.title, data, expected_revision: current.revision });
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(comparison(page).getByRole('alert')).toContainText('重新加载');
  expect(await trip.read()).toEqual(newer);
});

test('较慢方案先保存草稿，修正时间后应用且不重查选用方案', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'options-conflict' });
  await open(page); await compare(page);
  await expect(comparison(page).locator('.route-option').nth(1)).toContainText('冲突解决前无法应用');
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  await expect(page.getByRole('button', { name: '应用修改', exact: true })).toBeDisabled();
  await page.getByLabel('时间', { exact: true }).nth(1).fill('11:30');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await trip.read()).data.days[0].activities[1].incomingRoute).toMatchObject({ durationMinutes: 75, selection: 'user' });
  expect(await trip.calls()).toHaveLength(1);
});

test('取消等待后迟到结果不回填，也不会写入草稿', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page);
  const before = await trip.read();
  let release!: () => void;
  let captured!: () => void;
  const started = new Promise<void>(resolve => { captured = resolve; });
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/trip/routes/options', async route => { captured(); await pending; await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ revision: before.revision, options: [] }) }).catch(() => undefined); }, { times: 1 });
  await page.getByRole('button', { name: '查询公交方案', exact: true }).click();
  await started;
  await expect(comparison(page)).toContainText('正在查询本段公交方案');
  await page.getByRole('button', { name: '返回编辑', exact: true }).click();
  release();
  await expect(editor(page)).toBeVisible();
  await expect(editor(page).getByRole('button', { name: '比较公交方案' })).toBeFocused();
  expect(await trip.read()).toEqual(before);
  expect(await trip.calls()).toHaveLength(0);
});

test('过期用户方案要求重新选择，不展示旧报价且不自动查图', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page); await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  const selected = await trip.read();
  selected.data.draft[0].activities[1].incomingRoute.queriedAt = new Date(Date.now() - 25 * 3600_000).toISOString();
  await writeFile(resolve(localApp.dataDir, 'conversations', selected.id + '.json'), JSON.stringify(selected), 'utf8');
  await page.reload();
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await expect(editor(page)).toContainText('用户选用方案已过期');
  await expect(page.getByRole('button', { name: '应用修改', exact: true })).toBeDisabled();
  await expect(editor(page).locator('.trip-leg')).not.toContainText('参考总票价 ¥0.00');
  expect(await trip.calls()).toHaveLength(1);
  await editor(page).getByRole('button', { name: '比较公交方案' }).click();
  await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 3/ }).check();
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(page.getByRole('button', { name: '应用修改', exact: true })).toBeEnabled();
});


test('更名后未定位的接驳不允许比较', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByLabel('地点', { exact: true }).nth(1).fill('尚未定位的新地点');
  await expect(editor(page).getByRole('button', { name: '比较公交方案' })).toBeDisabled();
  await expect(editor(page)).toContainText('地点名称已改，请重新定位。');
  expect(await trip.calls()).toHaveLength(0);
});
