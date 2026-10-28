import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { test, expect } from './fixtures/local-app';

test.describe('工具服务地址设置（真实本地配置 API，无外部查询）', () => {
  test.use({ reducedMotion: 'reduce' });

  test.afterEach(async ({ localApp }) => {
    // Restore the fixture tools for subsequent suites; retain the preferences to test persistence.
    await localApp.restart();
  });

  test('默认本机地址，离线仍可保存；重开和重启恢复，错误地址不覆盖配置', async ({ page, localApp }, info) => {
    await page.route('**/api/tools/status', route => route.fulfill({ status: 502, json: { detail: { message: '固定离线测试' } } }));
    await page.goto('/');
    await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '设置与更新', exact: true }).click();
    const address = page.getByLabel('工具服务地址', { exact: true });
    await expect(address).toHaveValue('http://127.0.0.1:8080');
    await expect(page.getByText('暂时无法读取工具服务状态。请启动本机工具服务，或检查服务地址和网络。', { exact: true })).toBeVisible();
    await expect(address).toBeEnabled();
    await expect(page.locator('input[type="password"]')).toHaveCount(1);

    const savedUrl = 'http://127.0.0.1:9';
    await address.fill(savedUrl);
    const [saved] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/tools/configure')),
      page.getByRole('button', { name: '保存工具服务地址', exact: true }).click(),
    ]);
    expect(saved.ok()).toBe(true);
    expect(saved.request().postDataJSON()).toEqual({ base_url: savedUrl });
    await expect(page.getByText('工具服务地址已保存，立即生效。可检查连接。', { exact: true })).toBeVisible();
    expect(await localApp.request('/tools/settings')).toMatchObject({ baseUrl: savedUrl, mode: 'shared' });
    const disk = await readFile(resolve(localApp.dataDir, 'tools.json'), 'utf8');
    expect(disk).toContain(savedUrl);
    expect(disk).not.toMatch(/api.?key|token|secret/i);

    await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
    await page.getByRole('button', { name: '设置与更新', exact: true }).click();
    await expect(address).toHaveValue(savedUrl);
    await localApp.restart();
    await page.reload();
    await page.getByRole('button', { name: '设置与更新', exact: true }).click();
    await expect(address).toHaveValue(savedUrl);

    await address.fill('http://tools.example.invalid');
    const [rejected] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/tools/configure')),
      page.getByRole('button', { name: '保存工具服务地址', exact: true }).click(),
    ]);
    expect(rejected.ok()).toBe(false);
    await expect(page.locator('.tool-connections').getByRole('alert').first()).toBeVisible();
    expect((await localApp.request('/tools/settings')).baseUrl).toBe(savedUrl);
    expect(await readFile(resolve(localApp.dataDir, 'tools.json'), 'utf8')).toBe(disk);
    await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
    await page.getByRole('button', { name: '设置与更新', exact: true }).click();
    await expect(address).toHaveValue(savedUrl);
    await address.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('tool-settings-offline.png') });
  });

  test('状态查询未返回也能保存地址，旧服务的迟到状态不会覆盖新配置', async ({ page, localApp }) => {
    let resolveStatus!: () => void;
    const pendingStatus = new Promise<void>(resolve => { resolveStatus = resolve; });
    let statusStarted!: () => void;
    const started = new Promise<void>(resolve => { statusStarted = resolve; });
    await page.route('**/api/tools/status', async route => {
      statusStarted();
      await pendingStatus;
      await route.fulfill({ json: { providers: [{ id: 'baidu', label: '旧服务迟到状态', kind: 'mcp', configured: true, connection: 'connected', toolCount: 1, message: '', lastCall: null }] } }).catch(() => undefined);
    });
    try {
      await page.goto('/');
      await expect(page.getByRole('button', { name: '保存行程', exact: true })).toBeEnabled();
      await page.getByRole('button', { name: '设置与更新', exact: true }).click();
      await started;
      const address = page.getByLabel('工具服务地址', { exact: true });
      await expect(address).toBeEnabled();
      await expect(page.getByText('正在读取服务状态…', { exact: true })).toBeVisible();
      await address.fill('https://tools.example.invalid');
      const cancelled = page.waitForEvent('requestfailed', request => request.url().endsWith('/tools/status'));
      await page.getByRole('button', { name: '保存工具服务地址', exact: true }).click();
      await expect(page.getByText('工具服务地址已保存，立即生效。可检查连接。', { exact: true })).toBeVisible();
      await cancelled;
      resolveStatus();
      await expect(page.getByText('旧服务迟到状态', { exact: true })).toHaveCount(0);
      expect((await localApp.request('/tools/settings')).baseUrl).toBe('https://tools.example.invalid');
      await expect(page.getByRole('button', { name: '检查工具服务', exact: true })).toBeEnabled();
    } finally {
      resolveStatus();
    }
  });
});
