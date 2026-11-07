import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { chromium, type Browser, type BrowserContext } from '@playwright/test'
import type { PerfEntry } from '../src/perf.ts'
import { aggregate, renderAggs, type BenchFile, type RunResult } from './lib.ts'

// Render bench (specs/render-bench.md §2.3): load each URL N times, wait for
// every widget's `settled` mark, read `window.__perf.entries()`, print
// per-widget p50/p95 tables, write the raw loads as JSON for `diff.ts`.
//
//   pnpm bench -- <base-url> <path…> [--runs N] [--cold] [--token T] [--out FILE] [--timeout S] [--headed]
//
// warm (default): one browser context for the whole bench — the browser's
// HTTP cache and the edge's caches are both warm after the first load of a
// path. cold: a fresh context per load, and `Cache-Control: no-cache` on
// every /api and /data request. A gated deploy takes a bearer token
// (`--token`, or $BENCH_TOKEN / $SITE_TOKEN / $GCS_USAGE_TOKEN), sent as
// `Authorization: Bearer` on every request like `playwright.config.ts` does.
// Runs on Node ≥ 23.6 directly (type stripping; `.ts` imports).

interface Opts { base: string; paths: string[]; runs: number; cold: boolean; token?: string; out?: string; note?: string; timeoutS: number; headed: boolean }

function usage(msg?: string): never {
  if (msg) console.error(`bench: ${msg}`)
  console.error('usage: pnpm bench -- <base-url> <path…> [--runs N] [--cold] [--token T] [--out FILE] [--note TEXT] [--timeout S] [--headed]')
  process.exit(2)
}

export function parseArgs(argv: string[], env: Record<string, string | undefined> = {}): Opts {
  const pos: string[] = []
  let runs = 1, cold = false, token = env.BENCH_TOKEN ?? env.SITE_TOKEN ?? env.GCS_USAGE_TOKEN, out: string | undefined, note: string | undefined, timeoutS = 60, headed = false
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    const val = () => { const v = argv[++i]; if (v === undefined) usage(`${a} needs a value`); return v }
    if (a === '--') continue  // pnpm forwards it literally
    if (a === '--runs') runs = Number(val())
    else if (a === '--cold') cold = true
    else if (a === '--token') token = val()
    else if (a === '--out') out = val()
    else if (a === '--note') note = val()
    else if (a === '--timeout') timeoutS = Number(val())
    else if (a === '--headed') headed = true
    else if (a.startsWith('--')) usage(`unknown option ${a}`)
    else pos.push(a)
  }
  if (pos.length < 2) usage('need a base URL and at least one path')
  if (!Number.isInteger(runs) || runs < 1) usage('--runs must be a positive integer')
  if (!Number.isFinite(timeoutS) || timeoutS <= 0) usage('--timeout must be positive seconds')
  const base = pos[0].replace(/\/+$/, '')
  if (!/^https?:\/\//.test(base)) usage(`base URL must be http(s): ${pos[0]}`)
  const paths = pos.slice(1).map(p => (p.startsWith('/') ? p : `/${p}`))
  return { base, paths, runs, cold, token, out, note, timeoutS, headed }
}

/** Marks stable: every load `done` or `failed`, and no load changed for 1 s
 * (a widget may start its fetch only after another's answer lands). Times
 * out with what it has, naming the loads still open. */
async function waitSettled(page: import('@playwright/test').Page, timeoutMs: number): Promise<{ entries: PerfEntry[]; timedOut: boolean }> {
  const t0 = Date.now()
  let stableSince = t0, lastSig = ''
  let entries: PerfEntry[] = []
  while (Date.now() - t0 < timeoutMs) {
    entries = await page.evaluate(() => window.__perf?.entries() ?? [])
    const sig = entries.map(e => `${e.id}:${e.done ? 1 : 0}${e.failed ? 'x' : ''}`).join(',')
    if (sig !== lastSig) { lastSig = sig; stableSince = Date.now() }
    const allDone = entries.length > 0 && entries.every(e => e.done || e.failed)
    if (allDone && Date.now() - stableSince >= 1000) return { entries, timedOut: false }
    await page.waitForTimeout(200)
  }
  return { entries, timedOut: true }
}

const stamp = () => new Date().toISOString().replace(/:\d\d\.\d+Z$/, '').replace(/[-:]/g, '').replace('T', '-')

async function main() {
  const opts = parseArgs(process.argv.slice(2), process.env)
  const viewport = { width: 1280, height: 800 }
  const extraHTTPHeaders: Record<string, string> = opts.token ? { Authorization: `Bearer ${opts.token}` } : {}
  const browser: Browser = await chromium.launch({ headless: !opts.headed })
  const newContext = async (): Promise<BrowserContext> => {
    const ctx = await browser.newContext({ viewport, extraHTTPHeaders })
    if (opts.cold) await ctx.route(/\/(api|data)\//, route => route.continue({ headers: { ...route.request().headers(), 'cache-control': 'no-cache' } }))
    return ctx
  }
  console.log(`bench ${opts.base} · ${opts.paths.join(' ')} · ${opts.runs} run${opts.runs > 1 ? 's' : ''} · ${opts.cold ? 'cold' : 'warm'} · ${viewport.width}×${viewport.height}`)
  const results: RunResult[] = []
  let warm: BrowserContext | null = null
  try {
    for (let run = 1; run <= opts.runs; run++) {
      for (const path of opts.paths) {
        const ctx = opts.cold ? await newContext() : (warm ??= await newContext())
        const page = await ctx.newPage()
        const t0 = Date.now()
        await page.goto(opts.base + path, { waitUntil: 'domcontentloaded' })
        // A page without the marks (a deploy that predates them) would sit
        // out the whole timeout: say so instead. `waitForFunction` polls until
        // the bundle has installed `window.__perf`.
        await page.waitForFunction(() => !!window.__perf, undefined, { timeout: 10_000 }).catch(() => {
          throw new Error(`${opts.base + path}: no window.__perf after 10 s — this deploy predates the render marks (bench a build that has src/perf.ts, e.g. a local \`vite preview\` proxied to it)`)
        })
        const { entries, timedOut } = await waitSettled(page, opts.timeoutS * 1000)
        const ms = Date.now() - t0
        const open = entries.filter(e => !e.done && !e.failed).map(e => `${e.widget}:${e.key}`)
        results.push({ path, run, ms, timedOut, open, entries })
        const failed = entries.filter(e => e.failed).length
        console.log(`  run ${run} ${path}  ${(ms / 1000).toFixed(1)} s  ${entries.length} loads${failed ? `, ${failed} failed` : ''}${timedOut ? `  TIMED OUT, open: ${open.join(' ')}` : ''}`)
        await page.close()
        if (opts.cold) await ctx.close()
      }
    }
  } finally {
    await browser.close()
  }
  const file: BenchFile = { stamp: stamp(), base: opts.base, paths: opts.paths, runs: opts.runs, cold: opts.cold, viewport, ...(opts.note ? { note: opts.note } : {}), results }
  const out = opts.out ?? `tmp/bench/${file.stamp}.json`
  mkdirSync(dirname(out), { recursive: true })
  writeFileSync(out, JSON.stringify(file, null, 2) + '\n')
  console.log()
  console.log(renderAggs(aggregate(results)))
  console.log()
  console.log(`wrote ${out}`)
}

main().catch(e => { console.error(e); process.exit(1) })
