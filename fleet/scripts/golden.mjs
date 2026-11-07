#!/usr/bin/env node
// Regenerate the fleet card's goldens (`tests/golden/*.svg`, what `functions/_lib/card.test.ts` compares against) from
// the test fleet (`functions/_lib/testFleet.ts`); `--png DIR` also rasterizes each as the route would (2.5×, Inter), to
// look at. Run from `fleet/`: `node scripts/golden.mjs [--png ../tmp]`.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const pngDir = process.argv.includes('--png') ? resolve(process.argv[process.argv.indexOf('--png') + 1]) : null

const server = await createServer({ root: ROOT, configFile: false, appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false } })
try {
  const { fleetSvg, ZOOM } = await server.ssrLoadModule('/functions/_lib/card.ts')
  const fx = await server.ssrLoadModule('/functions/_lib/testFleet.ts')
  const { readFleet } = await server.ssrLoadModule('/functions/_lib/fleet.ts')
  const doc = await readFleet(fx.fleetEnv(), fx.NOW, fx.stubFetch(fx.ANSWERS))
  const deployments = fx.FLEET.map(({ name, color }) => ({ name, color }))
  const cards = {
    'card-full-dark': fleetSvg({ tier: 'full', theme: 'dark', deployments, doc, now: doc.now }),
    'card-full-light': fleetSvg({ tier: 'full', theme: 'light', deployments, doc, now: doc.now }),
    'card-anon-dark': fleetSvg({ tier: 'anon', theme: 'dark', deployments, now: fx.NOW }),
  }
  const out = join(ROOT, 'tests/golden')
  mkdirSync(out, { recursive: true })
  for (const [name, svg] of Object.entries(cards)) writeFileSync(join(out, `${name}.svg`), svg + '\n')
  console.error(`wrote ${Object.keys(cards).length} goldens to ${out}`)
  if (pngDir) {
    const { ensureWasm, svgToPng } = await server.ssrLoadModule('@disk-tree/pages-kit/og/render')
    await ensureWasm(readFileSync(join(ROOT, 'node_modules/@resvg/resvg-wasm/index_bg.wasm')))
    const fonts = ['Inter-400.ttf', 'Inter-600.ttf'].map(f => new Uint8Array(readFileSync(join(ROOT, '../site/public/_fonts', f))))
    mkdirSync(pngDir, { recursive: true })
    for (const [name, svg] of Object.entries(cards)) writeFileSync(join(pngDir, `fleet-${name}.png`), svgToPng(svg, fonts, ZOOM))
    console.error(`wrote PNGs to ${pngDir}`)
  }
} finally {
  await server.close()
}
