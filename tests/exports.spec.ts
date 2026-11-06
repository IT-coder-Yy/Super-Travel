import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect, type LocalApp } from './fixtures/local-app';

test.use({ reducedMotion: 'reduce' });
async function createTrip(page: Page, localApp: LocalApp) {
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf3ee"/></svg>' }));
  await localApp.reset();
  await localApp.request('/model/configure', 'POST', { base_url: 'https://fixture.invalid/v1', model: 'fixture-model', api_key: 'fixture-key-not-real' });
  const document = await localApp.request('/conversations', 'POST', { title: '正式行程导出固定回归', data: { schema: 1, days: [], draft: null, selectedDay: 1, messages: [], favorites: [] } });
  await page.goto('/');
  await expect(page.locator('.startup-intro')).toHaveCount(0);
  await expect(page.locator('.trip-title')).toHaveText(document.title);
  await expect(page.getByRole('button', { name: '导出行程', exact: true })).toBeDisabled();
  await page.getByLabel('旅行对话输入').fill('生成固定行程，杭州两天');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.activity-heading')).toHaveCount(2);
  await expect(page.getByRole('button', { name: '导出行程', exact: true })).toBeEnabled();
  return { id: document.id, read: () => localApp.request('/conversations/' + document.id), path: resolve(localApp.dataDir, 'conversations', document.id + '.json') };
}
async function openExport(page: Page) {
  await page.getByRole('button', { name: '导出行程', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '导出行程', exact: true })).toBeVisible();
}
async function editNote(page: Page, note: string) {
  await page.getByRole('button', { name: /^(编辑当天行程|继续编辑草稿)$/ }).click();
  await page.getByLabel('备注', { exact: true }).first().fill(note);
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
}
async function screenshots(page: Page, name: string) {
  await page.screenshot({ path: 'artifacts/b4-' + name + '-1440.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'artifacts/b4-' + name + '-390.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1440, height: 960 });
}
async function downloadedText(page: Page, localApp: LocalApp) {
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name: '保存 Markdown', exact: true }).click();
  const download = await waiting;
  expect(download.suggestedFilename()).toMatch(/\.md$/);
  const directory = resolve(localApp.dataDir, '中文导出 含空格');
  await mkdir(directory, { recursive: true });
  const path = resolve(directory, download.suggestedFilename());
  await download.saveAs(path);
  return readFile(path, 'utf8');
}

test('正式导出隔离草稿候选与聊天，下载等于固定预览，重启后可再次导出', async ({ page, localApp }) => {
  test.setTimeout(120_000);
  const trip = await createTrip(page, localApp);
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByLabel('备注', { exact: true }).first().fill('正式备注：上午到达');
  await page.getByLabel('活动费用（元）', { exact: true }).first().fill('0');
  await page.getByLabel('活动费用（元）', { exact: true }).nth(1).fill('12.50');
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  // Only isolated fictional data is extended with exclusion sentinels; no real user data.
  const seed = await trip.read();
  const source = seed.data.days[0].activities[0];
  seed.data.candidates = [{ id: 'candidate-export-fixture', destination: '杭州', title: source.title, duration: source.duration, description: '候选独有私密备注不应导出', place: source.place }];
  seed.data.messages.push({ id: 'export-secret-fixture', role: 'user', content: '聊天独有私密内容不应导出' });
  seed.data.configuration = { api_key: 'configuration-secret-not-exported' };
  await writeFile(trip.path, JSON.stringify(seed), 'utf8');
  await page.reload();
  await editNote(page, '尚未应用的草稿备注');
  let requests = 0;
  page.on('request', request => { if (request.url().endsWith('/trip/export')) requests++; });
  await openExport(page);
  await screenshots(page, 'choice');
  const response = page.waitForResponse(response => response.url().endsWith('/trip/export') && response.status() === 200);
  await page.getByRole('button', { name: '导出已保存正式版', exact: true }).click();
  const snapshot = await (await response).json();
  const preview = page.getByLabel('Markdown 预览', { exact: true });
  await expect(preview).toHaveText(snapshot.markdown, { useInnerText: false });
  expect(await preview.textContent()).toBe(snapshot.markdown);
  await expect(preview).toBeFocused();
  expect(snapshot.markdown).toContain('正式备注：上午到达');
  expect(snapshot.markdown).toContain('正式行程概览');
  expect(snapshot.markdown).toContain('12.50');
  for (const secret of ['尚未应用的草稿备注', '候选独有私密备注不应导出', '聊天独有私密内容不应导出', 'configuration-secret-not-exported', 'fixture-key-not-real']) expect(snapshot.markdown).not.toContain(secret);
  await screenshots(page, 'preview');
  // Another client applies a new formal version after preview; this export stays pinned.
  const before = await trip.read();
  await localApp.request('/conversations/' + trip.id + '/trip/apply', 'POST', { expected_revision: before.revision });
  expect(await downloadedText(page, localApp)).toBe(snapshot.markdown);
  expect(requests).toBe(1);
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('已开始下载');
  await localApp.restart();
  await page.reload();
  await openExport(page);
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await expect(preview).toContainText('尚未应用的草稿备注');
  expect(await downloadedText(page, localApp)).toBe(await preview.textContent());
});

test('先应用再导出必须等应用成功，失败留在弹窗且不读取旧正式版', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  const formal = await trip.read();
  await editNote(page, '应用后才可导出的最新备注');
  let requests = 0;
  page.on('request', request => { if (request.url().endsWith('/trip/export')) requests++; });
  await page.route('**/trip/apply', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定测试应用失败，请重试。' } }) }), { times: 1 });
  await openExport(page);
  await page.getByRole('button', { name: '先应用草稿再导出', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('应用失败');
  expect(requests).toBe(0);
  expect((await trip.read()).data.days).toEqual(formal.data.days);
  await expect(page.getByLabel('Markdown 预览', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '先应用草稿再导出', exact: true }).click();
  await expect(page.getByLabel('Markdown 预览', { exact: true })).toContainText('应用后才可导出的最新备注');
  const applied = await trip.read();
  expect(applied.data.draft).toBeNull();
  expect(applied.data.draftCandidates).toBeNull();
  expect(requests).toBe(1);
  expect(await downloadedText(page, localApp)).toBe(await page.getByLabel('Markdown 预览', { exact: true }).textContent());
});

test('版本变化需重新读取并确认，查询失败可重试，关闭后晚响应不会重开预览', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  await openExport(page);
  const current = await trip.read();
  await localApp.request('/conversations/' + trip.id, 'PUT', { title: current.title, expected_revision: current.revision, data: { ...current.data, favorites: [current.data.days[0].activities[0].id] } });
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('版本已变化');
  await expect(page.getByLabel('Markdown 预览', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '确认重新读取会话', exact: true }).click();
  await expect(page.getByRole('button', { name: '预览正式行程', exact: true })).toBeVisible();
  await expect(page.getByLabel('Markdown 预览', { exact: true })).toHaveCount(0);
  await page.route('**/trip/export', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定测试读取失败，请重试。' } }) }), { times: 1 });
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('读取失败');
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await expect(page.getByLabel('Markdown 预览', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  const latest = await trip.read();
  const snapshot = await localApp.request('/conversations/' + trip.id + '/trip/export', 'POST', { expected_revision: latest.revision });
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let entered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  await page.route('**/trip/export', async route => { entered(); await held; await route.fulfill({ json: snapshot }).catch(() => undefined); }, { times: 1 });
  await openExport(page);
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await started;
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  release();
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await trip.read()).data).toEqual(latest.data);
});

test('桌面保存桥接的取消与失败保留同一预览，可重试；不冒充系统对话框实测', async ({ page, localApp }) => {
  await createTrip(page, localApp);
  await openExport(page);
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  const preview = page.getByLabel('Markdown 预览', { exact: true });
  await expect(preview).toBeVisible();
  const markdown = await preview.textContent();
  // This substitutes IPC results only; Rust file tests verify actual writes separately.
  await page.evaluate(() => {
    const state = window as unknown as { isTauri: boolean; __TAURI_INTERNALS__: { invoke: (command: string, args: unknown) => Promise<unknown> }; b4Calls: unknown[] };
    state.isTauri = true;
    state.b4Calls = [];
    state.__TAURI_INTERNALS__ = { invoke: async (command, args) => {
      if (command !== 'save_markdown') throw new Error('Unexpected mocked native command: ' + command);
      state.b4Calls.push(args);
      if (state.b4Calls.length === 1) return { status: 'cancelled' };
      if (state.b4Calls.length === 2) throw new Error('固定测试：所选目录只读，原文件未改变。');
      return { status: 'saved', path: 'D:/隔离测试/中文 行程.md' };
    } };
  });
  await page.getByRole('button', { name: '保存 Markdown', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('已取消保存');
  expect(await preview.textContent()).toBe(markdown);
  await page.getByRole('button', { name: '保存 Markdown', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('目录只读');
  expect(await preview.textContent()).toBe(markdown);
  await page.getByRole('button', { name: '保存 Markdown', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('Markdown 已保存');
  const calls = await page.evaluate(() => (window as unknown as { b4Calls: { markdown: string }[] }).b4Calls);
  expect(calls).toHaveLength(3);
  for (const args of calls) expect(args.markdown).toBe(markdown);
  await page.evaluate(() => { (window as unknown as { isTauri: boolean }).isTauri = false; });
});

test('只有草稿或旧示例不能导出，只有候选草稿也必须明确选择版本', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  const real = await trip.read();
  const draftOnly = structuredClone(real);
  draftOnly.data.draft = draftOnly.data.days;
  draftOnly.data.draftTrip = draftOnly.data.trip;
  draftOnly.data.days = [];
  delete draftOnly.data.trip;
  await writeFile(trip.path, JSON.stringify(draftOnly), 'utf8');
  await page.reload();
  await expect(page.getByRole('button', { name: '导出行程', exact: true })).toBeDisabled();
  const example = structuredClone(real);
  delete example.data.trip;
  await writeFile(trip.path, JSON.stringify(example), 'utf8');
  await page.reload();
  await expect(page.getByRole('button', { name: '导出行程', exact: true })).toBeDisabled();
  real.data.draftCandidates = [];
  await writeFile(trip.path, JSON.stringify(real), 'utf8');
  await page.reload();
  await openExport(page);
  await expect(page.getByRole('button', { name: '导出已保存正式版', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '先应用草稿再导出', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '先应用草稿再导出', exact: true }).click();
  await expect(page.getByLabel('Markdown 预览', { exact: true })).toBeVisible();
  expect((await trip.read()).data.draftCandidates).toBeNull();
});


test('之前自动保存失败可在导出时重试，正式版导出仍不包含未应用草稿', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await expect(page.locator('.storage-indicator')).toContainText('已保存到本机');
  await page.route('**/api/conversations/' + trip.id, async route => {
    if (route.request().method() === 'PUT') await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: { message: '固定测试自动保存失败，请重试。' } }) });
    else await route.continue();
  }, { times: 1 });
  await page.getByLabel('备注', { exact: true }).first().fill('自动保存重试后的草稿备注');
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await expect(page.getByText('固定测试自动保存失败，请重试。', { exact: true })).toBeVisible();
  await openExport(page);
  await page.getByRole('button', { name: '导出已保存正式版', exact: true }).click();
  const preview = page.getByLabel('Markdown 预览', { exact: true });
  await expect(preview).toBeVisible();
  expect(await preview.textContent()).not.toContain('自动保存重试后的草稿备注');
  expect((await trip.read()).data.draft[0].activities[0].description).toBe('自动保存重试后的草稿备注');
});


test('PDF 导出正式快照，下载字节固定且隔离未应用草稿', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  await editNote(page, 'PDF_DRAFT_ONLY 未应用草稿');
  await openExport(page);
  await page.getByLabel('导出格式', { exact: true }).selectOption('pdf');
  await page.screenshot({ path: 'artifacts/b6-pdf-choice-1440.png', fullPage: true });
  const response = page.waitForResponse(r => r.url().endsWith('/trip/export') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '导出已保存正式版', exact: true }).click();
  const snapshot = await (await response).json();
  const preview = page.getByLabel('PDF 内容预览', { exact: true });
  await expect(preview).toBeVisible();
  expect(await preview.textContent()).toBe(snapshot.previewText);
  expect(snapshot.markdown).not.toContain('PDF_DRAFT_ONLY');
  expect(snapshot.filename).toMatch(/\.pdf$/);
  await page.screenshot({ path: 'artifacts/b6-pdf-preview-1440.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'artifacts/b6-pdf-preview-390.png', fullPage: true });
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name: '保存 PDF', exact: true }).click();
  const download = await waiting;
  expect(download.suggestedFilename()).toBe(snapshot.filename);
  const directory = resolve(localApp.dataDir, '中文 PDF 含空格');
  await mkdir(directory, { recursive: true });
  const path = resolve(directory, download.suggestedFilename());
  await download.saveAs(path);
  const bytes = await readFile(path);
  expect(bytes.equals(Buffer.from(snapshot.pdfBase64, 'base64'))).toBe(true);
  expect(bytes.subarray(0, 5).toString()).toBe('%PDF-');
  await writeFile('artifacts/b6-browser-saved.pdf', bytes);
  expect((await trip.read()).data.draft[0].activities[0].description).toBe('PDF_DRAFT_ONLY 未应用草稿');
});

test('PDF 生成失败不改正式行程，可切回 Markdown 并重试 PDF', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  await openExport(page);
  await page.getByLabel('导出格式', { exact: true }).selectOption('pdf');
  const before = await trip.read();
  let failed = false;
  await page.route('**/trip/export', async route => {
    if (!failed && route.request().postDataJSON().format === 'pdf') {
      failed = true;
      await route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'trip_pdf_unavailable', message: '固定测试：PDF 字体不可用，请重试或选择 Markdown。' } }) });
    } else await route.continue();
  });
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('字体不可用');
  await expect(page.getByLabel('PDF 内容预览', { exact: true })).toHaveCount(0);
  expect((await trip.read()).data).toEqual(before.data);
  await page.getByLabel('导出格式', { exact: true }).selectOption('markdown');
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await expect(page.getByLabel('Markdown 预览', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await openExport(page);
  await page.getByLabel('导出格式', { exact: true }).selectOption('pdf');
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await expect(page.getByLabel('PDF 内容预览', { exact: true })).toBeVisible();
  expect((await trip.read()).data).toEqual(before.data);
});

test('PDF 原生桥接取消和失败保留固定二进制，重试不重新生成', async ({ page, localApp }) => {
  await createTrip(page, localApp);
  await openExport(page);
  await page.getByLabel('导出格式', { exact: true }).selectOption('pdf');
  const response = page.waitForResponse(r => r.url().endsWith('/trip/export') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  const snapshot = await (await response).json();
  await expect(page.getByLabel('PDF 内容预览', { exact: true })).toBeVisible();
  await page.evaluate(() => {
    const state = window as unknown as { isTauri: boolean; __TAURI_INTERNALS__: { invoke: (command: string, args: unknown) => Promise<unknown> }; b6Calls: unknown[] };
    state.isTauri = true; state.b6Calls = [];
    state.__TAURI_INTERNALS__ = { invoke: async (command, args) => {
      if (command !== 'save_pdf') throw new Error('Unexpected mocked native command: ' + command);
      state.b6Calls.push(args);
      if (state.b6Calls.length === 1) return { status: 'cancelled' };
      if (state.b6Calls.length === 2) throw new Error('固定测试：文件被占用，原文件已保留。');
      return { status: 'saved', path: 'D:/隔离测试/中文 行程.pdf' };
    } };
  });
  await page.route('**/trip/export', () => { throw new Error('保存固定 PDF 不能再次读取会话'); });
  await page.getByRole('button', { name: '保存 PDF', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('已取消保存');
  await page.getByRole('button', { name: '保存 PDF', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('原文件已保留');
  await page.getByRole('button', { name: '保存 PDF', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('PDF 已保存');
  const calls = await page.evaluate(() => (window as unknown as { b6Calls: { bytes: number[] }[] }).b6Calls);
  expect(calls).toHaveLength(3);
  for (const args of calls) expect(Buffer.from(args.bytes).equals(Buffer.from(snapshot.pdfBase64, 'base64'))).toBe(true);
  expect(await page.getByLabel('PDF 内容预览', { exact: true }).textContent()).toBe(snapshot.previewText);
  await page.evaluate(() => { (window as unknown as { isTauri: boolean }).isTauri = false; });
});


test('罕字与组合 emoji 经 Markdown 中间内容导出 PDF，正式快照及下载保持一致', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  const note = '罕字：𠮷、𠮶、龘；表情：😀 👍🏽 👨‍👩‍👧‍👦 👩🏽‍💻 🇨🇳 1️⃣ ❤️；原样：<b>文字</b> [链接](https://example.test)';
  await page.getByRole('button', { name: '编辑当天行程', exact: true }).click();
  await page.getByLabel('备注', { exact: true }).first().fill(note);
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const before = await trip.read();
  await openExport(page);
  await page.getByLabel('导出格式', { exact: true }).selectOption('pdf');
  const response = page.waitForResponse(r => r.url().endsWith('/trip/export') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(200);
  const snapshot = await result.json();
  const preview = page.getByLabel('PDF 内容预览', { exact: true });
  await expect(preview).toContainText(note);
  expect(await preview.textContent()).toBe(snapshot.previewText);
  expect(snapshot.markdown).toContain('𠮷、𠮶、龘');
  expect(snapshot.markdown).toContain('👨‍👩‍👧‍👦 👩🏽‍💻 🇨🇳');
  await page.screenshot({ path: 'artifacts/b6-unicode-preview-1440.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'artifacts/b6-unicode-preview-390.png', fullPage: true });
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name: '保存 PDF', exact: true }).click();
  const download = await waiting;
  expect(download.suggestedFilename()).toMatch(/\.pdf$/);
  const path = resolve(localApp.dataDir, '罕字 表情.pdf');
  await download.saveAs(path);
  const bytes = await readFile(path);
  expect(bytes.equals(Buffer.from(snapshot.pdfBase64, 'base64'))).toBe(true);
  expect(bytes.subarray(0, 5).toString()).toBe('%PDF-');
  await writeFile('artifacts/b6-unicode-browser.pdf', bytes);
  expect((await trip.read()).data).toEqual(before.data);
});


test('旧概述不影响正式和草稿概览，导出始终对应已保存日程', async ({ page, localApp }) => {
  const trip = await createTrip(page, localApp);
  const seed = await trip.read();
  seed.data.trip.summary = '旧概述：每天2项；候选雷峰塔不应导出';
  seed.data.days[0].title = '雷峰塔旧标题';
  seed.data.days[0].subtitle = '每天2项的旧日程描述';
  seed.data.days[1].activities.forEach((item: any, index: number) => {
    item.place.id = `baidu:overview-day2-${index}`;
    item.title = item.place.name = `固定第二日地点${index}`;
    delete item.incomingRoute;
  });
  seed.data.draft = structuredClone(seed.data.days);
  const moved = seed.data.draft[0].activities.pop();
  moved.time = '15:00';
  delete moved.incomingRoute;
  seed.data.draft[1].activities.push(moved);
  seed.data.draftTrip = { ...seed.data.trip };
  await writeFile(trip.path, JSON.stringify(seed), 'utf8');
  await page.reload();
  await expect(page.locator('.intro-description')).toHaveText('草稿概览：2 天行程；第 1 天 1 项活动；第 2 天 3 项活动。');
  await expect(page.locator('.day-cover-copy')).toHaveText('第 1 天行程1 项活动');
  await openExport(page);
  await page.getByRole('button', { name: '导出已保存正式版', exact: true }).click();
  const preview = page.getByLabel('Markdown 预览', { exact: true });
  await expect(preview).toContainText('2 天行程；第 1 天 2 项活动；第 2 天 2 项活动。');
  await expect(preview).not.toContainText('雷峰塔');
  await page.getByRole('button', { name: '关闭窗口', exact: true }).click();
  await page.getByRole('button', { name: '继续编辑草稿', exact: true }).click();
  await page.getByRole('button', { name: '应用修改', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.locator('.intro-description')).toHaveText('正式概览：2 天行程；第 1 天 1 项活动；第 2 天 3 项活动。');
  await openExport(page);
  await page.getByRole('button', { name: '预览正式行程', exact: true }).click();
  await expect(preview).toContainText('2 天行程；第 1 天 1 项活动；第 2 天 3 项活动。');
  expect(await downloadedText(page, localApp)).toBe(await preview.textContent());
});
