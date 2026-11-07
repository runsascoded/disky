import { expect, test } from '@playwright/test'
import { collectErrors } from './helpers'

test('home renders the treemap', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/')

  await expect(page.getByText('Marin GCS usage')).toBeVisible()
  // The treemap draws as SVG (or canvas) — assert it mounted with content.
  await expect(page.locator('svg, canvas').first()).toBeVisible()

  expect(errors).toEqual([])
})
