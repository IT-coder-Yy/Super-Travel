import { test, expect, type Page } from '@playwright/test';

test.beforeEach(async ({ page }, testInfo) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '旅程', exact: true }).click();
  const name = `界面验证 ${testInfo.testId.slice(-6)} ${Date.now()}`;
  await page.getByLabel('会话名称').fill(name);
  await page.getByRole('button', { name: '创建本地会话', exact: true }).click();
  await expect(page.locator('.trip-title')).toHaveText(name);
  await expect(page.locator('.draft-label')).toHaveCount(0);
  await page.getByRole('button', { name: '关闭提示', exact: true }).click();
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

test('示例工作台可编辑、保存并在刷新后恢复，更新状态诚实展示', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await captureLoadedWorkspace(page, 'artifacts/preview-desktop.png');

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
  await expect(page.getByText('我想放慢节奏', { exact: true })).toBeVisible();
  await expect(page.locator('.storage-indicator')).toContainText('已保存到本机', { timeout: 15_000 });
  await page.reload();
  await expect(page.locator('.activity-description').first()).toHaveText('保存与恢复验证：留半小时休息。');
  await page.getByRole('button', { name: /旅伴对话/ }).click();
  await expect(page.getByText('我想放慢节奏', { exact: true })).toBeVisible();
  await expect(page.getByText(/尚未连接规划 Agent/)).toBeVisible();

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
  await captureLoadedWorkspace(page, 'artifacts/preview-compact.png');
});
