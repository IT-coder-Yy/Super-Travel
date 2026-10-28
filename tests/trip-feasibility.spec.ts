import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect, type LocalApp } from './fixtures/local-app';

test.use({ reducedMotion: 'reduce' });

async function createTrip(page: Page, localApp: LocalApp) {
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>' }));
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const document = await localApp.request('/conversations', 'POST', { title: '路线费用固定回归', data: { schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [] } });
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(page.locator('.trip-title')).toHaveText('路线费用固定回归');
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await expect(page.getByLabel('旅行对话输入')).toHaveValue('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  return { read: () => localApp.request(`/conversations/${document.id}`), path: resolve(localApp.dataDir, 'conversations', `${document.id}.json`) };
}

test('费用按用户填写汇总：空值未知、0 元免费、小数输入及重启恢复', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await createTrip(page, localApp);
  await expect(page.getByLabel('行程费用')).toContainText('另有 4 项活动费用未知');
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  const costs = page.getByLabel('活动费用（元）', { exact: true });
  await costs.first().fill('0');
  await costs.nth(1).pressSequentially('12.50');
  expect(Number(await costs.nth(1).inputValue())).toBe(12.5);
  await expect(page.getByLabel('行程费用')).toContainText('¥12.50');
  await expect(page.getByLabel('行程费用')).toContainText('另有 2 项活动费用未知');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const saved = await trip.read();
  expect(saved.data.days[0].activities[0].cost).toEqual({ amountFen: 0, currency: 'CNY', source: 'user' });
  expect(saved.data.days[0].activities[1].cost.amountFen).toBe(1250);
  await localApp.restart();
  await page.reload();
  await expect(page.getByLabel('行程费用')).toContainText('¥12.50');
  await expect(page.locator('.activity-cost').first()).toHaveText('¥0.00 · 用户填写');

  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByLabel('地点', { exact: true }).first().fill('固定测试花园');
  await expect(costs.first()).toHaveValue('');
  await expect(costs.nth(1)).toHaveValue('12.5');
  await expect(page.getByLabel('行程费用')).toContainText('另有 3 项活动费用未知');
  await page.getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.locator('.activity-cost').first()).toHaveText('¥0.00 · 用户填写');
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.screenshot({ path: 'artifacts/trip-costs-1024.png', fullPage: true });
});

test('查询路线显示、时间不足拦截、排序清理及过期证据兼容', async ({ page, localApp }) => {
  test.setTimeout(90_000);
  const trip = await createTrip(page, localApp);
  const document = await trip.read();
  document.data.trip.transportMode = 'transit';
  const activities = document.data.days[0].activities;
  // Explicit fixture evidence, never a real provider query. The real API still validates edits.
  activities[1].incomingRoute = { fromPlaceId: activities[0].place.id, toPlaceId: activities[1].place.id, mode: 'transit', status: 'verified', durationMinutes: 20, distanceMeters: 1200, provider: 'baidu', queriedAt: new Date().toISOString() };
  await writeFile(trip.path, JSON.stringify(document), 'utf8');
  await page.reload();
  await expect(page.locator('.activities .trip-leg')).toContainText('公共交通约 20 分钟 · 1.2 公里');
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByLabel('时间', { exact: true }).nth(1).fill('10:10');
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('接驳时间不足');
  await expect(page.getByRole('button', { name: '应用修改', exact: true })).toBeDisabled();
  await page.screenshot({ path: 'artifacts/trip-conflict-editor.png', fullPage: true });
  expect((await trip.read()).data.days[0].activities[1].time).toBe('11:00');
  await page.getByLabel('时间', { exact: true }).nth(1).fill('10:30');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await trip.read()).data.days[0].activities[1].time).toBe('10:30');
  expect((await trip.read()).data.days[0].activities[1].incomingRoute.status).toBe('verified');
  await page.screenshot({ path: 'artifacts/trip-routes-1440.png', fullPage: true });

  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByRole('button', { name: '上移固定测试博物馆', exact: true }).click();
  await expect(page.getByRole('dialog').locator('.trip-leg')).toContainText('接驳耗时待查询');
  await page.getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.locator('.storage-indicator')).toContainText('已保存到本机');
  const expired = await trip.read();
  expired.data.days[0].activities[1].incomingRoute.queriedAt = new Date(Date.now() - 48 * 60 * 60 * 1000).toISOString();
  await writeFile(trip.path, JSON.stringify(expired), 'utf8');
  await page.reload();
  await expect(page.locator('.activities .trip-leg')).toContainText('接驳耗时已过期');
});

test('旧行程缺少路线和费用字段时可正常读取，未知数据不显示为免费或已核查', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  const legacy = await trip.read();
  delete legacy.data.trip.transportMode;
  for (const day of legacy.data.days) for (const activity of day.activities) {
    delete activity.cost;
    delete activity.incomingRoute;
    delete activity.place.sourceLocation;
  }
  await writeFile(trip.path, JSON.stringify(legacy), 'utf8');
  await page.reload();
  await expect(page.locator('.activity-heading')).toHaveCount(2);
  await expect(page.getByLabel('行程费用')).toContainText('另有 4 项活动费用未知');
  await expect(page.locator('.activities .trip-leg')).toContainText('接驳耗时待查询');
  await expect(page.locator('.activity-cost').first()).toHaveText('活动费用未知');
});
