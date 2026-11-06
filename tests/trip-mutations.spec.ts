import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect, type LocalApp } from './fixtures/local-app';

test.use({ reducedMotion: 'reduce' });

async function createTrip(page: Page, localApp: LocalApp, single = false) {
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({
    contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>',
  }));
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const original = await localApp.request('/conversations', 'POST', { title: '跨日移动与删除固定回归', data: {
    schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [],
  } });
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(page.locator('.trip-title')).toHaveText('跨日移动与删除固定回归');
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  await expect(page.getByRole('button', { name: '编辑当天行程', exact: true })).toBeEnabled();
  const document = await localApp.request('/conversations/' + original.id);
  // Isolated, explicitly fictional saved-data fixture: the second day uses a different POI.
  // No real provider evidence or user data is modified by this setup.
  const garden = structuredClone(document.data.days[1].activities[0]);
  garden.title = '固定测试花园';
  garden.place.id = 'baidu:fixture-garden';
  garden.place.name = garden.title;
  garden.place.address = '固定测试花园地址';
  garden.description = '固定测试第二天花园安排';
  delete garden.incomingRoute;
  document.data.days[1].activities = single ? [] : [garden];
  if (single) document.data.days[0].activities = document.data.days[0].activities.slice(0, 1);
  document.data.favorites = document.data.days[0].activities.map((activity: { id: string }) => activity.id);
  const path = resolve(localApp.dataDir, 'conversations', original.id + '.json');
  await writeFile(path, JSON.stringify(document), 'utf8');
  await page.reload();
  await expect(page.locator('.trip-title')).toHaveText(document.title);
  await expect(page.locator('.activity-heading')).toHaveCount(single ? 1 : 2);
  return { read: () => localApp.request('/conversations/' + original.id), id: original.id, path };
}

async function openEditor(page: Page) {
  await page.getByRole('button', { name: /^(编辑当天行程|继续编辑草稿)$/ }).click();
}
async function editorDay(page: Page, day: number) {
  await page.getByRole('dialog').getByRole('button', { name: '第 ' + day + ' 天', exact: true }).click();
}
async function previewDeletion(page: Page, index = 0) {
  await page.getByRole('button', { name: '删除活动', exact: true }).nth(index).click();
  await expect(page.getByRole('button', { name: '确认从草稿删除', exact: true })).toBeEnabled();
}

test('跨日移动保留身份、备注、费用与收藏，冲突失败不改数据，后续聊天仍保留', async ({ page, localApp }) => {
  test.setTimeout(120_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const trip = await createTrip(page, localApp);
  const formal = await trip.read();
  const movedId = formal.data.days[0].activities[0].id;
  await openEditor(page);
  await page.getByLabel('活动费用（元）', { exact: true }).first().fill('42.50');
  await page.getByLabel('备注', { exact: true }).first().fill('移动后仍保留的用户备注');
  await page.getByRole('button', { name: '移动活动', exact: true }).first().click();
  await page.getByLabel('目标日期', { exact: true }).selectOption('2');
  await page.getByLabel('插入位置', { exact: true }).selectOption('1');
  await page.getByLabel('移动后开始时间', { exact: true }).fill('09:00');
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText(/重叠|冲突|时间|位置/);
  await expect(page.getByLabel('目标日期', { exact: true })).toHaveValue('2');
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('1');
  await expect(page.getByLabel('移动后开始时间', { exact: true })).toHaveValue('09:00');
  let saved = await trip.read();
  expect(saved.data.days).toEqual(formal.data.days);
  expect(saved.data.draft[0].activities.map((activity: { id: string }) => activity.id)).toContain(movedId);
  await page.getByLabel('移动后开始时间', { exact: true }).fill('13:00');
  await page.screenshot({ path: 'artifacts/b2-move-1440.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'artifacts/b2-move-390.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '编辑当天行程', exact: true })).toBeVisible();
  await expect(page.getByLabel('地点', { exact: true })).toHaveCount(2);
  saved = await trip.read();
  expect(saved.data.selectedDay).toBe(2);
  expect(saved.data.draft[0].activities.map((activity: { id: string }) => activity.id)).not.toContain(movedId);
  const moved = saved.data.draft[1].activities[1];
  expect(moved.id).toBe(movedId);
  expect(moved.time).toBe('13:00');
  expect(moved.duration).toBe('约 60 分钟');
  expect(moved.description).toBe('移动后仍保留的用户备注');
  expect(moved.cost.amountFen).toBe(4250);
  expect(saved.data.favorites).toContain(movedId);
  expect(saved.data.days).toEqual(formal.data.days);
  await expect(page.locator('#move-trip-activity-' + movedId)).toBeFocused();
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByLabel('旅行对话输入').fill('调整固定行程，保持身份');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.draft-label')).toHaveText('修改草稿');
  await expect(page.getByRole('button', { name: '继续编辑草稿', exact: true })).toBeEnabled();
  saved = await trip.read();
  const afterChat = saved.data.draft[1].activities.find((activity: { id: string }) => activity.id === movedId);
  expect(afterChat.time).toBe('13:15');
  expect(afterChat.cost).toEqual(moved.cost);
  expect(afterChat.description).toBe(moved.description);
  await openEditor(page);
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const beforeRestart = await trip.read();
  await localApp.restart();
  await page.reload();
  await page.getByRole('tab', { name: '第 2 天', exact: true }).click();
  await expect(page.locator('.activity-heading')).toHaveCount(2);
  await expect(page.locator('.real-map-marker')).toHaveCount(2);
  expect((await trip.read()).data.days).toEqual(beforeRestart.data.days);
  const disk = JSON.parse(await readFile(trip.path, 'utf8'));
  expect(JSON.stringify(disk)).not.toContain('fixture-key-not-real');
  expect(errors).toEqual([]);
});

test('删除先读影响且可取消，删空一天后地图不残留，应用与重启及上一版恢复同步收藏', async ({ page, localApp }) => {
  test.setTimeout(120_000);
  const trip = await createTrip(page, localApp);
  const formal = await trip.read();
  const deletedIds = formal.data.days[0].activities.map((activity: { id: string }) => activity.id);
  await openEditor(page);
  await previewDeletion(page);
  await expect(page.getByRole('button', { name: '取消删除', exact: true })).toBeFocused();
  await expect(page.getByText('应用修改时移除该活动的收藏', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '取消删除', exact: true }).click();
  await expect(page.getByLabel('地点', { exact: true })).toHaveCount(2);
  expect((await trip.read()).data.draft[0].activities).toEqual(formal.data.days[0].activities);

  await previewDeletion(page);
  await page.getByRole('button', { name: '确认从草稿删除', exact: true }).click();
  await expect(page.getByLabel('地点', { exact: true })).toHaveCount(1);
  expect((await trip.read()).data.favorites).toEqual(deletedIds);
  await previewDeletion(page);
  await page.screenshot({ path: 'artifacts/b2-delete-1440.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'artifacts/b2-delete-390.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole('button', { name: '确认从草稿删除', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('heading', { name: '自由安排', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '新增地点', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: '新增地点', exact: true })).toBeFocused();
  let saved = await trip.read();
  expect(saved.data.days).toEqual(formal.data.days);
  expect(saved.data.draft[0].activities).toEqual([]);
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '自由安排', exact: true })).toBeVisible();
  await expect(page.locator('.real-map-marker')).toHaveCount(0);
  await expect(page.locator('.place-card')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '新增当天活动', exact: true })).toBeEnabled();
  saved = await trip.read();
  expect(saved.data.favorites).toEqual([]);
  expect(saved.data.previousTrip.removedFavorites.sort()).toEqual([...deletedIds].sort());
  await localApp.restart();
  await page.reload();
  await expect(page.getByRole('heading', { name: '自由安排', exact: true })).toBeVisible();
  await expect(page.locator('.real-map-marker')).toHaveCount(0);
  await expect(page.locator('.place-card')).toHaveCount(0);
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect(page.locator('.activity-heading')).toHaveCount(2);
  await expect(page.locator('.real-map-marker')).toHaveCount(2);
  saved = await trip.read();
  expect(saved.data.days).toEqual(formal.data.days);
  expect(saved.data.favorites.sort()).toEqual([...deletedIds].sort());
  await page.getByRole('button', { name: '收藏', exact: true }).click();
  await expect(page.locator('.favorite-item')).toHaveCount(2);
});

test('读取删除影响失败不能确认，最后一个活动拒绝删除，失败不污染正式版', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp, true);
  const formal = await trip.read();
  await openEditor(page);
  await page.route('**/trip/activities/delete-preview', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定测试：删除影响读取失败，请重试。' } }) }), { times: 1 });
  await page.getByRole('button', { name: '删除活动', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('删除影响读取失败');
  await expect(page.getByRole('button', { name: '确认从草稿删除', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '重新读取删除影响', exact: true }).click();
  await expect(page.getByRole('button', { name: '确认从草稿删除', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '确认从草稿删除', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText(/至少|最后|一个活动/);
  const saved = await trip.read();
  expect(saved.data.days).toEqual(formal.data.days);
  expect(saved.data.draft[0].activities).toHaveLength(1);
  expect(saved.data.draft[1].activities).toEqual([]);
  await page.getByRole('button', { name: '取消删除', exact: true }).click();
  await expect(page.getByLabel('地点', { exact: true })).toHaveCount(1);
  await editorDay(page, 2);
  await expect(page.getByRole('dialog').getByRole('heading', { name: '自由安排', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await expect(page.locator('.real-map-marker')).toHaveCount(0);
  await expect(page.locator('.place-card')).toHaveCount(0);
});
