#!/usr/bin/env node
// Render each deployment's status card (`functions/_lib/deployCard.ts`, open question #87) from saved reads, without
// Slack, credentials or a server: PNGs per deployment and theme, the variants, and a review sheet.
// From fleet/: node scripts/deploy-cards.mjs --data ../tmp/deploy-cards/data --out ../tmp/deploy-cards
// `--data` holds, per deployment, `<dep>-runs.json` (`/api/scan-runs` reduced in the reading page to the card's input:
// each run's `familySeconds`, family names by index into `families`) and `<dep>-series.json` (`/api/series`' points).
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'
import { createServer } from 'vite'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const { values } = parseArgs({ options: { data: { type: 'string' }, out: { type: 'string' } } })
if (!values.data || !values.out) throw new Error('usage: deploy-cards.mjs --data DIR --out DIR')
const dataDir = resolve(values.data)
const out = resolve(values.out)

// The deployments' colours, as the fleet's `FLEET_JSON` names them (`wrangler.example.toml`).
const COLORS = { gcs: '#EA4335', cw: '#4C5FF0', hf: '#FFD21E', r2: '#F38020' }
const STATE = { f: 'failed', c: 'cancelled', r: 'running' }

const server = await createServer({ root: ROOT, configFile: false, appType: 'custom', logLevel: 'error', optimizeDeps: { noDiscovery: true, entries: [] }, server: { middlewareMode: true, hmr: false } })
try {
  const { deployCardSvg, DEPLOY_ZOOM } = await server.ssrLoadModule('/functions/_lib/deployCard.ts')
  const { scanTime } = await server.ssrLoadModule('/functions/_lib/fleet.ts')
  const { ensureWasm, svgToPng } = await server.ssrLoadModule('@disk-tree/pages-kit/og/render')
  await ensureWasm(readFileSync(join(ROOT, 'node_modules/@resvg/resvg-wasm/index_bg.wasm')))
  const fonts = ['Inter-400.ttf', 'Inter-600.ttf'].map(f => new Uint8Array(readFileSync(join(ROOT, '../site/public/_fonts', f))))

  const deps = readdirSync(dataDir).filter(f => f.endsWith('-runs.json')).map(f => f.replace(/-runs\.json$/, ''))
    .sort((a, b) => Object.keys(COLORS).indexOf(a) - Object.keys(COLORS).indexOf(b))
  const inputs = deps.map(name => {
    const r = JSON.parse(readFileSync(join(dataDir, `${name}-runs.json`), 'utf8'))
    const s = JSON.parse(readFileSync(join(dataDir, `${name}-series.json`), 'utf8'))
    const fam = i => (i < 0 ? '' : r.families[i])
    const runs = r.runs.map(([scan, status, trigger, started, , , secs, cost_usd, open_issues, , families, , bad]) => ({
      scan, status, trigger, started, secs, cost_usd, open_issues,
      ...(families ? { families: families.map(([i, v]) => [fam(i), v]), bad: bad.map(([i, st]) => [fam(i), STATE[st]]) } : {}),
    }))
    const points = s.points.flatMap(([date, bytes, objects]) => { const t = scanTime(date); return t === null ? [] : [{ t, bytes, objects }] })
    return { name, color: COLORS[name] ?? '#888888', now: r.read_at, runs, points }
  })

  const VARIANTS = {
    '': { mode: 'family', window: { runs: 30 } },
    outcome: { mode: 'outcome', window: { runs: 30 } },
    '14d': { mode: 'family', window: { days: 14 } },
  }
  mkdirSync(join(out, 'variants'), { recursive: true })
  const written = []
  for (const input of inputs) {
    for (const [v, opts] of Object.entries(VARIANTS)) {
      for (const theme of ['light', 'dark']) {
        const svg = deployCardSvg({ ...input, theme, ...opts })
        const file = v ? `variants/${input.name}-${v}-${theme}.png` : `${input.name}-${theme}.png`
        writeFileSync(join(out, file), svgToPng(svg, fonts, DEPLOY_ZOOM))
        if (!v) writeFileSync(join(out, `${input.name}-${theme}.svg`), svg + '\n')
        written.push(file)
      }
    }
  }

  const img = (f, w) => `<figure><img src="${f}" width="${w}" alt="${f}"><figcaption>${f}</figcaption></figure>`
  const row = (title, files, w) => `<h2>${title}</h2><div class="row">${files.map(f => img(f, w)).join('')}</div>`
  const names = inputs.map(i => i.name)
  const html = `<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Deployment cards</title>
<style>
:root { color-scheme: light dark; --bg: #fff; --ink: #1a1a19; --ink3: #6b6a62 }
@media (prefers-color-scheme: dark) { :root { --bg: #111110; --ink: #f4f4f5; --ink3: #9a988d } }
body { background: var(--bg); color: var(--ink); font: 14px/1.4 system-ui, sans-serif; margin: 16px }
h1 { font-size: 18px } h2 { font-size: 15px; margin: 28px 0 8px }
p { color: var(--ink3); max-width: 760px }
.row { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start }
figure { margin: 0 } figcaption { font-size: 11px; color: var(--ink3) }
img { display: block; height: auto; max-width: 100% }
</style></head><body>
<h1>Deployment status cards (#87), from live reads at ${new Date(Math.max(...inputs.map(i => i.now)) * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC</h1>
<p>Recommended: bars stacked by step family over the last 30 runs, the 4-sparkline strip under them. Each card is drawn at 400 px and rasterized at 3×; Slack shows an image block about 360 px wide on a phone.</p>
${row('At phone width (360 px), light', names.map(n => `${n}-light.png`), 360)}
${row('At phone width (360 px), dark', names.map(n => `${n}-dark.png`), 360)}
${row('At desktop width (560 px), light and dark', names.flatMap(n => [`${n}-light.png`, `${n}-dark.png`]), 560)}
${row('Variant: outcome colour only (no step split), last 30 runs, at 360 px', names.flatMap(n => [`variants/${n}-outcome-light.png`, `variants/${n}-outcome-dark.png`]), 360)}
${row('Variant: stacked by step, every run in the last 14 days, at 360 px', names.flatMap(n => [`variants/${n}-14d-light.png`, `variants/${n}-14d-dark.png`]), 360)}
</body></html>
`
  writeFileSync(join(out, 'sheet.html'), html)
  console.error(`wrote ${written.length} PNGs and sheet.html to ${out}; nothing posted`)
} finally {
  await server.close()
}
