import { expect, test } from '@playwright/test'

// The children table's empty state (the mark axis can leave a drilled path
// with no listed children) renders after the same hooks as a populated page:
// an in-app scope change that empties the list used to throw React's
// "Rendered fewer hooks than expected" and blank the page (2026-09-11). The
// band is marked sweep, so nothing under it is undecided: marks → unmarked
// lists nothing.
test('emptying the children table via the mark axis renders its note, not a crash', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  await page.goto('/marin-us-east5/grug?o=kaiyue#tbl')
  await expect(page.locator('#tbl .worklist tbody tr').first()).toBeVisible()
  // The marks popover's own change is a router navigation; drive it the same
  // way (a history push the router listens to), keeping the page mounted.
  await page.evaluate(() => {
    history.pushState(null, '', '/marin-us-east5/grug?o=kaiyue&k=u')
    window.dispatchEvent(new PopStateEvent('popstate'))
  })
  await expect(page.locator('#tbl .tab-note')).toHaveText('No prefix under this view is unmarked.')
  expect(errors).toEqual([])
})
