import { expect, test } from '@playwright/test'

test.skip(process.env.CHILDREN_PREVIEW !== '1', 'set CHILDREN_PREVIEW=1 against the local dev server')
test.use({ actionTimeout: 5000 })

const selected = async (page: import('@playwright/test').Page) => page.locator('tbody tr.sel td.prefix').allTextContents()

test('checkbox inputs and adjacent cell padding toggle additively, exactly once', async ({ page }) => {
  await page.goto('/dev/children-table')
  const cells = page.locator('tbody td.col-sel')
  await cells.nth(0).locator('input').check()
  expect(await selected(page)).toEqual(['alpha'])
  await cells.nth(1).click({ position: { x: 2, y: 2 } })
  expect(await selected(page)).toEqual(['alpha', 'beta'])
  await cells.nth(1).locator('input').uncheck()
  expect(await selected(page)).toEqual(['alpha'])
  await cells.nth(2).locator('input').check()
  expect(await selected(page)).toEqual(['alpha', 'gamma.bin'])
  await cells.nth(0).click({ position: { x: 2, y: 2 } })
  expect(await selected(page)).toEqual(['gamma.bin'])
})

test('single-row staging reports progress, rejects repeat clicks, then retains success and review link', async ({ page }) => {
  await page.goto('/dev/children-table')
  const row = page.locator('tbody tr').nth(0)
  await row.getByRole('button', { name: 'trash', exact: true }).click()
  await expect(row.getByRole('status')).toHaveText('staging 1 folder…', { timeout: 1500 })
  await expect(row.getByRole('button', { name: 'trash', exact: true })).toHaveCount(0)
  await expect(row.getByRole('status')).toHaveText('✓ staged 1 folderreview stagedundo×')
  await expect(row.getByRole('link', { name: 'review staged' })).toHaveAttribute('href', '/staged')
})

test('plain, shift, meta and whole-page clicks retain their intended selection semantics', async ({ page }) => {
  await page.goto('/dev/children-table')
  const rows = page.locator('tbody tr')
  await rows.nth(0).locator('td.num').first().click()
  await rows.nth(1).locator('td.num').first().click({ modifiers: ['Meta'] })
  expect(await selected(page)).toEqual(['alpha', 'beta'])
  await rows.nth(2).locator('td.num').first().click()
  expect(await selected(page)).toEqual(['gamma.bin'])
  await rows.nth(0).locator('td.num').first().click({ modifiers: ['Shift'] })
  expect(await selected(page)).toEqual(['alpha', 'beta', 'gamma.bin'])
  await page.locator('thead th.col-sel').click({ position: { x: 2, y: 2 } })
  expect(await selected(page)).toEqual([])
  await page.getByRole('checkbox', { name: 'select / deselect this page', exact: true }).check()
  expect(await selected(page)).toEqual(['alpha', 'beta', 'gamma.bin'])
  await page.locator('thead th.col-sel').click({ position: { x: 2, y: 2 } })
  expect(await selected(page)).toEqual([])
})

test('bulk staging keeps feedback after the selection is cleared and sends the memo once', async ({ page }) => {
  const sent: unknown[] = []
  await page.route('**/api/plans/stage', async route => {
    const body = route.request().postDataJSON()
    sent.push(body)
    await route.fulfill({ json: { plan_id: 1, batch_id: 1, staged: body.prefixes, covered: [], absorbed: [], as_of: null } })
  })
  await page.goto('/dev/children-table')
  await page.locator('tbody td.col-sel input').nth(0).check()
  await page.locator('tbody td.col-sel input').nth(1).check()
  await page.getByRole('textbox', { name: 'deletion note' }).fill('fixture cleanup')
  await page.getByRole('button', { name: 'trash selected', exact: true }).click()
  await expect(page.locator('.sel-bar [role="status"]')).toHaveText('✓ staged 2 foldersreview stagedundo×', { timeout: 1500 })
  expect(await selected(page)).toEqual([])
  expect(sent).toEqual([{ prefixes: ['gs://demo/alpha/', 'gs://demo/beta/'], note: 'fixture cleanup' }])
})

test('a file stages its exact object identity, never a directory prefix', async ({ page }) => {
  const sent: unknown[] = []
  await page.route('**/api/plans/stage', async route => {
    const body = route.request().postDataJSON()
    sent.push(body)
    await route.fulfill({ json: { plan_id: 1, batch_id: 1, staged: ['gs://demo/gamma.bin'], staged_objects: ['gs://demo/gamma.bin'], covered: [], absorbed: [], as_of: null } })
  })
  await page.goto('/dev/children-table')
  const row = page.locator('tbody tr').nth(2)
  await row.getByRole('button', { name: 'trash', exact: true }).click()
  await expect(row.getByRole('status')).toHaveText('✓ staged 1 filereview stagedundo×')
  expect(sent).toEqual([{ prefixes: [], objects: ['gs://demo/gamma.bin'] }])
})

test('already-staged and errors stay visible, with dismiss or retry', async ({ page }) => {
  await page.goto('/dev/children-table?outcome=already')
  let row = page.locator('tbody tr').nth(2)
  await row.getByRole('button', { name: 'trash', exact: true }).click()
  await expect(row.getByRole('status')).toHaveText('✓ already stagedreview staged×', { timeout: 4500 })
  await row.getByRole('button', { name: 'dismiss' }).click()
  await expect(row.getByRole('button', { name: 'trash', exact: true })).toHaveCount(1)
  await page.goto('/dev/children-table?outcome=error')
  row = page.locator('tbody tr').nth(2)
  await row.getByRole('button', { name: 'trash', exact: true }).click()
  await expect(row.getByRole('status')).toHaveText('Can’t stage: Synthetic staging failureretry×')
  await row.getByRole('button', { name: 'retry' }).click()
  await expect(row.getByRole('status')).toHaveText('staging 1 file…', { timeout: 1500 })
})
