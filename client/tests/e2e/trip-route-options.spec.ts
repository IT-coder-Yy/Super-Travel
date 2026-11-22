import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { isWorkspaceData, type Conversation } from '../../src/types';
import { clearInvalidRoutes } from '../../src/tripDetails';
import { test, expect, type LocalApp } from '../fixtures/local-app';
import { setLegMode } from '../fixtures/leg-mode';

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

async function concurrentEdit(localApp: LocalApp, current: Conversation, selectedDay = current.data.selectedDay): Promise<Conversation> {
  const data = structuredClone(current.data);
  data.selectedDay = selectedDay;
  data.draft![0]!.activities[0]!.description = '另一个窗口的新备注';
  return localApp.request('/conversations/' + current.id, 'PUT', { title: current.title, data, expected_revision: current.revision });
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

test('票据失效可直接重查，保存遇到并发修订则重读后才能继续选用', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page); await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  const current = await trip.read();
  await localApp.request('/conversations/' + current.id + '/trip/routes/options', 'POST', { expected_revision: current.revision, activity_id: current.data.draft[0].activities[1].id });
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(comparison(page).getByRole('alert')).toContainText('比较结果已失效');
  await expect(comparison(page).getByRole('radio')).toHaveCount(0);
  await expect(comparison(page).getByRole('button', { name: '重新读取会话' })).toHaveCount(0);
  expect(await trip.read()).toEqual(current);
  await compare(page);
  const choice = comparison(page).getByRole('radio', { name: /方案 2/ });
  await choice.check();
  const newer = await concurrentEdit(localApp, current);
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(comparison(page).getByRole('alert')).toContainText('会话已被更新');
  await expect(choice).toBeChecked();
  await expect(choice).toBeDisabled();
  const calls = await trip.calls();
  await comparison(page).getByRole('button', { name: '重新读取会话', exact: true }).click();
  await comparison(page).getByRole('button', { name: '保留本地输入' }).click();
  await expect(choice).toBeChecked();
  expect(await trip.read()).toEqual(newer);
  expect(await trip.calls()).toEqual(calls);
  await comparison(page).getByRole('button', { name: '返回编辑' }).click();
  await expect(editor(page).getByRole('button', { name: '比较公交方案' })).toBeDisabled();
  await expect(editor(page).getByRole('button', { name: '切换交通方式' })).toBeDisabled();
  await editor(page).getByRole('button', { name: '重新读取会话' }).click();
  await expect(page.getByLabel('备注', { exact: true }).first()).toHaveValue('另一个窗口的新备注');
  expect(await trip.read()).toEqual(newer);
  expect(await trip.calls()).toEqual(calls);
  await editor(page).getByRole('button', { name: '比较公交方案' }).click();
  await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  const selection = page.waitForRequest(request => request.url().endsWith('/trip/routes/select'));
  await comparison(page).getByRole('button', { name: '保存方案到草稿' }).click();
  expect((await selection).postDataJSON().expected_revision).toBe(newer.revision);
  await expect(editor(page)).toBeVisible();
  const saved = await trip.read();
  expect(saved.revision).toBe(newer.revision + 1);
  expect(saved.data.draft[0].activities[0]).toEqual(newer.data.draft![0]!.activities[0]);
  expect(saved.data.draft[0].activities[1].incomingRoute).toMatchObject({ selection: 'user', durationMinutes: 20 });
  expect(saved.data.days).toEqual(newer.data.days);
  expect(saved.data.favorites).toEqual(newer.data.favorites);
});

for (const selected of [false, true]) test(`${selected ? '选中后再查' : '首次比较'}遇到并发修订，重读后只查询最新日期的相邻活动`, async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page);
  const choice = comparison(page).getByRole('radio', { name: /方案 2/ });
  if (selected) {
    await compare(page);
    await choice.check();
  }
  const calls = await trip.calls();
  const newer = await concurrentEdit(localApp, await trip.read(), 2);
  await comparison(page).getByRole('button', { name: selected ? '重新查询方案' : '查询公交方案' }).click();
  await expect(comparison(page).getByRole('alert')).toContainText('会话已被更新');
  await comparison(page).getByRole('button', { name: '重新读取会话' }).click();
  if (selected) {
    await expect(choice).toBeChecked();
    await expect(choice).toBeDisabled();
    await comparison(page).getByRole('button', { name: '确认重新读取会话' }).click();
  }
  await expect(editor(page)).toBeVisible();
  expect(await trip.calls()).toEqual(calls);
  await editor(page).getByRole('button', { name: '比较公交方案' }).click();
  await expect(comparison(page)).toContainText('第 2 天');
  const query = page.waitForRequest(request => request.url().endsWith('/trip/routes/options'));
  await compare(page);
  expect((await query).postDataJSON()).toEqual({ expected_revision: newer.revision, activity_id: newer.data.draft![1]!.activities[1]!.id });
  expect(await trip.calls()).toHaveLength(calls.length + 1);
  expect(await trip.read()).toEqual(newer);
});

test('草稿保存冲突后，取消重读保留本地输入，确认后可用新修订继续编辑', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page);
  await page.getByRole('button', { name: '返回编辑' }).click();
  const newer = await concurrentEdit(localApp, await trip.read());
  const requests: string[] = [];
  page.on('request', request => {
    if (request.url().endsWith('/api/conversations/' + newer.id)) requests.push(request.method());
  });
  const note = editor(page).getByLabel('备注', { exact: true }).first();
  await note.fill('尚未保存的本地备注');
  const reload = editor(page).getByRole('button', { name: '重新读取会话', exact: true });
  await reload.click();
  await expect(editor(page).getByRole('status')).toContainText('替换本地未保存的输入');
  await editor(page).getByRole('button', { name: '保留本地输入' }).click();
  await expect(note).toHaveValue('尚未保存的本地备注');
  expect(requests).toEqual(['PUT']);
  expect(await trip.read()).toEqual(newer);
  await reload.click();
  await editor(page).getByRole('button', { name: '确认重新读取会话' }).click();
  await expect(note).toHaveValue('另一个窗口的新备注');
  expect(requests).toEqual(['PUT', 'GET']);
  await editor(page).getByRole('button', { name: '比较公交方案' }).click();
  await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  await comparison(page).getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  expect((await trip.read()).data.draft[0].activities[0]).toEqual(newer.data.draft![0]!.activities[0]);
});

async function holdReload(page: Page, id: string) {
  let captured!: () => void;
  const started = new Promise<void>(resolve => { captured = resolve; });
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let delivered!: () => void;
  const done = new Promise<void>(resolve => { delivered = resolve; });
  await page.route('**/api/conversations/' + id, async route => {
    const response = await route.fetch();
    captured();
    await held;
    try { await route.fulfill({ response }); } finally { delivered(); }
  }, { times: 1 });
  return { started, release, done };
}

test('关闭正在重读的比较窗口后，迟到响应不会替换同一会话的编辑内容', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page);
  const original = await trip.read();
  await concurrentEdit(localApp, original);
  await comparison(page).getByRole('button', { name: '查询公交方案' }).click();
  const held = await holdReload(page, original.id);
  await comparison(page).getByRole('button', { name: '重新读取会话' }).click();
  await held.started;
  try {
    await expect(comparison(page).getByRole('button', { name: '取消读取' })).toBeVisible();
    await comparison(page).getByRole('button', { name: '关闭窗口' }).click();
    await expect(editor(page).getByRole('button', { name: '重新读取会话', exact: true })).toBeEnabled();
  } finally { held.release(); }
  await held.done;
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
  const note = editor(page).getByLabel('备注', { exact: true }).first();
  await expect(note).toHaveValue(original.data.draft[0].activities[0].description);
  await editor(page).getByRole('button', { name: '重新读取会话' }).click();
  await expect(note).toHaveValue('另一个窗口的新备注');
});

test('取消重读并切换会话后，迟到的原会话不会替换当前行程', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page);
  const newer = await concurrentEdit(localApp, await trip.read());
  await comparison(page).getByRole('button', { name: '查询公交方案' }).click();
  const held = await holdReload(page, newer.id);
  await comparison(page).getByRole('button', { name: '重新读取会话' }).click();
  await held.started;
  try {
    await comparison(page).getByRole('button', { name: '取消读取' }).click();
    await expect(comparison(page).getByRole('button', { name: '重新读取会话' })).toBeEnabled();
    await page.keyboard.press('Escape');
    await expect(editor(page)).toBeVisible();
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: '旅程', exact: true }).click();
    await page.getByLabel('会话名称').fill('切换后的新旅程');
    await page.getByRole('button', { name: '创建本地会话' }).click();
    await expect(page.locator('.trip-title')).toHaveText('切换后的新旅程');
  } finally { held.release(); }
  await held.done;
  await expect(page.locator('.trip-title')).toHaveText('切换后的新旅程');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(await trip.read()).toEqual(newer);
  expect(await trip.calls()).toHaveLength(0);
});

test('较慢方案先保存草稿，修正时间后应用且不重查选用方案', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'options-conflict' });
  await open(page); await compare(page);
  await expect(comparison(page).locator('.route-option').nth(1)).toContainText('冲突解决前无法应用');
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  await editor(page).getByRole('button', { name: '第 2 天', exact: true }).click();
  await expect(page.getByRole('button', { name: '应用修改', exact: true })).toBeDisabled();
  await editor(page).getByRole('button', { name: '查看冲突路段' }).press('Enter');
  const row = editor(page).locator('.editor-activity').nth(1);
  await expect(row.getByRole('alert')).toBeFocused();
  await expect(row).toHaveAttribute('data-activity-id', trip.original.data.days[0].activities[1].id);
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
  await expect(editor(page).getByRole('button', { name: '设置本段方式' })).toBeDisabled();
  await expect(editor(page).locator('.editor-activity').first().getByRole('button', { name: '设置本段方式' })).toHaveCount(0);
  await expect(editor(page)).toContainText('地点名称已改，请重新定位。');
  expect(await trip.calls()).toHaveLength(0);
});

test('整程改驾车后，固定公交仍可选第二方案，撤销单段恢复跟随', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  const edit = editor(page);
  const row = edit.locator('.editor-activity').nth(1);
  await setLegMode(page, row, 'transit');
  await expect(row.locator('.trip-leg')).toContainText('本段指定公共交通');
  const original = trip.original.data.days[0].activities[1].incomingRoute;
  expect((await trip.read()).data.draft[0].activities[1].incomingRoute).toEqual(original);
  await edit.getByRole('button', { name: '切换交通方式' }).click();
  const whole = page.getByRole('dialog', { name: '整程交通方式' });
  await whole.getByLabel('整程接驳方式').selectOption('driving');
  await expect(whole).toContainText('影响 1 段接驳');
  await whole.getByText('保留 1 段已指定的方式').click();
  await expect(whole.getByRole('listitem').filter({ hasText: '第 1 天' })).toContainText('公共交通');
  await expect(whole.getByRole('listitem').filter({ hasText: '第 2 天' })).toContainText('固定测试');
  await whole.getByRole('button', { name: '保存方式到草稿' }).click();
  await expect(edit).toContainText('整程交通方式：驾车');
  expect((await trip.read()).data.draft[0].activities[1].incomingRoute).toEqual(original);
  expect(await trip.calls()).toHaveLength(0);
  await row.getByRole('button', { name: '比较公交方案' }).click();
  await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  await comparison(page).getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(row.locator('.trip-leg')).toContainText('参考总票价 ¥0.00');
  const pinned = await trip.read();
  expect(pinned.data.draft[0].activities[1].incomingRoute).toMatchObject({ selection: 'user', durationMinutes: 20 });
  await row.getByRole('button', { name: '设置本段方式' }).press('Enter');
  const form = page.getByRole('dialog', { name: '本段交通方式', exact: true });
  const choice = form.getByLabel('本段接驳方式');
  await choice.selectOption('follow');
  await expect(form.getByRole('listitem')).toContainText('会清除已选公交方案');
  for (const [width, height] of [[390, 844], [960, 640], [1440, 960]]) {
    await page.setViewportSize({ width, height });
    await expect(choice).toBeInViewport();
    await expect(form.getByRole('button', { name: '保存方式到草稿' })).toBeInViewport();
    expect(await form.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(row.getByRole('button', { name: '设置本段方式' })).toBeFocused();
  expect(await trip.read()).toEqual(pinned);
  const clearing = page.waitForRequest(request => request.url().endsWith('/trip/routes/transport'));
  await setLegMode(page, row, 'follow');
  expect((await clearing).postDataJSON()).toEqual({ expected_revision: pinned.revision, activity_id: pinned.data.draft[0].activities[1].id, mode: null });
  await expect(row.locator('.trip-leg')).toContainText('跟随整程（驾车）');
  await expect(row.getByRole('button', { name: '比较公交方案' })).toHaveCount(0);
  const cleared = await trip.read();
  expect(cleared.data.draft[0].activities[1]).not.toHaveProperty('incomingTransportOverride');
  expect(cleared.data.draft[0].activities[1]).not.toHaveProperty('incomingRoute');
  expect(cleared.data.days).toEqual(trip.original.data.days);
  expect(await trip.calls()).toHaveLength(1);
});

test('单段保存失败或另一窗口撤销草稿时，保留选择直到确认重读', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await editor(page).getByRole('button', { name: '设置本段方式' }).press('Enter');
  const form = page.getByRole('dialog', { name: '本段交通方式', exact: true });
  const choice = form.getByLabel('本段接驳方式');
  await choice.selectOption('walking');
  const before = await trip.read();
  await page.route('**/trip/routes/transport', route => route.fulfill({ status: 503, json: { detail: { message: '夹具写入失败' } } }), { times: 1 });
  await form.getByRole('button', { name: '保存方式到草稿' }).click();
  await expect(form.getByRole('alert')).toContainText('写入失败');
  await expect(choice).toHaveValue('walking');
  expect(await trip.read()).toEqual(before);
  const discarded = await localApp.request('/conversations/' + before.id + '/trip/discard', 'POST', { expected_revision: before.revision });
  await form.getByRole('button', { name: '保存方式到草稿' }).click();
  const reload = form.getByRole('button', { name: '重新读取会话', exact: true });
  await expect(reload).toBeFocused();
  await expect(choice).toBeDisabled();
  await reload.press('Enter');
  await form.getByRole('button', { name: '保留本地输入' }).press('Enter');
  await expect(choice).toHaveValue('walking');
  expect(await trip.read()).toEqual(discarded);
  await page.route('**/api/conversations/' + before.id, route => route.fulfill({ status: 503, json: { detail: { message: '夹具读取失败' } } }), { times: 1 });
  await reload.click();
  await form.getByRole('button', { name: '确认重新读取会话' }).click();
  await expect(form.getByRole('alert')).toContainText('读取失败');
  await expect(choice).toHaveValue('walking');
  await reload.click();
  await form.getByRole('button', { name: '确认重新读取会话' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '编辑当天行程', exact: true })).toBeEnabled();
  expect(await trip.read()).toEqual(discarded);
  expect(await trip.calls()).toHaveLength(0);
});

for (const failure of ['丢失', '超时']) test(`单段保存应答${failure}时先重读，已落盘的选择只写一次`, async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await editor(page).getByRole('button', { name: '设置本段方式' }).click();
  const form = page.getByRole('dialog', { name: '本段交通方式', exact: true });
  await form.getByLabel('本段接驳方式').selectOption('walking');
  const before = await trip.read();
  let writes = 0;
  let release!: () => void;
  const hold = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/trip/routes/transport', async route => {
    writes++;
    const response = await route.fetch();
    if (failure === '丢失') return route.abort('failed');
    await hold; // Hold the successful response past the real 15-second fetch deadline.
    await route.fulfill({ response });
  });
  await form.getByRole('button', { name: '保存方式到草稿' }).click();
  try {
    await expect.poll(async () => (await trip.read()).revision).toBe(before.revision + 1);
    await expect(form.getByRole('alert')).toContainText('保存结果尚未确认', { timeout: 20_000 });
  } finally { release(); }
  await expect(form.getByRole('button', { name: '保存方式到草稿' })).toHaveCount(0);
  await form.getByRole('button', { name: '重新读取会话', exact: true }).click();
  await form.getByRole('button', { name: '确认重新读取会话' }).click();
  await expect(editor(page).locator('.trip-leg')).toContainText('本段指定步行');
  const saved = await trip.read();
  expect(saved.revision).toBe(before.revision + 1);
  expect(saved.data.days).toEqual(before.data.days);
  expect(await trip.calls()).toHaveLength(0);
  expect(writes).toBe(1);
});

test('混合草稿聊天续改后，导出反映正式版，重启可恢复两天各自的接驳', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page);
  await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await editor(page).getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const formal = await trip.read();
  const second = formal.data.days[1].activities[1];
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await editor(page).getByRole('button', { name: '第 2 天', exact: true }).click();
  await expect(editor(page).locator('.trip-leg')).toContainText('跟随整程（公共交通）');
  const trigger = editor(page).getByRole('button', { name: '设置本段方式', exact: true });
  await trigger.press('Enter');
  const legForm = page.getByRole('dialog', { name: '本段交通方式', exact: true });
  const choice = legForm.getByLabel('本段接驳方式');
  await expect(choice).toBeFocused();
  const beforePreview = await trip.read();
  await choice.selectOption('walking');
  expect(await trip.read()).toEqual(beforePreview);
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  await trigger.press('Enter');
  await expect(choice).toHaveValue('follow');
  await choice.selectOption('walking');
  const write = page.waitForRequest(request => request.url().endsWith('/trip/routes/transport'));
  await legForm.getByRole('button', { name: '保存方式到草稿' }).press('Enter');
  expect((await write).postDataJSON()).toEqual({ expected_revision: beforePreview.revision, activity_id: second.id, mode: 'walking' });
  await expect(trigger).toBeFocused();
  await expect(editor(page).getByRole('button', { name: '比较公交方案' })).toHaveCount(0);
  await expect(editor(page).locator('.trip-leg')).toContainText('本段指定步行');
  await editor(page).getByRole('button', { name: '关闭窗口', exact: true }).click();
  await expect(page.locator('.activities .trip-leg')).toContainText('本段指定步行');
  const mixed = await trip.read();
  expect(mixed.data.days).toEqual(formal.data.days);
  expect(await trip.calls()).toHaveLength(1);
  await page.getByLabel('旅行对话输入').fill('固定行程，保持身份，每天末项延后15分钟');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect.poll(async () => (await trip.read()).data.draft[1].activities[1].time).toBe('11:15');
  const pending = await trip.read();
  expect(pending.data.days).toEqual(formal.data.days);
  expect(pending.data.draft[0].activities[1].incomingRoute).toEqual(formal.data.days[0].activities[1].incomingRoute);
  expect(pending.data.draft[1].activities[1].incomingTransportOverride).toEqual(mixed.data.draft[1].activities[1].incomingTransportOverride);
  expect(await trip.calls()).toHaveLength(1);

  const exportButton = page.getByRole('button', { name: '导出行程', exact: true });
  const preview = page.getByLabel('Markdown 预览', { exact: true });
  await exportButton.click();
  await page.getByRole('button', { name: '导出已保存正式版', exact: true }).click();
  await expect(preview).toContainText('离线方案2号线');
  await expect(preview).not.toContainText('本段指定步行');
  await page.keyboard.press('Escape');
  await expect(exportButton).toBeFocused();
  await exportButton.click();
  await page.getByRole('button', { name: '先应用草稿再导出', exact: true }).click();
  await expect(preview).toContainText('本段指定步行');
  await expect(preview).toContainText('跟随整程（公共交通）');
  await expect(preview).toContainText('参考总票价：¥0.00');
  await expect(preview).toBeFocused();
  for (const [width, height] of [[960, 640], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await expect(preview).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  const applied = await trip.read();
  expect(applied.data.days[1].activities[1].incomingRoute).toMatchObject({ mode: 'walking', status: 'verified' });
  expect(await trip.calls()).toHaveLength(2);
  await localApp.restart();
  await page.reload();
  await expect(page.getByRole('button', { name: '恢复上一版行程', exact: true })).toBeEnabled();
  expect(await trip.read()).toEqual(applied);
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect.poll(async () => (await trip.read()).data.days).toEqual(formal.data.days);
  const restored = await trip.read();
  for (const key of ['trip', 'candidates', 'favorites']) expect(restored.data[key]).toEqual(formal.data[key]);
  expect(await trip.calls()).toHaveLength(0);
});

test('D3 整程切换、非首方案、时间备注费用、聊天续改、导出重启及恢复闭环', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await setup(page, localApp);
  for (const mode of ['driving', 'transit']) {
    await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
    await page.getByRole('button', { name: '切换交通方式', exact: true }).click();
    await page.getByLabel('整程接驳方式', { exact: true }).selectOption(mode);
    await page.getByRole('button', { name: '保存方式到草稿' }).click();
    await expect(editor(page)).toBeVisible();
    const pending = await trip.read();
    expect(pending.data.draftTrip.transportMode).toBe(mode);
    expect(pending.data.draft.every((day: { activities: { incomingRoute?: unknown }[] }) => day.activities.every(item => !item.incomingRoute))).toBe(true);
    expect(await trip.calls()).toHaveLength(mode === 'driving' ? 0 : 2);
    if (mode === 'driving') {
      await page.getByRole('button', { name: '应用修改', exact: true }).click();
      await expect(page.getByRole('dialog')).toHaveCount(0);
      expect((await trip.read()).data.trip.transportMode).toBe('driving');
    }
  }
  await editor(page).getByRole('button', { name: '比较公交方案', exact: true }).click();
  await compare(page);
  await comparison(page).getByRole('radio', { name: /方案 2/ }).check();
  await page.getByRole('button', { name: '保存方案到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  await page.getByLabel('时间', { exact: true }).first().fill('09:15');
  await page.getByLabel('备注', { exact: true }).first().fill('D3 串联验收备注𠮷🏞️');
  await page.getByLabel('活动费用（元）', { exact: true }).first().fill('23.45');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByLabel('行程费用')).toContainText('¥23.45');
  const beforeChat = await trip.read();
  const selected = beforeChat.data.days[0].activities[1].incomingRoute;
  expect(selected).toMatchObject({ selection: 'user', durationMinutes: 20, transit: { fare: { amountFen: 0 } } });
  expect(beforeChat.data.days[0].activities[0]).toMatchObject({ time: '09:15', description: 'D3 串联验收备注𠮷🏞️', cost: { amountFen: 2345 } });
  expect(await trip.calls()).toHaveLength(4);

  await page.getByLabel('旅行对话输入').fill('固定行程，保持身份，末项延后15分钟');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByRole('button', { name: '继续编辑草稿', exact: true })).toBeEnabled();
  const pending = await trip.read();
  expect(pending.data.days).toEqual(beforeChat.data.days);
  expect(pending.data.draft[0].activities[1]).toMatchObject({ time: '11:15', incomingRoute: selected });
  expect(pending.data.draftTrip.transportMode).toBe('transit');
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const applied = await trip.read();
  expect(applied.data.days[0].activities[1].incomingRoute).toEqual(selected);
  for (const [index, day] of beforeChat.data.days.entries()) {
    for (const [position, activity] of day.activities.entries()) {
      const current = applied.data.days[index].activities[position];
      for (const key of ['id', 'description', 'cost', 'place']) expect(current[key]).toEqual(activity[key]);
    }
  }
  expect(applied.data.favorites).toEqual(trip.original.data.favorites);
  expect(applied.data.candidates).toEqual(trip.original.data.candidates);
  expect(await trip.calls()).toHaveLength(4);
  const path = '/conversations/' + applied.id + '/trip/export';
  const md = await localApp.request(path, 'POST', { expected_revision: applied.revision });
  for (const text of ['用户选用方案', '离线方案2号线𠮷🏞️', '¥0.00', '¥23.45', 'D3 串联验收备注𠮷🏞️']) expect(md.markdown).toContain(text);
  const pdf = await localApp.request(path, 'POST', { expected_revision: applied.revision, format: 'pdf' });
  const bytes = Buffer.from(pdf.pdfBase64, 'base64');
  expect(bytes.subarray(0, 5).toString()).toBe('%PDF-');
  await writeFile('artifacts/d3-flow.pdf', bytes);
  await writeFile('artifacts/d3-flow.md', md.markdown, 'utf8');
  expect(await trip.read()).toEqual(applied);
  await localApp.restart();
  await page.reload();
  await expect(page.locator('.activities .trip-leg').first()).toContainText('公共交通约 20 分钟');
  expect(await trip.read()).toEqual(applied);
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await page.screenshot({ path: 'artifacts/d3-stage-flow.png', fullPage: true });
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect(page.locator('.activities .activity-heading time').last()).toContainText('11:00');
  const restored = await trip.read();
  for (const field of ['days', 'trip', 'candidates', 'favorites']) expect(restored.data[field]).toEqual(beforeChat.data[field]);
  expect(await trip.calls()).toHaveLength(0); // Restart reset the fixture counter; read/restore never query.
});
