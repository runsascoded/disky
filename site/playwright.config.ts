import { defineConfig, devices } from '@playwright/test'

// E2E against a deployed site (prod by default; point BASE_URL at a preview).
// The Cloudflare Access gate accepts `Authorization: Bearer` (functions/_lib/
// auth.ts), so a `gcs`-scoped grant token in $GCS_USAGE_TOKEN — set on every
// request via extraHTTPHeaders — authorizes both page navigations and the
// in-app /api/* + /data/* fetches. Without a token the specs run unauthenticated
// (they'll hit the sign-in wall — supply the token to actually exercise them).
//
// Browsers: Node ≥23 hangs on `playwright install` (see global CLAUDE.md) — use
// `pw-install` / `pwi`, not the bare installer.
const BASE_URL = process.env.BASE_URL ?? 'https://gcs.oa.dev'
const token = process.env.GCS_USAGE_TOKEN

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: true,
  // Two at a time: every page load pulls a subtree through the local wrangler
  // → prod path, and five parallel loads starve it (rows never appear inside
  // the 30 s expect window; late data commits leak into the render specs).
  workers: 2,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: BASE_URL,
    extraHTTPHeaders: token ? { Authorization: `Bearer ${token}` } : {},
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
