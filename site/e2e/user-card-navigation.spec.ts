import { expect, test } from '@playwright/test'

const FIXTURE = '/e2e/fixtures/user-card-navigation.html'

test('clicking the legend chip itself still applies its owner filter', async ({ page }) => {
  await page.goto(FIXTURE)
  await page.locator('.user-chip').click()
  await expect(page.getByTestId('legend-picks')).toHaveText('1')
  await expect(page).toHaveURL(new URL(`${FIXTURE}?o=jane`, page.url()).href)
})
