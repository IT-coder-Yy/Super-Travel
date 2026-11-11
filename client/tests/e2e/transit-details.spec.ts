import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect, type LocalApp } from '../fixtures/local-app';

test.use({ reducedMotion: 'reduce' });

async function setup(page: Page, localApp: LocalApp, provider: 'baidu' | 'amap' = 'baidu') {
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>' }));
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const doc = await localApp.request('/conversations', 'POST', { title: '公交详情离线验收', data: { schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [] } });
  const read = () => localApp.request(`/conversations/${doc.id}`);
  const path = resolve(localApp.dataDir, 'conversations', `${doc.id}.json`);
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(page.locator('.trip-title')).toHaveText('公交详情离线验收');
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.activity-heading').first()).toContainText('固定测试公园');
  const result = await read();
  result.data.trip.transportMode = 'transit';
  for (const day of result.data.days) {
    for (const a of day.activities) {
      a.place.provider = provider;
      a.place.id = a.place.id.replace('baidu:', `${provider}:`);
      if (provider === 'amap') delete a.place.sourceLocation;
    }
    const [first, second] = day.activities;
    second.cost = { amountFen: 1250, currency: 'CNY', source: 'user' };
    second.incomingRoute = { fromPlaceId: first.place.id, toPlaceId: second.place.id, mode: 'transit', status: 'verified', durationMinutes: 42, distanceMeters: 12248, provider, queriedAt: new Date().toISOString(),
      transit: { segments: [{ mode: 'walking' }, { mode: 'subway', lineName: '固定地铁1号线', departureStop: '火车东站(B口)', arrivalStop: '龙翔桥站' }, { mode: 'bus', lineName: '固定观光巴士', departureStop: '超长中文换乘站名'.repeat(12), arrivalStop: '净寺站' }], transferCount: 1, fare: { amountFen: 2900, currency: 'CNY' } } };
  }
  await writeFile(path, JSON.stringify(result), 'utf8');
  await page.reload();
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  return { read, path, original: result };
}

test('公交详情可键盘展开，费用独立，草稿应用、聊天续改、重启和上一版保持证据', async ({ page, localApp }) => {
  test.setTimeout(120_000);
  const trip = await setup(page, localApp);
  const details = page.locator('.activities .transit-details');
  await expect(details.locator('summary')).toContainText('参考总票价 ¥29.00');
  await details.locator('summary').focus();
  await page.keyboard.press('Enter');
  await expect(details).toHaveAttribute('open', '');
  await expect(details).toContainText('换乘 1 次');
  await expect(details).toContainText('火车东站(B口) 上车 → 龙翔桥站 下车');
  await expect(page.getByLabel('行程费用')).toContainText('¥25.00');
  expect(await page.locator('.activity button, .activity details').count()).toBe(0);
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await details.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy();
    await details.locator("p").last().scrollIntoViewIfNeeded();
    await expect(details.locator("p").last()).toBeVisible();
    await page.screenshot({ path: `artifacts/c4-transit-${width}.png`, fullPage: true });
  }
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  const editor = page.getByRole('dialog');
  await editor.locator('.transit-details summary').click();
  await expect(editor.locator('.transit-details')).toContainText('净寺站 下车');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(editor).toHaveCount(0);
  let current = await trip.read();
  const evidence = trip.original.data.days[0].activities[1].incomingRoute;
  expect(current.data.days[0].activities[1].incomingRoute).toEqual(evidence);
  await page.getByLabel('旅行对话输入').fill('生成固定行程，保持身份，稍微延后');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.draft-label')).toBeVisible();
  current = await trip.read();
  expect(current.data.draft[0].activities[1].incomingRoute).toEqual(evidence);
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await page.getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(editor).toHaveCount(0);
  expect((await trip.read()).data.draft).toBeNull();
  await localApp.restart();
  await page.reload();
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(details.locator('summary')).toContainText('¥29.00');
  expect((await trip.read()).data.days[0].activities[1].incomingRoute).toEqual(evidence);
  await page.getByRole('button', { name: '恢复上一版行程', exact: true }).click();
  await expect(page.locator('.storage-indicator')).toContainText('已保存到本机');
  expect((await trip.read()).data.days[0].activities[1].incomingRoute).toEqual(evidence);
});

test('零报价原样展示并注明仅供参考，兼容缺失、旧记录与过期地点变化', async ({ page, localApp }) => {
  const trip = await setup(page, localApp, 'amap');
  const details = page.locator('.activities .transit-details');
  const rewrite = async (edit: (route: any) => void) => {
    const doc = await trip.read();
    edit(doc.data.days[0].activities[1].incomingRoute);
    await writeFile(trip.path, JSON.stringify(doc), 'utf8');
    await page.reload();
    await expect(page.locator('.startup-intro')).toHaveCount(0);
  };
  await rewrite(route => { route.transit.fare.amountFen = 0; });
  await expect(details.locator('summary')).toContainText('参考总票价 ¥0.00（仅供参考，以实际结果为准）');
  await expect(details).not.toContainText('参考总票价未知');
  await expect(details).not.toContainText('是否免费待确认');
  await details.locator('summary').click();
  await expect(details).toContainText('高德地图');
  await rewrite(route => { delete route.transit.fare; delete route.transit.segments[1].departureStop; });
  await expect(details.locator('summary')).toContainText('参考总票价未知');
  await details.locator('summary').click();
  await expect(details).toContainText('上车站待确认');
  await rewrite(route => { delete route.transit; });
  await details.locator('summary').click();
  await expect(details).toContainText('公交详情未查询');
  await rewrite(route => { route.transit = trip.original.data.days[0].activities[1].incomingRoute.transit; route.queriedAt = new Date(Date.now() - 48 * 3600000).toISOString(); });
  await expect(details).toHaveCount(0);
  await expect(page.locator('.activities .trip-leg')).toContainText('已过期');
  await rewrite(route => { route.queriedAt = new Date().toISOString(); route.fromPlaceId = 'amap:wrong'; });
  await expect(details).toHaveCount(0);
  await expect(page.locator('.activities .trip-leg')).toContainText('待查询');
  await expect(page.getByLabel('行程费用')).toContainText('¥25.00');
});
