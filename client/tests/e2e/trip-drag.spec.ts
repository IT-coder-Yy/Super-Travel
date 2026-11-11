import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import type { WorkspaceData } from '../../src/types';
import { test, expect, type LocalApp } from '../fixtures/local-app';

test.use({ reducedMotion: 'reduce' });

async function setup(page: Page, localApp: LocalApp, change?: (data: WorkspaceData) => void) {
  await page.route('https://**/*', route => route.request().url().startsWith('https://tile.openstreetmap.org/')
    ? route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>' })
    : route.abort());
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const document = await localApp.request('/conversations', 'POST', { title: '同日拖拽固定回归', data: {
    schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [],
  } });
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.activity-heading')).toHaveCount(2);
  await expect(page.getByRole('button', { name: '编辑当天行程', exact: true })).toBeEnabled();
  const formal = await localApp.request('/conversations/' + document.id);
  // Fictional saved data in the isolated test directory, never a real provider response.
  const third = structuredClone(formal.data.days[0].activities[0]);
  third.id = 'drag-third'; third.time = '13:00'; third.title = '固定测试花园';
  third.place = { ...third.place, id: 'baidu:drag-garden', name: third.title };
  formal.data.days[0].activities.push(third);
  formal.data.favorites = [formal.data.days[0].activities[0].id];
  const { time, x, y, incomingRoute, ...candidate } = structuredClone(third);
  formal.data.candidates = [{ ...candidate, id: 'drag-candidate', destination: '杭州' }];
  change?.(formal.data);
  const path = resolve(localApp.dataDir, 'conversations', document.id + '.json');
  await writeFile(path, JSON.stringify(formal), 'utf8');
  await page.reload();
  await expect(page.locator('.activity-heading')).toHaveCount(formal.data.days[0].activities.length);
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await expect(page.locator('.activity-drag-handle').first()).toBeEnabled();
  return { id: document.id, path, formal, read: () => localApp.request('/conversations/' + document.id) };
}

async function beginDrag(page: Page, index = 0) {
  const handle = page.locator('.activity-drag-handle').nth(index);
  await handle.scrollIntoViewIfNeeded();
  const box = (await handle.boundingBox())!;
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  return point;
}

async function dragTo(page: Page, from: number, target: number, before = false) {
  const point = await beginDrag(page, from);
  await page.mouse.move(point.x - 12, point.y, { steps: 2 });
  const row = page.locator('[data-activity-id]').nth(target);
  await row.scrollIntoViewIfNeeded();
  const box = (await row.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 + (before ? -24 : 24), { steps: 8 });
  await expect(row).toHaveClass(new RegExp(before ? 'drop-before' : 'drop-after'));
  await page.mouse.up();
  await expect(page.getByRole('dialog', { name: '移动活动', exact: true })).toBeVisible();
}

test('拖拽首项到末尾：确认时间、保存一次、应用重启和上一版恢复', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await setup(page, localApp);
  const first = trip.formal.data.days[0].activities[0];
  await page.getByLabel('备注', { exact: true }).first().fill('拖拽前未保存的备注');
  await page.getByLabel('活动费用（元）', { exact: true }).first().fill('42.50');
  await dragTo(page, 0, 2);
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('2');
  await expect(page.getByLabel('移动后开始时间')).toHaveValue('09:00');
  const beforeMove = await trip.read();
  expect(beforeMove.data.days).toEqual(trip.formal.data.days);
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText(/时间|重叠|位置/);
  expect((await trip.read()).data.draft).toEqual(beforeMove.data.draft);
  await page.getByLabel('移动后开始时间').fill('15:00');
  let calls = 0;
  await page.route('**/trip/activities/move', async route => { calls++; await new Promise(resolve => setTimeout(resolve, 150)); await route.continue(); });
  await page.getByRole('button', { name: '移动到草稿', exact: true }).dblclick();
  await expect(page.getByRole('dialog', { name: '编辑当天行程', exact: true })).toBeVisible();
  expect(calls).toBe(1);
  await expect(page.locator('#drag-trip-activity-' + first.id)).toBeFocused();
  let saved = await trip.read();
  expect(saved.data.days).toEqual(trip.formal.data.days);
  expect(saved.data.draft[0].activities.map((item: { id: string }) => item.id)).toEqual([
    ...trip.formal.data.days[0].activities.slice(1).map((item: { id: string }) => item.id), first.id,
  ]);
  expect(saved.data.draft[0].activities[2]).toMatchObject({ id: first.id, time: '15:00', description: '拖拽前未保存的备注', cost: { amountFen: 4250 }, place: first.place });
  expect(saved.data.candidates).toEqual(trip.formal.data.candidates);
  expect(saved.data.favorites).toEqual(trip.formal.data.favorites);
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  saved = await trip.read();
  expect(saved.data.days[0].activities[2].id).toBe(first.id);
  await localApp.restart();
  await page.reload();
  await expect(page.locator('.activity-heading')).toHaveCount(3);
  expect((await trip.read()).data.days).toEqual(saved.data.days);
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect(page.locator('.activity-heading').first()).toContainText(first.title);
  expect((await trip.read()).data.days).toEqual(trip.formal.data.days);
  expect(JSON.parse(await readFile(trip.path, 'utf8')).data.candidates).toEqual(trip.formal.data.candidates);
});

test('末项拖到首位后可取消，键盘手柄和原有上下移按钮仍可用', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await dragTo(page, 2, 0, true);
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('0');
  await page.getByRole('button', { name: '返回编辑', exact: true }).click();
  await expect(page.locator('#drag-trip-activity-drag-third')).toBeFocused();
  expect((await trip.read()).data.draft[0].activities).toEqual(trip.formal.data.days[0].activities);
  await page.locator('#drag-trip-activity-drag-third').press('Enter');
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('2');
  await page.getByLabel('插入位置', { exact: true }).selectOption('0');
  await page.getByLabel('移动后开始时间').fill('07:00');
  await page.getByRole('button', { name: '移动到草稿', exact: true }).press('Enter');
  await expect(page.locator('[data-activity-id]').first()).toHaveAttribute('data-activity-id', 'drag-third');
  await expect(page.getByRole('button', { name: '下移固定测试花园', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '下移固定测试花园', exact: true }).press('Enter');
  await expect(page.locator('[data-activity-id]').nth(1)).toHaveAttribute('data-activity-id', 'drag-third');
  await page.getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await trip.read()).data.days).toEqual(trip.formal.data.days);
});

test('误触、原位、Esc、失焦、捕获取消和放到列表外均不移动', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  const moves: unknown[] = [];
  page.on('request', request => { if (request.url().endsWith('/trip/activities/move')) moves.push(request.postDataJSON()); });
  const point = await beginDrag(page);
  await page.mouse.move(point.x + 3, point.y + 2); await page.mouse.up();
  await expect(page.getByRole('dialog', { name: '编辑当天行程', exact: true })).toBeVisible();
  await beginDrag(page);
  await page.mouse.move(point.x - 15, point.y); await page.mouse.up();
  for (const cancel of ['escape', 'blur', 'capture', 'outside']) {
    const current = await beginDrag(page);
    await page.mouse.move(current.x - 20, current.y + 12);
    await expect(page.locator('.activity-drag-preview')).toBeVisible();
    if (cancel === 'escape') await page.keyboard.press('Escape');
    if (cancel === 'blur') await page.evaluate(() => window.dispatchEvent(new Event('blur')));
    if (cancel === 'capture') await page.locator('.activity-drag-handle').first().dispatchEvent('lostpointercapture');
    if (cancel === 'outside') await page.mouse.move(10, 10);
    await page.mouse.up();
    await expect(page.locator('.activity-drag-preview')).toHaveCount(0);
    await expect(page.getByRole('dialog', { name: '编辑当天行程', exact: true })).toBeVisible();
  }
  expect(moves).toEqual([]);
  await expect.poll(async () => (await trip.read()).data.draft).not.toBeNull();
  expect((await trip.read()).data.draft[0].activities).toEqual(trip.formal.data.days[0].activities);
});

test('保存失败可重试，旧修订拒绝覆盖且保留位置和时间输入', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  await dragTo(page, 2, 0, true);
  await page.getByLabel('移动后开始时间').fill('07:00');
  const before = await trip.read();
  await page.route('**/trip/activities/move', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定测试：写入失败，请重试。' } }) }), { times: 1 });
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('写入失败');
  expect((await trip.read()).data).toEqual(before.data);
  await page.route('**/trip/activities/move', route => route.abort('connectionrefused'), { times: 1 });
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText(/fetch|network|连接/i);
  expect((await trip.read()).data).toEqual(before.data);
  await expect(page.getByLabel('移动后开始时间')).toHaveValue('07:00');
  const latest = await localApp.request('/conversations/' + trip.id, 'PUT', { expected_revision: before.revision, title: before.title, data: before.data });
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('会话已被更新');
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('0');
  await expect(page.getByLabel('移动后开始时间')).toHaveValue('07:00');
  expect((await trip.read()).revision).toBe(latest.revision);
  expect((await trip.read()).data).toEqual(before.data);
  await page.reload();
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await dragTo(page, 2, 0, true);
  await page.getByLabel('移动后开始时间').fill('07:00');
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.locator('[data-activity-id]').first()).toHaveAttribute('data-activity-id', 'drag-third');
});

test('最小桌面窗口可边缘滚动，拖动时不自动保存，未完成时间输入禁用拖动', async ({ page, localApp }) => {
  await page.setViewportSize({ width: 960, height: 640 });
  const trip = await setup(page, localApp);
  await page.getByLabel('时间', { exact: true }).first().fill('09:07');
  await expect(page.locator('.activity-drag-handle').first()).toBeDisabled();
  await page.getByLabel('时间', { exact: true }).first().fill('09:00');
  await expect(page.locator('.activity-drag-handle').first()).toBeEnabled();
  const point = await beginDrag(page);
  const revision = (await trip.read()).revision;
  const dialog = page.getByRole('dialog');
  const box = (await dialog.boundingBox())!;
  await page.mouse.move(point.x - 30, box.y + box.height - 20, { steps: 6 });
  await expect.poll(() => dialog.evaluate(element => element.scrollTop)).toBeGreaterThan(300);
  await page.waitForTimeout(1100); // Exceed the existing autosave debounce while the pointer is held.
  expect((await trip.read()).revision).toBe(revision);
  await page.screenshot({ path: 'artifacts/c1-drag-960.png' });
  await page.keyboard.press('Escape'); await page.mouse.up();
  await expect(page.locator('.activity-drag-preview')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('相邻移动前保存失败保留输入，再拖动可重试；切换日期取消拖动', async ({ page, localApp }) => {
  const trip = await setup(page, localApp);
  const original = await trip.read();
  let moves = 0;
  page.on('request', request => { if (request.url().endsWith('/trip/activities/move')) moves++; });
  await page.route('**/conversations/' + trip.id, route => route.request().method() === 'PUT'
    ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定测试：草稿暂时无法写入。' } }) })
    : route.continue());
  await page.getByLabel('备注', { exact: true }).nth(1).fill('保存失败后仍保留');
  await beginDrag(page, 1);
  const first = page.locator('[data-activity-id]').first();
  await first.scrollIntoViewIfNeeded();
  const box = (await first.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 - 24, { steps: 8 });
  await expect(first).toHaveClass(/drop-before/);
  await page.mouse.up();
  await expect(page.getByRole('alert')).toContainText('草稿暂时无法写入');
  await expect(page.getByRole('dialog', { name: '移动活动', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('备注', { exact: true }).nth(1)).toHaveValue('保存失败后仍保留');
  expect((await trip.read()).data).toEqual(original.data);
  expect(moves).toBe(0);
  await page.unroute('**/conversations/' + trip.id);
  await dragTo(page, 1, 0, true);
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('0');
  await page.getByLabel('移动后开始时间').fill('07:00');
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.locator('[data-activity-id]').first()).toHaveAttribute('data-activity-id', trip.formal.data.days[0].activities[1].id);
  expect((await trip.read()).data.draft[0].activities[0].description).toBe('保存失败后仍保留');
  const start = await beginDrag(page);
  await page.mouse.move(start.x - 15, start.y);
  await expect(page.locator('.activity-drag-preview')).toBeVisible();
  const tab = page.getByRole('dialog').getByRole('button', { name: '第 2 天', exact: true });
  await tab.focus(); await tab.press('Enter');
  await expect(page.locator('.activity-drag-preview')).toHaveCount(0);
  await page.mouse.up();
  await expect(tab).toHaveClass('selected');
  expect(moves).toBe(1);
  expect((await trip.read()).data.days).toEqual(trip.formal.data.days);
});

function crossDays(data: WorkspaceData) {
  const target = structuredClone(data.days[0].activities[2]);
  target.id = 'day2-garden'; target.time = '10:00';
  data.days[1].activities = [target];
  for (let index = 1; index < data.days[0].activities.length; index++) {
    const activities = data.days[0].activities;
    activities[index].incomingRoute = { fromPlaceId: activities[index - 1].place!.id, toPlaceId: activities[index].place!.id,
      mode: 'walking', status: 'verified', durationMinutes: 15, distanceMeters: 500, provider: 'baidu', queriedAt: new Date().toISOString() };
  }
}

async function hoverDate(page: Page, from: number, targetDay: number) {
  const start = await beginDrag(page, from);
  await page.mouse.move(start.x - 12, start.y, { steps: 2 });
  const tab = page.locator(`[data-drop-day="${targetDay}"]`);
  await tab.scrollIntoViewIfNeeded();
  const box = (await tab.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 8 });
  await expect(tab).toHaveClass(/editor-day-drop-target/);
  return tab;
}

async function dragToDate(page: Page, from: number, targetDay: number) {
  await hoverDate(page, from, targetDay);
  await page.mouse.up();
  await expect(page.getByRole('dialog', { name: '移动活动', exact: true })).toBeVisible();
  await expect(page.getByLabel('目标日期', { exact: true })).toHaveValue(String(targetDay));
}

test('跨日拖拽只预填目标日：取消、迟到响应、应用重启与固定聊天续改保持身份', async ({ page, localApp }) => {
  test.setTimeout(120_000);
  const trip = await setup(page, localApp, crossDays);
  const first = trip.formal.data.days[0].activities[0];
  await page.getByLabel('备注', { exact: true }).first().fill('跨日后仍保留的备注');
  await page.getByLabel('活动费用（元）', { exact: true }).first().fill('42.50');
  await hoverDate(page, 0, 2);
  const hovering = await trip.read();
  await page.waitForTimeout(1100);
  expect((await trip.read()).revision).toBe(hovering.revision);
  await expect(page.getByRole('dialog').getByRole('button', { name: '第 1 天', exact: true })).toHaveClass('selected');
  await expect(page.locator('[data-activity-id]')).toHaveCount(3);
  await expect(page.locator('.editor-activity--drop-after')).toHaveCount(0);
  await page.screenshot({ path: 'artifacts/c2-date-hover-1440.png' });
  await page.mouse.up();
  await expect(page.getByLabel('目标日期', { exact: true })).toHaveValue('2');
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('1');
  await expect(page.getByLabel('移动后开始时间')).toHaveValue('09:00');
  await page.getByRole('button', { name: '返回编辑', exact: true }).click();
  await expect(page.locator('#drag-trip-activity-' + first.id)).toBeFocused();
  expect((await trip.read()).data.days).toEqual(trip.formal.data.days);
  await expect(page.getByLabel('备注', { exact: true }).first()).toHaveValue('跨日后仍保留的备注');
  await dragToDate(page, 0, 2);
  await page.getByLabel('移动后开始时间').fill('12:00');
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let calls = 0;
  await page.route('**/trip/activities/move', async route => {
    calls++;
    expect(route.request().postDataJSON()).toMatchObject({ activity_id: first.id, target_day: 2, target_index: 1, time: '12:00' });
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: '移动到草稿', exact: true }).dblclick();
  await expect.poll(() => calls).toBe(1);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: '移动活动', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '关闭窗口', exact: true })).toBeDisabled();
  await expect(page.getByLabel('目标日期', { exact: true })).toBeDisabled();
  release();
  await expect(page.locator('#drag-trip-activity-' + first.id)).toBeFocused();
  await expect(page.getByRole('dialog').getByRole('button', { name: '第 2 天', exact: true })).toHaveClass('selected');
  let saved = await trip.read();
  expect(saved.data.days).toEqual(trip.formal.data.days);
  expect(saved.data.draft.map((day: { activities: unknown[] }) => day.activities.length)).toEqual([2, 2]);
  const moved = saved.data.draft[1].activities[1];
  expect(moved).toMatchObject({ id: first.id, time: '12:00', description: '跨日后仍保留的备注', cost: { amountFen: 4250 }, place: first.place });
  expect(saved.data.draft[0].activities[0].incomingRoute).toBeUndefined();
  expect(saved.data.draft[0].activities[1].incomingRoute).toEqual(trip.formal.data.days[0].activities[2].incomingRoute);
  expect(moved.incomingRoute).toBeUndefined();
  expect(saved.data.candidates).toEqual(trip.formal.data.candidates);
  expect(saved.data.favorites).toEqual(trip.formal.data.favorites);
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  saved = await trip.read();
  expect(saved.data.days[1].activities[1].incomingRoute).toMatchObject({ fromPlaceId: saved.data.days[1].activities[0].place.id, toPlaceId: first.place.id, status: 'unknown' });
  await localApp.restart();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  await page.reload();
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(page.locator('.activity-heading')).toHaveCount(2);
  expect((await trip.read()).data.days).toEqual(saved.data.days);
  await page.getByLabel('旅行对话输入').fill('调整固定行程，保持身份');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByRole('button', { name: '继续编辑草稿', exact: true })).toBeEnabled();
  const edited = await trip.read();
  expect(edited.data.draft.map((day: { activities: unknown[] }) => day.activities.length)).toEqual([2, 2]);
  expect(edited.data.draft[1].activities.find((activity: { id: string }) => activity.id === first.id)).toMatchObject({ id: first.id, time: '12:15', cost: moved.cost, description: moved.description });
  expect(edited.data.days).toEqual(saved.data.days);
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await page.getByRole('button', { name: '放弃修改', exact: true }).click();
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect.poll(async () => (await trip.read()).data.days).toEqual(trip.formal.data.days);
  expect((await trip.read()).data.candidates).toEqual(trip.formal.data.candidates);
});

test('跨日拖拽可移空源日并移入空日，960 窗口与键盘入口可用', async ({ page, localApp }) => {
  await page.setViewportSize({ width: 960, height: 640 });
  const trip = await setup(page, localApp, data => { data.days[0].activities = data.days[0].activities.slice(0, 1); data.days[1].activities = []; });
  const first = trip.formal.data.days[0].activities[0];
  await dragToDate(page, 0, 2);
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('0');
  await page.screenshot({ path: 'artifacts/c2-empty-day-960.png' });
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.locator('#drag-trip-activity-' + first.id)).toBeFocused();
  let saved = await trip.read();
  expect(saved.data.draft.map((day: { activities: unknown[] }) => day.activities.length)).toEqual([0, 1]);
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  saved = await trip.read();
  await localApp.restart(); await page.reload();
  await page.getByRole('tab', { name: '第 1 天', exact: true }).click();
  await expect(page.locator('.activity-heading')).toHaveCount(0);
  await expect(page.locator('.real-map-marker')).toHaveCount(0);
  expect((await trip.read()).data.days).toEqual(saved.data.days);
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await expect(page.locator('.editor-empty-day')).toContainText('自由安排');
  await page.getByRole('dialog').getByRole('button', { name: '第 2 天', exact: true }).press('Enter');
  await page.locator('#drag-trip-activity-' + first.id).press('Space');
  await page.getByLabel('目标日期', { exact: true }).selectOption('1');
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('0');
  await page.getByRole('button', { name: '返回编辑', exact: true }).press('Enter');
  await expect(page.locator('#drag-trip-activity-' + first.id)).toBeFocused();
  await page.getByRole('button', { name: '放弃修改', exact: true }).click();
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect.poll(async () => (await trip.read()).data.days).toEqual(trip.formal.data.days);
});

for (const blocked of ['full', 'duplicate'] as const) test(`跨日目标 ${blocked} 不覆盖草稿，保持日期和位置可修改`, async ({ page, localApp }) => {
  const trip = await setup(page, localApp, data => {
    crossDays(data);
    if (blocked === 'full') data.days[1].activities = Array.from({ length: 8 }, (_, index) => {
      const activity = structuredClone(data.days[1].activities[0]);
      activity.id = 'full-' + index; activity.time = `${String(index + 8).padStart(2, '0')}:00`;
      activity.place!.id = 'baidu:full-' + index;
      return activity;
    });
    else {
      data.days[1].activities[0].place = structuredClone(data.days[0].activities[0].place);
      data.days[1].activities[0].title = data.days[0].activities[0].title;
    }
  });
  await dragToDate(page, 0, 2);
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue(blocked === 'full' ? '8' : '1');
  const before = await trip.read();
  if (blocked === 'full') {
    await expect(page.getByRole('alert')).toContainText('已有 8 项活动');
    await expect(page.getByRole('button', { name: '移动到草稿', exact: true })).toBeDisabled();
  } else {
    await page.getByLabel('移动后开始时间').fill('13:00');
    await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText(/已安排|重复|已有/);
  }
  expect((await trip.read()).data).toEqual(before.data);
  await page.getByLabel('目标日期', { exact: true }).selectOption('1');
  await expect(page.getByRole('button', { name: '移动到草稿', exact: true })).toBeEnabled();
});

test('跨日时间与旧修订冲突保持表单，取消拖拽后可切换会话', async ({ page, localApp }) => {
  const other = await localApp.request('/conversations', 'POST', { title: 'C2 切换后的独立会话', data: { schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [] } });
  const trip = await setup(page, localApp, crossDays);
  await dragToDate(page, 0, 2);
  const before = await trip.read();
  for (const time of ['10:30', '23:30']) {
    await page.getByLabel('移动后开始时间').fill(time);
    await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText(/时间|重叠|午夜/);
    expect((await trip.read()).data).toEqual(before.data);
  }
  const latest = await localApp.request('/conversations/' + trip.id, 'PUT', { expected_revision: before.revision, title: before.title, data: before.data });
  await page.getByLabel('移动后开始时间').fill('12:00');
  await page.getByRole('button', { name: '移动到草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('会话已被更新');
  await expect(page.getByLabel('目标日期', { exact: true })).toHaveValue('2');
  await expect(page.getByLabel('插入位置', { exact: true })).toHaveValue('1');
  expect((await trip.read()).revision).toBe(latest.revision);
  await page.reload();
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  let calls = 0;
  page.on('request', request => { if (request.url().endsWith('/trip/activities/move')) calls++; });
  await hoverDate(page, 0, 2);
  await page.keyboard.press('Escape'); await page.mouse.up();
  await expect(page.locator('.editor-day-drop-target')).toHaveCount(0);
  await expect(page.getByRole('dialog', { name: '编辑当天行程', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '历史旅行', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '历史旅行', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /C2 切换后的独立会话/ }).click();
  await expect(page.locator('.trip-title')).toHaveText('C2 切换后的独立会话');
  expect((await localApp.request('/conversations/' + other.id)).data.days).toEqual([]);
  expect(calls).toBe(0);
  expect((await trip.read()).data.days).toEqual(trip.formal.data.days);
});
