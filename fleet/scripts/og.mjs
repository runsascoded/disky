#!/usr/bin/env node
// Draw the docs site's own card (`public/og.jpg`, 1200×630) through the cards' renderer, as a PNG at the given path;
// `sips -s format jpeg -s formatOptions 85 <png> --out public/og.jpg` makes the JPEG. Run from `fleet/`:
// `node scripts/og.mjs ../tmp/og.png`.
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const out = resolve(process.argv[2] ?? 'og.png')

// A squarified-looking block of tiles, the treemap disky draws, in a neutral palette.
const TILES = [
  [0, 0, 420, 330, '#3987e5'], [420, 0, 260, 200, '#e3b341'], [420, 200, 260, 130, '#2dd4bf'],
  [0, 330, 260, 150, '#a78bfa'], [260, 330, 200, 150, '#f472b6'], [460, 330, 220, 90, '#4ade80'], [460, 420, 220, 60, '#fb923c'],
]
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" font-family="Inter">`
  + `<rect width="1200" height="630" fill="#1a1a19"/>`
  + `<g transform="translate(470,75)">${TILES.map(([x, y, w, h, c]) => `<rect x="${x + 2}" y="${y + 2}" width="${w - 4}" height="${h - 4}" rx="6" fill="${c}" fill-opacity="0.85"/>`).join('')}</g>`
  + `<text x="70" y="260" font-size="96" font-weight="600" fill="#f4f4f5">disky</text>`
  + `<text x="72" y="320" font-size="30" fill="#c3c2b7">storage usage across</text>`
  + `<text x="72" y="360" font-size="30" fill="#c3c2b7">object stores</text>`
  + `<text x="72" y="430" font-size="22" fill="#9a988d">deployments · docs · fleet</text>`
  + `</svg>`

const server = await createServer({ root: ROOT, configFile: false, appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false } })
try {
  const { ensureWasm, svgToPng } = await server.ssrLoadModule('@disk-tree/pages-kit/og/render')
  await ensureWasm(readFileSync(join(ROOT, 'node_modules/@resvg/resvg-wasm/index_bg.wasm')))
  const fonts = ['Inter-400.ttf', 'Inter-600.ttf'].map(f => new Uint8Array(readFileSync(join(ROOT, '../site/public/_fonts', f))))
  writeFileSync(out, svgToPng(svg, fonts))
  console.error(`wrote ${out}`)
} finally {
  await server.close()
}
