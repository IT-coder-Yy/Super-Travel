import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect, type LocalApp } from './fixtures/local-app';

test.use({ reducedMotion: 'reduce' });

async function createTrip(page: Page, localApp: LocalApp) {
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({
    contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>',
  }));
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const original = await localApp.request('/conversations', 'POST', { title: '第二阶段地点编辑固定回归', data: {
    schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [],
  } });
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(page.locator('.trip-title')).toHaveText('第二阶段地点编辑固定回归');
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  await expect(page.getByRole('button', { name: '编辑当天行程', exact: true })).toBeEnabled();
  return { read: () => localApp.request(`/conversations/${original.id}`), id: original.id };
}

async function search(page: Page, query: string) {
  await page.getByLabel('搜索地点', { exact: true }).fill(query);
  await page.getByRole('button', { name: '搜索', exact: true }).click();
}

test('真实形状选点：新增、替换、时长、正式隔离、应用与重启', async ({ page, localApp }) => {
  test.setTimeout(100_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const trip = await createTrip(page, localApp);
  const formal = await trip.read();
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByLabel('活动费用（元）', { exact: true }).nth(1).fill('25.80');
  await page.getByLabel('备注', { exact: true }).nth(1).fill('替换地点仍需保留的用户备注');
  await page.getByRole('button', { name: '新增地点', exact: true }).click();
  await page.getByRole('button', { name: '返回编辑', exact: true }).click();
  await expect(page.getByRole('button', { name: '新增地点', exact: true })).toBeFocused();
  await page.getByRole('button', { name: '新增地点', exact: true }).click();
  await page.getByLabel('检索类别', { exact: true }).selectOption('');
  await search(page, '同名茶馆');
  await expect(page.getByRole('radio')).toHaveCount(2);
  await expect(page.getByRole('button', { name: '保存到草稿', exact: true })).toBeDisabled();
  await page.getByRole('radio', { name: /固定测试西岸地址/ }).check();
  await page.getByLabel('开始时间', { exact: true }).fill('13:00');
  await page.getByLabel('停留时长（分钟）', { exact: true }).fill('60');
  await page.getByLabel('活动备注', { exact: true }).fill('选择西岸同名店');
  await page.screenshot({ path: 'artifacts/b1-place-picker-1440.png', fullPage: true });
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.screenshot({ path: 'artifacts/b1-place-picker-1024.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'artifacts/b1-place-picker-390.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole('button', { name: '保存到草稿', exact: true }).click();
  await expect(page.getByLabel('地点', { exact: true })).toHaveCount(3);
  await expect(page.getByRole('button', { name: '新增地点', exact: true })).toBeFocused();
  let saved = await trip.read();
  expect(saved.data.days).toEqual(formal.data.days);
  expect(saved.data.draft[0].activities[2].place.id).toBe('baidu:fixture-tea-west');
  expect(saved.data.draft[0].activities[2].description).toBe('选择西岸同名店');
  const replacementId = saved.data.draft[0].activities[1].id;

  await page.getByRole('button', { name: '替换地点', exact: true }).nth(1).click();
  await search(page, '固定测试花园');
  await page.getByRole('radio', { name: /固定测试花园地址/ }).check();
  await expect(page.getByLabel('活动备注', { exact: true })).toHaveValue('替换地点仍需保留的用户备注');
  await page.getByRole('button', { name: '保存到草稿', exact: true }).click();
  await expect(page.getByLabel('地点', { exact: true }).nth(1)).toHaveValue('固定测试花园');
  await expect(page.getByLabel('活动费用（元）', { exact: true }).nth(1)).toHaveValue('');
  await expect(page.getByRole('button', { name: '替换地点', exact: true }).nth(1)).toBeFocused();
  await page.getByLabel('停留时长', { exact: true }).first().selectOption('75');
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await expect(page.locator('.storage-indicator')).toContainText('已保存到本机');
  await page.reload();
  await expect(page.locator('.activity-heading')).toHaveCount(3);
  saved = await trip.read();
  expect(saved.data.draft[0].activities[1].id).toBe(replacementId);
  expect(saved.data.draft[0].activities[1].cost).toBeUndefined();
  expect(saved.data.draft[0].activities[0].duration).toBe('约 75 分钟');
  expect(saved.data.days).toEqual(formal.data.days);
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  saved = await trip.read();
  expect(saved.data.draft).toBeNull();
  expect(saved.data.previousTrip.days).toEqual(formal.data.days);
  const disk = JSON.parse(await readFile(resolve(localApp.dataDir, 'conversations', `${trip.id}.json`), 'utf8'));
  expect(disk.data.days).toEqual(saved.data.days);
  expect(JSON.stringify(disk)).not.toContain('fixture-key-not-real');
  await localApp.restart();
  await page.reload();
  await expect(page.locator('.activity-heading')).toHaveCount(3);
  await expect(page.locator('.real-map-marker')).toHaveCount(3);
  expect((await trip.read()).data.days).toEqual(saved.data.days);
  expect(errors).toEqual([]);
});

test('搜索空结果与失败、重叠拒绝和保存失败后保留输入重试', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await createTrip(page, localApp);
  const formal = await trip.read();
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByRole('button', { name: '新增地点', exact: true }).click();
  await search(page, '没有可核验地点');
  await expect(page.getByRole('radio')).toHaveCount(0);
  await expect(page.getByText(/没有找到地点/)).toBeVisible();
  await expect(page.getByRole('button', { name: '保存到草稿', exact: true })).toBeDisabled();
  await search(page, '测试额度不足');
  await expect(page.getByRole('alert')).toContainText('额度不足');
  await expect(page.getByLabel('搜索地点', { exact: true })).toHaveValue('测试额度不足');
  await search(page, '固定测试花园');
  await page.getByRole('radio', { name: /固定测试花园地址/ }).check();
  await page.getByLabel('开始时间', { exact: true }).fill('09:00');
  await page.getByLabel('停留时长（分钟）', { exact: true }).fill('60');
  await page.getByLabel('活动备注', { exact: true }).fill('失败也保留这条备注');
  await page.getByRole('button', { name: '保存到草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText(/重叠|冲突/);
  expect((await trip.read()).data.draft[0].activities).toHaveLength(2);
  await page.getByLabel('开始时间', { exact: true }).fill('14:00');
  await page.route('**/trip/places/select', async route => {
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定测试保存失败，原数据已保留。' } }) });
    await page.unroute('**/trip/places/select');
  }, { times: 1 });
  await page.getByRole('button', { name: '保存到草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('保存失败');
  await expect(page.getByLabel('活动备注', { exact: true })).toHaveValue('失败也保留这条备注');
  await page.getByRole('button', { name: '保存到草稿', exact: true }).click();
  await expect(page.getByLabel('地点', { exact: true })).toHaveCount(3);
  expect((await trip.read()).data.days).toEqual(formal.data.days);
  await page.getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.locator('.activity-heading')).toHaveCount(2);
  await expect(page.locator('.storage-indicator')).toContainText('已保存到本机');
  expect((await trip.read()).data.draft).toBeNull();
});

test('旧草稿非整刻时间可保留，新的人工时间仍要求15分钟', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await createTrip(page, localApp);
  const document = await trip.read();
  const data = structuredClone(document.data);
  data.draft = structuredClone(data.days);
  data.draftTrip = data.trip;
  data.draft[0].activities[0].time = '09:05';
  data.draft[0].activities[0].duration = '约 55 分钟';
  await localApp.request(`/conversations/${trip.id}`, 'PUT', { title: document.title, data, expected_revision: document.revision });
  await page.reload();
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await expect(page.getByLabel('时间', { exact: true }).first()).toHaveValue('09:05');
  await expect(page.getByLabel('停留时长', { exact: true }).first()).toHaveValue('55');
  await page.getByRole('button', { name: '替换地点', exact: true }).first().click();
  await search(page, '固定测试花园');
  await page.getByRole('radio', { name: /固定测试花园地址/ }).check();
  await expect(page.getByLabel('开始时间', { exact: true })).toHaveValue('09:05');
  await expect(page.getByLabel('停留时长（分钟）', { exact: true })).toHaveValue('55');
  await page.getByRole('button', { name: '保存到草稿', exact: true }).click();
  await expect(page.getByLabel('地点', { exact: true }).first()).toHaveValue('固定测试花园');
  await page.getByLabel('时间', { exact: true }).first().fill('09:07');
  await expect(page.getByLabel('时间', { exact: true }).first()).toHaveValue('09:07');
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('15 分钟');
  await expect(page.getByRole('button', { name: '应用修改', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '新增地点', exact: true })).toBeDisabled();
  await page.getByLabel('备注', { exact: true }).first().fill('合法备注仍应自动保存');
  await expect(page.locator('.storage-indicator')).toContainText('已保存到本机');
  let checkpoint = await trip.read();
  expect(checkpoint.data.days[0].activities[0].time).toBe('09:00');
  expect(checkpoint.data.draft[0].activities[0].time).toBe('09:05');
  expect(checkpoint.data.draft[0].activities[0].description).toBe('合法备注仍应自动保存');
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await expect(page.getByLabel('时间', { exact: true }).first()).toHaveValue('09:05');
  await expect(page.getByRole('button', { name: '应用修改', exact: true })).toBeEnabled();
  await page.getByLabel('时间', { exact: true }).first().fill('09:07');
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await page.reload();
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await expect(page.getByLabel('时间', { exact: true }).first()).toHaveValue('09:05');
  await expect(page.getByLabel('备注', { exact: true }).first()).toHaveValue('合法备注仍应自动保存');
  await page.getByLabel('时间', { exact: true }).first().fill('09:07');
  await page.getByLabel('时间', { exact: true }).first().fill('09:05');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const saved = await trip.read();
  expect(saved.data.days[0].activities[0].time).toBe('09:05');
  expect(saved.data.days[0].activities[0].duration).toBe('约 55 分钟');
});
