import { test, expect } from '@playwright/test'

test('开场三秒后开放真实工作区，遮罩期间键盘不会进入底层', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  const intro = page.getByRole('dialog', { name: 'SuperTravel 开场动画' })
  await expect(intro).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: '跳过动画' })).toBeFocused()
  await expect(page.locator('#root > div').first()).toHaveAttribute('inert', '')
  await expect(intro).toHaveCount(0, { timeout: 4000 })
  await expect(page.getByRole('button', { name: '返回地图首页' })).toBeFocused()
  await page.getByRole('button', { name: '设置与更新' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  expect(errors).toEqual([])
})

test('跳过与 Escape 都立即开放工作区', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '跳过动画' }).click()
  await expect(page.locator('.startup-intro')).toHaveCount(0)
  await page.reload()
  await expect(page.locator('.startup-intro')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.locator('.startup-intro')).toHaveCount(0)
})

test('减少动态效果或 Canvas 不可用时不会等待完整开场', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/')
  await expect(page.locator('.startup-intro')).toHaveCount(0, { timeout: 1000 })
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.addInitScript(() => { HTMLCanvasElement.prototype.getContext = () => null })
  await page.reload()
  await expect(page.locator('.startup-intro')).toHaveCount(0, { timeout: 1000 })
  await expect(page.getByRole('button', { name: '设置与更新' })).toBeVisible()
})
