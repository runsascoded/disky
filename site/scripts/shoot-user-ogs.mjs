#!/usr/bin/env node
// Screenshot every user's /user/<id>/og card → public/og-user/<id>.jpg.
// Run against the local dev stack (`./dev`, port 3253 — DEV identity, and
// sync local D1 from prod first if the ledger matters; see tmp/d1sync).
// `functions/user/[id].ts` serves these when present, falling back to the
// shared /og-users.jpg, then /og.jpg. Rerun after ownership churn; the output
// is the deployment's own (untracked: `public/.gitignore`), never the base's.
//
// Usage: node scripts/shoot-user-ogs.mjs [id ...]   (default: all meta users)
import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import puppeteer from 'puppeteer'

const BASE = process.env.OG_BASE ?? 'http://127.0.0.1:3253'
const OUT = new URL('../public/og-user/', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const scans = await fetch(`${BASE}/data/scans.json`).then(r => r.json())
const meta = await fetch(`${BASE}/data/${scans[0]}/meta.json`).then(r => r.json())
const ids = process.argv.length > 2 ? process.argv.slice(2) : (meta.users ?? []).map(u => u.u)

const browser = await puppeteer.launch()
const page = await browser.newPage()
await page.setViewport({ width: 1200, height: 630, deviceScaleFactor: 1 })
for (const id of ids) {
  await page.goto(`${BASE}/user/${id}/og`, { waitUntil: 'networkidle0' })
  // The bar renders only once tree.json + marks have loaded and folded.
  await page.waitForSelector('.og-user .ogu-bar', { timeout: 60_000 }).catch(() => null)
  await new Promise(r => setTimeout(r, 400))
  const png = `${OUT}${id}.png`
  await page.screenshot({ path: png })
  execFileSync('sips', ['-s', 'format', 'jpeg', '-s', 'formatOptions', '85', png, '--out', `${OUT}${id}.jpg`])
  execFileSync('rm', [png])
  console.log(`og-user/${id}.jpg`)
}
await browser.close()
