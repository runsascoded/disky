/**
 * Capture the live treemap of each demo bucket as a 1200x630 Open Graph card
 * (spec: dynamic OGIs, tier A). Run in `rescan-demo.yml` after the re-scan;
 * the JPEGs are uploaded to R2 `og/r2/<bucket>.jpg`, which the `/og/<key>`
 * Function serves in preference to the bundled seed cards.
 *
 *   OG_HOST     deployment to capture         (default https://r2.rbw.sh)
 *   OG_BUCKETS  comma-separated bucket names  (default ctbk,nj-crashes,jc-taxes)
 *   OG_OUT      output directory              (default og-out)
 *
 * Node ≥18, `puppeteer` installed. Headless Chromium renders the real widget
 * (fonts, colors, dust) — the pixel-perfect tier the edge renderer approximates.
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import puppeteer from 'puppeteer'

const HOST = process.env.OG_HOST || 'https://r2.rbw.sh'
const BUCKETS = (process.env.OG_BUCKETS || 'ctbk,nj-crashes,jc-taxes').split(',').filter(Boolean)
const OUT = process.env.OG_OUT || 'og-out'
const WIDTH = 1200
const HEIGHT = 630
const CELL = '.dt-treemap-map .dt-treemap-cell'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

mkdirSync(OUT, { recursive: true })
const browser = await puppeteer.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] })
let failed = 0
try {
  for (const bucket of BUCKETS) {
    const page = await browser.newPage()
    try {
      await page.setViewport({ width: WIDTH, height: 820, deviceScaleFactor: 1 })
      const url = `${HOST}/r2/${bucket}?view=treemap`
      await page.goto(url, { waitUntil: 'networkidle0', timeout: 60_000 })
      await page.waitForSelector(CELL, { timeout: 30_000 })
      await sleep(1000) // let cell labels settle
      const path = join(OUT, `${bucket}.jpg`)
      await page.screenshot({ path, type: 'jpeg', quality: 88, clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT } })
      console.error(`captured ${bucket} -> ${path}`)
    } catch (e) {
      failed++
      console.error(`FAILED ${bucket}: ${e.message}`)
    } finally {
      await page.close()
    }
  }
} finally {
  await browser.close()
}
process.exit(failed && failed === BUCKETS.length ? 1 : 0)
