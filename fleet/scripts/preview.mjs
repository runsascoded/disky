#!/usr/bin/env node
// Build a reviewable fleet post and card without Slack, credentials, a server or tracked-file mutations.
// From fleet/: node scripts/preview.mjs --fixture --out ../tmp/fleet-preview
// Or supply /api/fleet's JSON: node scripts/preview.mjs --doc ../tmp/fleet.json --out ../tmp/fleet-preview
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'
import { createServer } from 'vite'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const { values } = parseArgs({ options: {
  doc: { type: 'string' },
  fixture: { type: 'boolean', default: false },
  origin: { type: 'string', default: 'https://disky.oa.dev' },
  out: { type: 'string' },
} })
if (!values.out || Boolean(values.doc) === values.fixture) {
  throw new Error('usage: preview.mjs (--fixture | --doc FILE) --out DIR [--origin URL]')
}
const origin = new URL(values.origin).origin
const out = resolve(values.out)
const server = await createServer({ root: ROOT, configFile: false, appType: 'custom', logLevel: 'error', optimizeDeps: { noDiscovery: true, entries: [] }, server: { middlewareMode: true, hmr: false } })
try {
  const { fleetSvg, ZOOM } = await server.ssrLoadModule('/functions/_lib/card.ts')
  const { fleetMessage } = await server.ssrLoadModule('/functions/_lib/slack.ts')
  const { ensureWasm, svgToPng } = await server.ssrLoadModule('@disk-tree/pages-kit/og/render')
  let doc
  if (values.fixture) {
    const fx = await server.ssrLoadModule('/functions/_lib/testFleet.ts')
    const { readFleet } = await server.ssrLoadModule('/functions/_lib/fleet.ts')
    doc = await readFleet(fx.fleetEnv(), fx.NOW, fx.stubFetch(fx.ANSWERS))
  } else {
    doc = JSON.parse(readFileSync(resolve(values.doc), 'utf8'))
  }
  if (!Number.isFinite(doc.now) || !Array.isArray(doc.deployments)) throw new Error('expected a fleet document with now and deployments')
  // This deliberately invalid signature is a visible placeholder. Never mint a private card credential offline.
  const image = `${origin}/og/fleet.png?v=${doc.now}&sig=PREVIEW_ONLY`
  const msg = fleetMessage(doc, image, `${origin}/fleet`)
  mkdirSync(out, { recursive: true })
  writeFileSync(join(out, 'slack-message.json'), JSON.stringify(msg, null, 2) + '\n')
  writeFileSync(join(out, 'slack-message.txt'), msg.blocks[0].text.text + '\n\n' + msg.blocks[2].elements[0].text + '\n')
  writeFileSync(join(out, 'fleet.json'), JSON.stringify(doc, null, 2) + '\n')
  writeFileSync(join(out, 'preview.json'), JSON.stringify({ source: values.fixture ? 'fixture' : resolve(values.doc), now: doc.now, image_url: 'invalid preview placeholder; use the deployed ?dry=1 response for a signed URL', posted: false }, null, 2) + '\n')
  await ensureWasm(readFileSync(join(ROOT, 'node_modules/@resvg/resvg-wasm/index_bg.wasm')))
  const fonts = ['Inter-400.ttf', 'Inter-600.ttf'].map(f => new Uint8Array(readFileSync(join(ROOT, '../site/public/_fonts', f))))
  const deployments = doc.deployments.map(({ name, color }) => ({ name, color }))
  for (const theme of ['dark', 'light']) {
    const svg = fleetSvg({ tier: 'full', theme, deployments, doc, now: doc.now })
    writeFileSync(join(out, `card-${theme}.svg`), svg + '\n')
    writeFileSync(join(out, `card-${theme}.png`), svgToPng(svg, fonts, ZOOM))
  }
  console.error(`wrote ${values.fixture ? 'fixture' : 'supplied-document'} Slack/card preview to ${out}; nothing posted`)
} finally {
  await server.close()
}
