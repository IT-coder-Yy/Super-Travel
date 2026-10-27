import { test, expect } from './fixtures/local-app'
import { createExample } from '../src/types'

// Explicit front-end fixture: no writes, real POI claims or external tile traffic.
const fixture = {
  id: '8ea0c02e-a9c8-435e-a26e-371ae68573e8', title: '地图界面固定测试', revision: 1, updated_at: '2026-10-27T00:00:00Z',
  data: {
    schema: 1, selectedDay: 1, favorites: [], messages: [], draft: null,
    trip: { destination: '杭州', summary: '固定坐标只验证地图交互。', generatedAt: '2026-10-27T00:00:00Z' },
    days: [{ day: 1, title: '地图固定测试', subtitle: '非真实查询', activities: [
      { id: 'first', title: '地点 <b>固定测试</b>', time: '09:00', duration: '约 1 小时', description: '离线测试地点一。', x: 0, y: 0,
        place: { id: 'fixture:first', name: '地点 <b>固定测试</b>', address: '固定地址一', latitude: 30.257, longitude: 120.148, coordinateSystem: 'wgs84', provider: 'baidu' } },
      { id: 'second', title: '固定测试地点二', time: '11:00', duration: '约 1 小时', description: '离线测试地点二。', x: 0, y: 0,
        place: { id: 'fixture:second', name: '固定测试地点二', address: '固定地址二', latitude: 30.241, longitude: 120.129, coordinateSystem: 'wgs84', provider: 'baidu' } },
    ] }],
  },
}
const transparentTile = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7R8AAAAASUVORK5CYII=', 'base64')

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/conversations', route => route.fulfill({ json: [fixture] }))
  await page.route(`**/api/conversations/${fixture.id}`, route => route.fulfill({ json: fixture }))
  await page.route('**/api/conversations/recovery', route => route.fulfill({ json: [] }))
})

test('真实地图组件按坐标显示并支持选择、键盘、标注和缩放（固定测试数据）', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({ contentType: 'image/png', body: transparentTile }))
  await page.goto('/')
  const markers = page.locator('.real-map-marker')
  await expect(markers).toHaveCount(2)
  await expect(page.locator('.real-map-status')).toHaveCount(0)
  await expect(page.locator('.leaflet-control-attribution')).toContainText('OpenStreetMap')
  await expect(page.locator('.real-map-marker-label').first()).toHaveText('地点 <b>固定测试</b>')
  await expect(page.locator('.real-map-marker-label b')).toHaveCount(0)
  await page.getByRole('button', { name: '查看固定测试地点二', exact: true }).click()
  await expect(page.getByRole('region', { name: '选中地点' }).getByRole('heading')).toHaveText('固定测试地点二')
  const first = page.getByRole('button', { name: '查看地点 <b>固定测试</b>', exact: true })
  await first.focus()
  await page.keyboard.press('Space')
  await expect(page.getByRole('region', { name: '选中地点' }).getByRole('heading')).toHaveText('地点 <b>固定测试</b>')
  await page.getByRole('button', { name: '切换地图图层', exact: true }).click()
  await expect(page.locator('.real-map-marker-label')).toHaveCount(0)
  await page.getByRole('button', { name: '放大地图', exact: true }).click()
  await expect(markers).toHaveCount(2)
  await page.getByRole('button', { name: '重置地图视角', exact: true }).click()
  await expect(first).toBeVisible()
  const pane = page.locator('.leaflet-map-pane')
  const beforeDrag = await pane.getAttribute('style')
  await page.mouse.move(1060, 430)
  await page.mouse.down()
  await page.mouse.move(1120, 450, { steps: 5 })
  await page.mouse.up()
  await expect(pane).not.toHaveAttribute('style', beforeDrag!)
  await page.reload()
  await expect(markers).toHaveCount(2)
  await page.setViewportSize({ width: 1024, height: 960 })
  const tools = await page.locator('.map-tools').boundingBox()
  for (const label of await page.locator('.real-map-marker-label').all()) {
    const box = await label.boundingBox()
    expect(box!.x + box!.width).toBeLessThan(tools!.x)
  }
  expect(errors).toEqual([])
})

test('底图失败明确提示且保留已定位地点，允许重试（固定测试数据）', async ({ page }) => {
  let failing = true
  await page.route('https://tile.openstreetmap.org/**', route => failing
    ? route.fulfill({ status: 503, body: 'fixture tile unavailable' })
    : route.fulfill({ contentType: 'image/png', body: transparentTile }))
  await page.goto('/')
  await expect(page.locator('.real-map-status')).toContainText('底图暂时无法完整加载')
  await expect(page.locator('.real-map-marker')).toHaveCount(2)
  await expect(page.locator('.map-art')).toHaveCount(0)
  failing = false
  await page.getByRole('button', { name: '重试地图', exact: true }).click()
  await expect(page.locator('.real-map-status')).toHaveCount(0)
  await expect(page.locator('.real-map-marker')).toHaveCount(2)
})

test('从旧示例切入已保存真实行程时地图可首次带地点挂载（固定测试数据）', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const example = { ...fixture, id: 'e1b0c02e-a9c8-435e-a26e-371ae68573e8', title: '旧示例', data: createExample() }
  await page.route('**/api/conversations', route => route.fulfill({ json: [example, fixture] }))
  await page.route(`**/api/conversations/${example.id}`, route => route.fulfill({ json: example }))
  await page.route('https://tile.openstreetmap.org/**', route => route.fulfill({ contentType: 'image/png', body: transparentTile }))
  await page.goto('/')
  await expect(page.locator('.map-art')).toHaveCount(1)
  await page.getByRole('button', { name: '历史旅行', exact: true }).click()
  await page.getByRole('button', { name: /地图界面固定测试/ }).click()
  await expect(page.locator('.real-map-marker')).toHaveCount(2)
  await expect(page.locator('.map-art')).toHaveCount(0)
  expect(errors).toEqual([])
})
