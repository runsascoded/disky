import { expect, test } from '@playwright/test'

const FIXTURE = '/e2e/fixtures/user-card-navigation.html'

test('portal profile navigation does not also filter the owner or add a second history entry', async ({ page }) => {
  await page.goto(FIXTURE)
  await page.locator('.user-chip').focus()
  await page.getByRole('link', { name: 'Storage Breakdown →' }).click()
  await expect(page.getByTestId('legend-picks')).toHaveText('0')
  await expect(page).toHaveURL(new URL('/user/jane-doe', page.url()).href)
  await page.goBack()
  await expect(page).toHaveURL(new URL(FIXTURE, page.url()).href)
})

test('clicking the legend chip itself still applies its owner filter', async ({ page }) => {
  await page.goto(FIXTURE)
  await page.locator('.user-chip').click()
  await expect(page.getByTestId('legend-picks')).toHaveText('1')
  await expect(page).toHaveURL(new URL(`${FIXTURE}?o=jane`, page.url()).href)
})
