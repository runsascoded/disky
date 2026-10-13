import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

// The section hash and scroll position across navigation (`src/hashSpy.ts`),
// on a bucket of the deployment under test (`HASH_BUCKET`, default gcs's
// `/marin-us-central2`; the public r2 demo: `/ctbk`). History writes are
// recorded from page start, so "the spy only ever replaceState's" is asserted
// on the calls themselves, not inferred from the history length.

type Write = { fn: 'push' | 'replace'; url: string }
declare global { interface Window { __hist: Write[] } }

const BUCKET = process.env.HASH_BUCKET ?? '/marin-us-central2'

async function recordHistory(page: Page) {
  await page.addInitScript(() => {
    window.__hist = []
    for (const fn of ['push', 'replace'] as const) {
      const orig = history[`${fn}State`].bind(history)
      history[`${fn}State`] = (state: unknown, unused: string, url?: string | URL | null) => {
        if (url != null) { const u = new URL(String(url), location.href); window.__hist.push({ fn, url: u.pathname + u.search + u.hash }) }
        return orig(state, unused, url)
      }
    }
  })
}
const writes = (page: Page) => page.evaluate(() => window.__hist.splice(0))
const at = (page: Page) => page.evaluate(() => ({ path: location.pathname + location.search + location.hash, y: Math.round(scrollY) }))
/** `#id`'s top relative to where it parks (its scroll-margin-top), px. */
const parkOffset = (page: Page, id: string) => page.evaluate(i => {
  const el = document.getElementById(i)!
  return Math.round(el.getBoundingClientRect().top - parseFloat(getComputedStyle(el).scrollMarginTop)) || 0  // not -0
}, id)
const loaded = (page: Page) => expect(page.locator('#tbl .worklist tbody tr').first()).toBeVisible()

test.beforeEach(async ({ page }) => { await recordHistory(page) })

test('a deep link parks its section below the header, once', async ({ page }) => {
  await page.goto(`${BUCKET}#tbl`)
  await loaded(page)
  await expect.poll(() => parkOffset(page, 'tbl'), { timeout: 30_000 }).toBe(0)
  // The section sits below the sticky header, with a gap.
  const gap = await page.evaluate(() => {
    const bar = document.querySelector('.topbar')!.getBoundingClientRect().bottom
    return Math.round(document.getElementById('tbl')!.getBoundingClientRect().top - bar)
  })
  expect(gap).toBe(16)
  await page.waitForTimeout(3000)
  // No spy write from the pursuit's own scrolls, nor from content landing: the
  // only history writes (the entry's position key, param canonicalization)
  // replace the URL with itself.
  expect((await at(page)).path).toBe(`${BUCKET}#tbl`)
  expect((await writes(page)).filter(w => w.fn !== 'replace' || w.url !== `${BUCKET}#tbl`)).toEqual([])
})

test('the spy names the section the reader scrolls to, with replaceState only', async ({ page }) => {
  await page.goto(BUCKET)
  await loaded(page)
  await page.waitForTimeout(2000)
  await writes(page)
  await page.mouse.move(600, 400)
  const tblY = await page.evaluate(() => document.getElementById('tbl')!.getBoundingClientRect().top + scrollY)
  // Wheel down until #tbl's top passes the spy's reference line.
  for (let i = 0; i < 40 && (await at(page)).y < tblY - 100; i++) await page.mouse.wheel(0, 200)
  await page.waitForTimeout(500)
  const w = await writes(page)
  expect(w.filter(x => x.fn === 'push')).toEqual([])
  expect(w.at(-1)).toEqual({ fn: 'replace', url: `${BUCKET}#tbl` })
  expect((await at(page)).path).toBe(`${BUCKET}#tbl`)
})

test('breadcrumb root from a scrolled `#tbl` → `/`, no hash, at the top; back restores', async ({ page }) => {
  await page.goto(`${BUCKET}#tbl`)
  await loaded(page)
  await expect.poll(() => parkOffset(page, 'tbl'), { timeout: 30_000 }).toBe(0)
  // The reader scrolls a little further on (the spy keeps #tbl), leaving a position to restore.
  await page.mouse.move(600, 400)
  await page.mouse.wheel(0, 120)
  await page.waitForTimeout(600)
  const before = await at(page)
  await page.locator('.tb-crumbs button').first().click()
  await expect.poll(() => at(page)).toEqual({ path: '/', y: 0 })
  // Content landing for the fleet view moves nothing and writes no hash.
  await page.waitForTimeout(4000)
  expect(await at(page)).toEqual({ path: '/', y: 0 })
  await page.goBack()
  await expect.poll(() => at(page), { timeout: 30_000 }).toEqual(before)
  await page.goForward()
  await expect.poll(() => at(page), { timeout: 30_000 }).toEqual({ path: '/', y: 0 })
})

test('a drill from a hashed, scrolled view clears the hash and starts at the top', async ({ page }) => {
  await page.goto(`${BUCKET}#tbl`)
  await loaded(page)
  await expect.poll(() => parkOffset(page, 'tbl'), { timeout: 30_000 }).toBe(0)
  // The biggest child (a directory): its name may be elided in the cell, so the
  // landing is checked by shape — one segment under the bucket, no hash, at the top.
  const drilled = () => page.evaluate(() => ({
    parent: location.pathname.replace(/\/[^/]+$/, ''), hash: location.hash, y: Math.round(scrollY),
  }))
  await page.locator('#tbl .worklist tbody tr td.prefix a[role=link]').first().click()
  await expect.poll(drilled).toEqual({ parent: BUCKET, hash: '', y: 0 })
  await page.waitForTimeout(3000)
  expect(await drilled()).toEqual({ parent: BUCKET, hash: '', y: 0 })
})
