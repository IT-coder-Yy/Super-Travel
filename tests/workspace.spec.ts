import type { Page } from '@playwright/test';
import { test, expect } from './fixtures/local-app';
import { fileURLToPath } from 'node:url';
import { createExample } from '../src/types';

test('新会话保持空白，等待用户生成行程而非沿用示例', async ({ page, localApp }, info) => {
  await localApp.reset();
  await page.route('https://tile.openstreetmap.org/**', route => route.abort());
  await page.goto('/');
  await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '旅程', exact: true }).click();
  await page.getByLabel('会话名称').fill('空白行程验证');
  const [created] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith('/api/conversations') && response.request().method() === 'POST'),
    page.getByRole('button', { name: '创建本地会话', exact: true }).click(),
  ]);
  const document = await created.json();
  expect(document.data.days).toEqual([]);
  await expect(page.getByRole('heading', { name: '下一站，想去哪里？' })).toBeVisible();
  await expect(page.locator('.example-badge')).toHaveCount(0);
  await expect(page.locator('.photo')).toHaveCount(0);
  await expect(page.getByLabel('旅行对话输入')).toBeVisible();
  await page.getByRole('button', { name: '关闭提示', exact: true }).click();
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: width === 1440 ? 960 : 768 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await expect(page.getByLabel('旅行对话输入')).toBeVisible();
    await page.screenshot({ path: info.outputPath(`empty-${width}.png`) });
  }
  await page.reload();
  await expect(page.getByRole('heading', { name: '下一站，想去哪里？' })).toBeVisible();
  expect((await localApp.request(`/conversations/${document.id}`)).data.days).toEqual([]);
});

async function captureLoadedWorkspace(page: Page, path: string) {
  await expect(page.locator('.photo')).toHaveCount(5);
  await expect(page.locator('.photo[aria-busy="true"]')).toHaveCount(0, { timeout: 30_000 });
  await expect(page.locator('.photo-fallback')).toHaveCount(0);
  await expect(page.locator('.photo--loaded')).toHaveCount(5);
  const images = await page.locator('.photo img').evaluateAll(elements => elements.map(element => {
    const image = element as HTMLImageElement;
    return { complete: image.complete, width: image.naturalWidth, height: image.naturalHeight };
  }));
  expect(images.every(image => image.complete && image.width > 0 && image.height > 0)).toBe(true);
  await page.screenshot({ path, fullPage: true });
}

test.describe('本地业务回归（固定图片）', () => {
  test.beforeEach(async ({ page, localApp }, testInfo) => {
    await localApp.reset();
    await page.route('https://thumb.wikimedia.org/**/960px-Sunset_over_the_Zigzag_Bridge*.jpg', route => route.fulfill({ path: fileURLToPath(new URL('./fixtures/west-lake-sunset.jpg', import.meta.url)), contentType: 'image/jpeg' }));
    await page.route('https://thumb.wikimedia.org/**/500px-West_lake_twilight.jpg', route => route.fulfill({ path: fileURLToPath(new URL('./fixtures/west-lake-twilight.jpg', import.meta.url)), contentType: 'image/jpeg' }));
    await page.goto('/');
    await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '旅程', exact: true }).click();
    const name = `界面验证 ${testInfo.testId.slice(-6)} ${Date.now()}`;
    await page.getByLabel('会话名称').fill(name);
    await page.getByRole('button', { name: '创建本地会话', exact: true }).click();
    await expect(page.locator('.trip-title')).toHaveText(name);
    await expect(page.locator('.draft-label')).toHaveCount(0);
    const document = (await localApp.request('/conversations')).find((item: { title: string }) => item.title === name);
    await localApp.request(`/conversations/${document.id}`, 'PUT', { title: name, expected_revision: document.revision, data: createExample() });
    await page.reload();
    await expect(page.locator('.example-badge')).toHaveText('示例行程');
  });


  test('示例工作台可编辑、保存并在刷新后恢复，更新状态诚实展示', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await captureLoadedWorkspace(page, 'artifacts/workspace-fixture-desktop.png');

    await page.getByRole('tab', { name: '第 2 天', exact: true }).click();
    await expect(page.getByRole('tab', { name: '第 2 天', exact: true })).toHaveAttribute('aria-selected', 'true');
    await page.getByRole('tab', { name: '第 1 天', exact: true }).click();
    await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
    await page.getByLabel('地点', { exact: true }).first().fill('断桥慢游（验证）');
    await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
    await expect(page.locator('.storage-indicator')).toContainText('已保存到本机', { timeout: 15_000 });
    await page.reload();
    await expect(page.locator('.draft-label')).toHaveText('修改草稿');
    await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
    await expect(page.getByLabel('地点', { exact: true }).first()).toHaveValue('断桥慢游（验证）');
    await page.getByRole('button', { name: '放弃修改', exact: true }).click();
    await expect(page.locator('.activity-heading').first()).toContainText('断桥残雪');

    await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
    const notes = page.getByLabel('备注', { exact: true }).first();
    await notes.fill('保存与恢复验证：留半小时休息。');
    let releaseSave = () => {};
    const saveGate = new Promise<void>(resolve => { releaseSave = resolve; });
    await page.route('**/api/conversations/*', async route => {
      const request = route.request();
      if (request.method() === 'PUT' && request.postDataJSON().data.draft === null) await saveGate;
      await route.continue();
    });
    try {
      await page.getByRole('button', { name: '应用修改', exact: true }).click();
      await expect(notes).toBeDisabled();
      await expect(page.getByRole('button', { name: '放弃修改', exact: true })).toBeDisabled();
      await expect(page.getByRole('button', { name: '关闭窗口', exact: true })).toBeDisabled();
    } finally { releaseSave(); }
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await page.unroute('**/api/conversations/*');
    await expect(page.locator('.draft-label')).toHaveCount(0);
    await page.getByLabel('旅行对话输入').fill('我想放慢节奏');
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('请先在设置中配置模型连接');
    await expect(page.getByLabel('旅行对话输入')).toHaveValue('我想放慢节奏');
    await expect(page.locator('.message--user')).toHaveCount(0);
    await expect(page.locator('.storage-indicator')).toContainText('已保存到本机', { timeout: 15_000 });
    await page.reload();
    await expect(page.locator('.activity-description').first()).toHaveText('保存与恢复验证：留半小时休息。');
    await page.getByRole('button', { name: /旅伴对话/ }).click();
    await expect(page.locator('.message--user')).toHaveCount(0);
    await expect(page.getByText('可以聊天、补充需求或查询旅行资料。当前展示的是示例行程。')).toBeVisible();

    await page.getByRole('button', { name: '设置与更新', exact: true }).click();
    await page.getByRole('button', { name: '检查更新', exact: true }).click();
    await expect(page.getByText(/更新源尚未配置/)).toBeVisible();
    await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
    await expect(page.getByText(/示意地图与示例行程/)).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('较小窗口保持主要控件可见且无横向溢出', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await expect(page.getByLabel('旅行对话输入')).toBeVisible();
    await expect(page.getByRole('button', { name: '编辑当天行程', exact: true })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(overflow).toBe(false);
    await captureLoadedWorkspace(page, 'artifacts/workspace-fixture-compact.png');
  });
});

test('真实 Wikimedia 照片与截图（显式联网）', async ({ page, localApp }, testInfo) => {
  test.skip(process.env.STA_LIVE_PHOTOS !== '1', '设置 STA_LIVE_PHOTOS=1 后单独验证远程图片，业务回归使用固定图片。');
  const evidence: { url: string; status?: number; failure?: string }[] = [];
  page.on('response', response => {
    if (response.url().startsWith('https://thumb.wikimedia.org/')) evidence.push({ url: response.url(), status: response.status() });
  });
  page.on('requestfailed', request => {
    if (request.url().startsWith('https://thumb.wikimedia.org/')) evidence.push({ url: request.url(), failure: request.failure()?.errorText });
  });
  try {
    await localApp.request('/conversations', 'POST', { title: '显式照片验证示例', data: createExample() });
    await page.goto('/');
    await expect(page.locator('.startup-intro')).toHaveCount(0);
    await page.locator('.header-tagline').click();
    await captureLoadedWorkspace(page, 'artifacts/preview-network.png');
  } finally {
    await testInfo.attach('wikimedia-network', { body: Buffer.from(JSON.stringify(evidence, null, 2)), contentType: 'application/json' });
  }
});
