import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 45_000,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:1420',
    channel: process.env.CI ? undefined : 'msedge',
    viewport: { width: 1440, height: 960 },
    trace: 'retain-on-failure',
  },
});
