#!/usr/bin/env node
// Writes every store's mark under `site/public/icons/` (or the dir given as the first argument):
//   <key>.svg        favicon and badge (dark family; transparent corners)
//   <key>.light.svg  the light family, for light backgrounds
//   <key>.ico        16 + 32 + 48 px favicon
//   <key>-32.png     small raster badge
//   <key>-180.png    apple-touch icon (full-bleed square)
//   <key>-512.png    large raster (PWA manifests, chat avatars)
// and the README banners, `banner-light.svg` / `banner-dark.svg` (+ 2400 px `.png`s).
// Rasters need `rsvg-convert` and ImageMagick's `magick` on PATH.
import { execFileSync } from 'node:child_process'
import { mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { STORES, banner, mark } from './marks.mjs'

const out = resolve(process.argv[2] ?? new URL('../../site/public/icons', import.meta.url).pathname)
mkdirSync(out, { recursive: true })
const png = (svgPath, px, pngPath) => execFileSync('rsvg-convert', ['-w', `${px}`, '-h', `${px}`, '-o', pngPath, svgPath])

for (const key of Object.keys(STORES)) {
  const base = join(out, key)
  writeFileSync(`${base}.svg`, mark(key) + '\n')
  writeFileSync(`${base}.light.svg`, mark(key, { theme: 'light' }) + '\n')
  const bleed = `${base}.bleed.tmp.svg`
  writeFileSync(bleed, mark(key, { bleed: true }))
  for (const px of [32, 180, 512]) png(px === 180 ? bleed : `${base}.svg`, px, `${base}-${px}.png`)
  const icoParts = [16, 48].map(px => { const p = `${base}-${px}.tmp.png`; png(`${base}.svg`, px, p); return p })
  execFileSync('magick', [icoParts[0], `${base}-32.png`, icoParts[1], `${base}.ico`])
  for (const p of [bleed, ...icoParts]) rmSync(p)
  console.log(key)
}
// The deployments' Slack usage-bot app icons, `deploy/slack/icons/<key>.png`: the store's mark full-bleed (Slack rounds
// the corners itself) at 1024 px (Slack takes a 512–2000 px square). Uploaded by hand: the API can't set an app icon.
const bots = resolve(new URL('../../deploy/slack/icons', import.meta.url).pathname)
mkdirSync(bots, { recursive: true })
for (const key of ['gcs', 'cw', 'hf']) {
  const bleed = join(bots, `${key}.bleed.tmp.svg`)
  writeFileSync(bleed, mark(key, { bleed: true }))
  png(bleed, 1024, join(bots, `${key}.png`))
  rmSync(bleed)
  console.log(`slack ${key}`)
}
for (const theme of ['light', 'dark']) {
  const base = join(out, `banner-${theme}`)
  writeFileSync(`${base}.svg`, banner(theme === 'dark') + '\n')
  execFileSync('rsvg-convert', ['-w', '2400', '-o', `${base}.png`, `${base}.svg`])
  console.log(`banner-${theme}`)
}
