import { expect, type Locator, type Page } from '@playwright/test';
import type { TripRoute } from '../../src/types';

export async function setLegMode(page: Page, row: Locator, mode: TripRoute['mode'] | 'follow') {
  const trigger = row.getByRole('button', { name: '设置本段方式', exact: true });
  await trigger.press('Enter');
  const form = page.getByRole('dialog', { name: '本段交通方式', exact: true });
  await form.getByLabel('本段接驳方式').selectOption(mode);
  await form.getByRole('button', { name: '保存方式到草稿' }).press('Enter');
  await expect(trigger).toBeFocused();
}
