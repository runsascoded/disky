import { expect, test } from '@playwright/test'
import { collectErrors, SIZE_RE } from './helpers'

test.describe('/users', () => {
  test('renders the owned-bytes table from the owner totals (the 2026-08-31 regression)', async ({ page }) => {
    const errors = collectErrors(page)
    await page.goto('/users')

    // The estate table loads: at least one user row shows an owned size.
    await expect(page.getByText(SIZE_RE).first()).toBeVisible()

    // The bug: the totals fold 1102'd, so every est. $/mo cell was stuck at
    // the '…' placeholder (UserPage renders '…' until the totals resolve). A
    // healthy page resolves them to a figure or '—' — none remain '…'.
    await expect(page.locator('td.num', { hasText: '…' })).toHaveCount(0)

    expect(errors).toEqual([])
  })
})
