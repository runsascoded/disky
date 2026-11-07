import { expect, test } from '@playwright/test'

// Development-only fixture route. Deployment E2E suites must not expect it.
test.skip(process.env.FEDERATED_PREVIEW !== '1', 'set FEDERATED_PREVIEW=1 against the local dev server')

test('three picker instances share exact cutoff semantics but keep independent URL values', async ({ page }) => {
  await page.goto('/dev/federated-scans?before=2026-10-12T09%3A00%3A00.000Z&after=2026-10-13T09%3A00%3A00.000Z')
  await expect(page.getByRole('button', { name: /: choose cutoff$/ })).toHaveCount(3)
  await page.getByRole('button', { name: 'As of: choose cutoff', exact: true }).click()
  await page.getByLabel('Exact cutoff (UTC)', { exact: true }).fill('2026-10-12T03:00:00Z')
  await page.getByRole('button', { name: 'Apply', exact: true }).click()
  const url = new URL(page.url())
  expect(Object.fromEntries(url.searchParams)).toEqual({ before: '2026-10-12T09:00:00.000Z', after: '2026-10-13T09:00:00.000Z', at: '2026-10-12T03:00:00.000Z' })
  await expect(page.getByRole('status')).toHaveText(' · Partial coverage: no earlier scan for awair, crashes, jc-taxes')
  await expect(page.locator('.fp-preview-table').first().locator('tbody tr')).toHaveText(['awair4 Mi1', 'crashes2 Mi1', 'ctbk1 Mi1', 'jc-taxes3 Mi1'])
})

test('invalid UTC dates keep the current view and Escape returns focus', async ({ page }) => {
  await page.goto('/dev/federated-scans')
  const trigger = page.getByRole('button', { name: 'Compare from: choose cutoff', exact: true })
  await trigger.click()
  const input = page.getByLabel('Exact cutoff (UTC)', { exact: true })
  await input.fill('2026-02-30T08:00:00Z')
  await page.getByRole('button', { name: 'Apply', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText('Enter a valid UTC instant, such as 2026-10-13T12:00:00Z.')
  expect(new URL(page.url()).search).toBe('')
  await input.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(trigger).toBeFocused()
})

test('mobile chips wrap inside the viewport and show honest per-source coverage', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/dev/federated-scans?at=2026-10-12T03%3A00%3A00.000Z')
  await page.getByRole('button', { name: 'As of: choose cutoff', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByText('1/4 sources observed at this cutoff', { exact: true }).click()
  await expect(page.locator('.fp-sources li strong')).toHaveText(['awair', 'crashes', 'ctbk', 'jc-taxes'])
  await expect(page.locator('.fp-sources li > span')).toHaveText([
    'No scan before cutoff', 'No scan before cutoff', /.+capture began .+/, 'No scan before cutoff',
  ])
  const rect = await page.getByRole('dialog').boundingBox()
  expect(rect === null).toBe(false)
  expect(rect!.x >= 0 && rect!.x + rect!.width <= 390).toBe(true)
  expect(rect!.y >= 0 && rect!.y + rect!.height <= 844).toBe(true)
  await page.getByRole('button', { name: 'Latest observed', exact: true }).click()
  expect(new URL(page.url()).searchParams.get('at')).toBe('2026-10-13T09:00:00.000Z')
  await expect(page.getByRole('status')).toHaveCount(0)
})

test('time chips and previous/next preserve full UTC event instants', async ({ page }) => {
  await page.goto('/dev/federated-scans')
  await page.getByRole('button', { name: 'As of: choose cutoff', exact: true }).click()
  await page.screenshot({ path: '../tmp/federated-scans-picker.png', fullPage: true })
  await page.locator('button').filter({ has: page.locator('time[datetime="2026-10-13T08:00:00.000Z"]') }).click()
  expect(new URL(page.url()).searchParams.get('at')).toBe('2026-10-13T08:00:00.000Z')
  await page.getByRole('button', { name: 'As of: previous update', exact: true }).click()
  expect(new URL(page.url()).searchParams.get('at')).toBe('2026-10-13T06:00:00.000Z')
  await page.getByRole('button', { name: 'As of: next update', exact: true }).click()
  expect(new URL(page.url()).searchParams.get('at')).toBe('2026-10-13T08:00:00.000Z')
  await page.goto('/dev/federated-scans?at=2026-02-30T08%3A00%3A00Z')
  await expect(page.getByRole('alert')).toHaveText('Invalid cutoff URL. Use valid UTC instants for at, before and after.')
  await expect(page.getByRole('button', { name: /: choose cutoff$/ })).toHaveCount(0)
})
