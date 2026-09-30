import { expect, test } from '@playwright/test'
import type { Locator, Page } from '@playwright/test'

// Render-count specs: an interaction causes exactly the commits and component
// renders it should — no app-wide re-render behind a table click. Reads the
// spy `src/dev/renderSpy.ts` installs when the page is loaded with `?spy=1`.

const summary = (page: Page) => page.evaluate(() => window.__renderSpy!.summary())
const reset = (page: Page) => page.evaluate(() => window.__renderSpy!.reset())
// React commits synchronously on a click; the tooltips a commit mounts
// position themselves in a follow-up commit. Wait past both before reading.
const settle = (page: Page) => page.waitForTimeout(400)
// Clicks are dispatched on the element, not aimed at coordinates: the page's
// deep-link scroller keeps nudging scroll for a while after load, so an aimed
// click can land on whatever slid under the pointer (a nav link, the map).
const click = (loc: Locator, init?: Record<string, unknown>) => loc.dispatchEvent('click', { bubbles: true, cancelable: true, ...init })
// The page keeps committing while the treemap's deeper levels, the ledger and
// the charts arrive; measure only once two whole seconds pass with no commit.
async function quiet(page: Page) {
  for (let i = 0; i < 30; i++) {
    await reset(page)
    await page.waitForTimeout(2000)
    if ((await summary(page)).length === 0) return
  }
  throw new Error('page never went quiet')
}
const shape = (page: Page) => summary(page).then(s => s.map(c => ({ components: c.components, updaters: c.updaters })))

// What one children-table selection change renders: the table (the selection
// lives there) and what it contains — nothing above it. The selection bar's
// five tooltips (three mark dots, ×, assign) follow in a second commit:
// floating-ui positions them as they mount, and schedules an update that
// renders nothing as they unmount.
const FIVE_TOOLTIPS = ['Tooltip', 'Tooltip', 'Tooltip', 'Tooltip', 'Tooltip']
const TABLE_ONLY = { components: ['AssignSelect', 'Avatar', 'ChildrenTable', 'OwnerBar', 'OwnerFactChip', 'Tooltip', 'UserChip'], updaters: ['ChildrenTable'] }
const BAR_TOOLTIPS_MOUNT = { components: ['Tooltip'], updaters: FIVE_TOOLTIPS }
const BAR_TOOLTIPS_UNMOUNT = { components: [], updaters: FIVE_TOOLTIPS }

test.describe('children table selection', () => {
  test.beforeEach(async ({ page }) => {
    // `#tbl` scopes every selector to the children table: the mark-history
    // section shares its `.children-tbl` styling class (and has rows first).
    await page.goto('/marin-us-east5/checkpoints?spy=1#tbl')
    await expect(page.locator('#tbl .worklist tbody tr').first()).toBeVisible()
    // Tell react-query the tab is hidden: that pauses its 30 s polls and the
    // refetch-on-focus a first pointer event would trigger — app-wide
    // re-renders that aren't the click's. (Only the property is faked; the
    // page still paints.)
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.mouse.move(2, 2)
    await quiet(page)
  })

  test('selecting a row is one table-only commit', async ({ page }) => {
    await reset(page)
    await click(page.locator('#tbl .worklist tbody tr').nth(2).locator('td.num').first())
    await settle(page)
    expect(await shape(page)).toEqual([TABLE_ONLY, BAR_TOOLTIPS_MOUNT])
    await expect(page.locator('#tbl tr.sel')).toHaveCount(1)
  })

  test('deselecting by a dead-space click is one table-only commit', async ({ page }) => {
    await click(page.locator('#tbl .worklist tbody tr').nth(2).locator('td.num').first())
    await settle(page)
    await reset(page)
    await click(page.locator('#tbl .pager.top'))
    await settle(page)
    expect(await shape(page)).toEqual([TABLE_ONLY, BAR_TOOLTIPS_UNMOUNT])
    await expect(page.locator('#tbl tr.sel')).toHaveCount(0)
  })

  test('Esc with a selection clears it and stays on the page', async ({ page }) => {
    await click(page.locator('#tbl .worklist tbody tr').nth(2).locator('td.num').first())
    await settle(page)
    await reset(page)
    await page.keyboard.press('Escape')
    await settle(page)
    expect(await shape(page)).toEqual([TABLE_ONLY, BAR_TOOLTIPS_UNMOUNT])
    await expect(page.locator('#tbl tr.sel')).toHaveCount(0)
    expect(new URL(page.url()).pathname).toBe('/marin-us-east5/checkpoints')
  })

  test('j moves the cursor in one table-only commit (no hotkey-state fan-out)', async ({ page }) => {
    await click(page.locator('#tbl .worklist tbody tr').nth(2).locator('td.num').first())
    await settle(page)
    await reset(page)
    await page.keyboard.press('j')
    await settle(page)
    expect(await shape(page)).toEqual([TABLE_ONLY])
    await expect(page.locator('#tbl tr.cur')).toHaveCount(1)
    await expect(page.locator('#tbl .worklist tbody tr').nth(3)).toHaveClass(/\bcur\b/)
  })

  test('Esc with nothing selected falls through to the treemap drill-up', async ({ page }) => {
    await page.keyboard.press('Escape')
    await page.waitForURL(url => new URL(url).pathname === '/marin-us-east5')
  })
})
