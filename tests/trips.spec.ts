import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { test, expect } from './fixtures/local-app';

test.use({ reducedMotion: 'reduce' });

test('真实业务链路的离线回归：生成、地图点、聊天草稿、手动改点、保存与重启恢复', async ({ page, localApp }) => {
  test.setTimeout(100_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  // An explicit offline tile fixture; live map availability is checked separately.
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({
    contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>',
  }));
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const original = await localApp.request('/conversations', 'POST', { title: '固定行程闭环测试', data: {
    schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [],
  } });
  const read = () => localApp.request(`/conversations/${original.id}`);
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
  await expect(page.locator('.trip-title')).toHaveText('固定行程闭环测试');
  await expect(page.locator('.activity')).toHaveCount(0);
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await expect(page.getByLabel('旅行对话输入')).toHaveValue('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  await expect(page.getByRole('tab', { name: '第 2 天', exact: true })).toBeVisible();
  await expect(page.locator('.real-map-marker')).toHaveCount(2);
  await expect(page.locator('.photo')).toHaveCount(0);
  const formal = await read();
  expect(formal.data.trip.destination).toBe('杭州');
  expect(formal.data.draft).toBeNull();
  expect(formal.data.days[0].activities[0].place.id).toBe('baidu:fixture-park');

  await page.getByLabel('旅行对话输入').fill('调整固定行程，放慢节奏');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.draft-label')).toHaveText('修改草稿');
  const pending = await read();
  expect(pending.data.days).toEqual(formal.data.days);
  expect(pending.data.draft[0].activities[0].description).toContain('固定测试调整');
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.draft-label')).toHaveCount(0);
  expect((await read()).data.previousTrip.days).toEqual(formal.data.days);

  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByLabel('地点', { exact: true }).first().fill('固定测试花园');
  await page.getByRole('button', { name: '重新定位', exact: true }).first().click();
  await expect(page.locator('.editor-location').first()).toContainText('固定测试花园地址');
  await page.getByLabel('备注', { exact: true }).first().fill('用户修改后保存的真实业务数据（固定测试）');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试花园');
  const saved = await read();
  expect(saved.data.days[0].activities[0].place.id).toBe('baidu:fixture-garden');
  const disk = JSON.parse(await readFile(resolve(localApp.dataDir, 'conversations', `${saved.id}.json`), 'utf8'));
  expect(disk.data.days).toEqual(saved.data.days);
  expect(JSON.stringify(disk)).not.toContain('fixture-key-not-real');
  await localApp.restart();
  await page.reload();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试花园');
  await expect(page.locator('.real-map-marker')).toHaveCount(2);
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  expect(errors).toEqual([]);
  await page.screenshot({ path: 'artifacts/trip-workflow-fixture.png', fullPage: true });
});
