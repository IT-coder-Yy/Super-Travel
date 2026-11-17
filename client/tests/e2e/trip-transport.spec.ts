import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect, type LocalApp } from '../fixtures/local-app';

test.use({ reducedMotion: 'reduce' });

test.beforeEach(async ({ page }) => {
  // All browser requests stay local, including map tiles and optional example photos.
  await page.route('**/*', route => {
    const host = new URL(route.request().url()).hostname;
    return ['127.0.0.1', 'localhost'].includes(host) ? route.continue() : route.fulfill({
      contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>',
    });
  });
});

test.afterEach(async ({ localApp }) => {
  await localApp.request('/test-fixture/routes', 'POST', { profile: null });
});

async function setup(page: Page, localApp: LocalApp) {
  await localApp.reset();
  await localApp.request('/test-fixture/routes', 'POST', { profile: null });
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const original = await localApp.request('/conversations', 'POST', { title: '整程交通方式离线回归', data: {
    schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [],
  } });
  const read = () => localApp.request('/conversations/' + original.id);
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  await expect(page.getByRole('button', { name: '编辑当天行程', exact: true })).toBeEnabled();
  const document = await read();
  delete document.data.trip.transportMode; // Old JSON defaults to walking without migration.
  for (const day of document.data.days) {
    day.activities[1].incomingRoute = { fromPlaceId: day.activities[0].place.id, toPlaceId: day.activities[1].place.id,
      mode: 'walking', status: 'verified', durationMinutes: 13, distanceMeters: 1200, provider: 'baidu', queriedAt: new Date().toISOString() };
  }
  const first = document.data.days[0].activities[0];
  first.cost = { amountFen: 4250, currency: 'CNY', source: 'user' };
  const candidate = { id: 'offline-candidate', title: first.title, place: structuredClone(first.place), destination: '杭州',
    duration: first.duration, description: '保留候选备注', cost: { amountFen: 0, currency: 'CNY', source: 'user' } };
  document.data.candidates = [candidate];
  document.data.favorites = [first.id, candidate.id];
  await writeFile(resolve(localApp.dataDir, 'conversations', original.id + '.json'), JSON.stringify(document), 'utf8');
  await page.reload();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'verified' });
  return { read, original: document, calls: async () => (await localApp.request('/test-fixture/state')).route_calls as { model: string }[] };
}

const editor = (page: Page) => page.getByRole('dialog', { name: '编辑当天行程', exact: true });
const form = (page: Page) => page.getByRole('dialog', { name: '整程交通方式', exact: true });
async function open(page: Page) {
  await page.getByRole('button', { name: /^(编辑当天行程|继续编辑草稿)$/ }).click();
  await page.getByRole('button', { name: '切换交通方式', exact: true }).click();
  await expect(form(page)).toBeVisible();
}

test('键盘取消、草稿保存与正式应用保留用户字段，并可重启、导出和恢复', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await setup(page, localApp);
  await open(page);
  await expect(page.getByLabel('整程接驳方式', { exact: true })).toBeFocused();
  await expect(page.getByRole('button', { name: '保存方式到草稿' })).toBeDisabled();
  const beforeCancel = await trip.read();
  await page.getByLabel('整程接驳方式', { exact: true }).press('ArrowDown');
  await page.keyboard.press('Escape');
  await expect(editor(page)).toBeVisible();
  await expect(page.getByRole('button', { name: '切换交通方式' })).toBeFocused();
  expect(await trip.read()).toEqual(beforeCancel);
  await page.getByLabel('备注', { exact: true }).first().fill('切换后保留的草稿备注𠮷🏞️');
  await page.getByRole('button', { name: '切换交通方式' }).press('Enter');
  await page.getByLabel('整程接驳方式', { exact: true }).selectOption('driving');
  await expect(form(page)).toContainText('影响 2 段接驳');
  await expect(form(page).locator('.transport-impact li')).toHaveCount(2);
  expect(await trip.calls()).toEqual([]);
  await page.screenshot({ path: 'artifacts/d1-transport-1440.png', fullPage: true });
  for (const [width, height] of [[960, 640], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await expect(page.getByRole('button', { name: '保存方式到草稿' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await form(page).evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: `artifacts/d1-transport-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole('button', { name: '保存方式到草稿' }).press('Enter');
  await expect(editor(page)).toBeVisible();
  await expect(page.getByRole('button', { name: '切换交通方式' })).toBeFocused();
  const pending = await trip.read();
  expect(pending.data.days).toEqual(trip.original.data.days);
  expect(pending.data.trip).toEqual(trip.original.data.trip);
  expect(pending.data.draftTrip.transportMode).toBe('driving');
  expect(pending.data.draft[0].activities[1].incomingRoute).toBeUndefined();
  expect(pending.data.draftCandidates).toEqual(trip.original.data.candidates);
  expect(await trip.calls()).toEqual([]);
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.activities .trip-leg').first()).toContainText('驾车约 13 分钟');
  const applied = await trip.read();
  expect(await trip.calls()).toHaveLength(2);
  expect((await trip.calls()).every(call => call.model === 'driving')).toBe(true);
  expect(applied.data.days[0].activities[0]).toMatchObject({ id: trip.original.data.days[0].activities[0].id,
    description: '切换后保留的草稿备注𠮷🏞️', cost: { amountFen: 4250 } });
  expect(applied.data.favorites).toEqual(trip.original.data.favorites);
  const exported = await localApp.request('/conversations/' + applied.id + '/trip/export', 'POST', { expected_revision: applied.revision });
  expect(exported.markdown).toContain('驾车约 13 分钟');
  expect(exported.markdown).toContain('¥42.50');
  await localApp.restart();
  await page.reload();
  await expect(page.locator('.activities .trip-leg').first()).toContainText('驾车约 13 分钟');
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect(page.locator('.activities .trip-leg').first()).toContainText('步行约 13 分钟');
  expect((await trip.read()).data.days).toEqual(trip.original.data.days);
});

test('保存失败保留选择；并发修订拒绝覆盖，返回重读后可继续', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await open(page);
  await page.getByLabel('整程接驳方式', { exact: true }).selectOption('transit');
  const pending = await trip.read();
  await page.route('**/trip/transport', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定离线写入失败，请重试。' } }) }), { times: 1 });
  await page.getByRole('button', { name: '保存方式到草稿' }).click();
  await expect(form(page).getByRole('alert')).toContainText('写入失败');
  await expect(page.getByLabel('整程接驳方式', { exact: true })).toHaveValue('transit');
  expect(await trip.read()).toEqual(pending);
  const newerData = structuredClone(pending.data);
  newerData.draft[0].activities[0].description = '另一个窗口刚保存的备注';
  const newer = await localApp.request('/conversations/' + pending.id, 'PUT', { title: pending.title, data: newerData, expected_revision: pending.revision });
  await page.getByRole('button', { name: '保存方式到草稿' }).click();
  await expect(form(page).getByRole('alert')).toContainText('重新加载');
  expect(await trip.read()).toEqual(newer);
  await page.getByRole('button', { name: '返回编辑', exact: true }).click();
  await page.reload();
  await open(page);
  await page.getByLabel('整程接驳方式', { exact: true }).selectOption('transit');
  await page.getByRole('button', { name: '保存方式到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  expect((await trip.read()).data.draft[0].activities[0].description).toBe('另一个窗口刚保存的备注');
  expect(await trip.calls()).toHaveLength(0);
});

test('硬时间冲突保留草稿，用户调整后应用；有效零票价不计入活动小计', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await setup(page, localApp);
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'conflict' });
  await open(page);
  await page.getByLabel('整程接驳方式', { exact: true }).selectOption('transit');
  await page.getByRole('button', { name: '保存方式到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  const pending = await trip.read();
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(editor(page).getByRole('alert')).toContainText('75 分钟');
  await expect(editor(page).getByRole('alert')).toBeInViewport();
  await expect(editor(page).getByRole('alert')).toBeFocused();
  expect(await trip.read()).toEqual(pending);
  await page.screenshot({ path: 'artifacts/d1-time-conflict.png', fullPage: true });
  await page.getByLabel('时间', { exact: true }).nth(1).fill('11:30');
  await editor(page).getByRole('button', { name: '第 2 天', exact: true }).click();
  await page.getByLabel('时间', { exact: true }).nth(1).fill('11:30');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByLabel('行程费用')).toContainText('¥42.50');
  await expect(page.locator('.activities .trip-leg').first()).toContainText('公共交通约 75 分钟');
  await expect(page.locator('.activities .transit-details').first()).toContainText('¥0.00');
  await expect(page.locator('.activities .transit-details').first()).toContainText('仅供参考，以实际结果为准');
  expect(await trip.calls()).toHaveLength(4);
  const result = await trip.read();
  expect(result.data.trip.transportMode).toBe('transit');
  expect(result.data.favorites).toEqual(trip.original.data.favorites);
});

test('查询失败允许保存未知路线，逐段提示原因且不自动重试', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await localApp.request('/test-fixture/routes', 'POST', { profile: 'quota' });
  await open(page);
  await page.getByLabel('整程接驳方式', { exact: true }).selectOption('driving');
  await page.getByRole('button', { name: '保存方式到草稿' }).click();
  await expect(editor(page)).toBeVisible();
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.activities .trip-leg').first()).toContainText('驾车耗时未知');
  await expect(page.locator('.activities .trip-leg').first()).toContainText('额度或权限不可用');
  expect(await trip.calls()).toHaveLength(2);
  const result = await trip.read();
  expect(result.data.days[0].activities[1].incomingRoute.durationMinutes).toBeUndefined();
  await page.reload();
  await expect(page.locator('.activities .trip-leg').first()).toContainText('驾车耗时未知');
  expect(await trip.calls()).toHaveLength(2);
});
