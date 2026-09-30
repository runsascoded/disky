import { expect, test } from '@playwright/test'
import { collectErrors, SIZE_RE } from './helpers'

test('/user/:id loads for the first user linked from /users', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/users')

  // react-router <Link> rows render <a href="/user/…"> — follow the first.
  const link = page.locator('a[href^="/user/"]').first()
  await expect(link).toBeVisible()
  const href = await link.getAttribute('href')
  expect(href).toMatch(/^\/user\//)

  await page.goto(href!)
  await expect(page).toHaveURL(/\/user\//)
  await expect(page.getByText(SIZE_RE).first()).toBeVisible()

  expect(errors).toEqual([])
})
