#!/usr/bin/env node
import { readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { parseArgs } from 'node:util'
import { createServer } from 'vite'

const { values } = parseArgs({ options: {
  name: { type: 'string', short: 'n' },
  output: { type: 'string', short: 'o' },
} })
if (!values.name || !values.output) throw new Error('Required: --name <site> --output <PNG>')
const server = await createServer({
  configFile: false,
  server: { middlewareMode: true, hmr: false, ws: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  appType: 'custom',
})
try {
  const { cardSvg } = await server.ssrLoadModule('/functions/_lib/og/card.ts')
  const { genericCard } = await server.ssrLoadModule('/functions/_lib/og/policy.ts')
  const { ensureWasm, FONT_FILES, svgToPng } = await server.ssrLoadModule('/functions/_lib/og/render.ts')
  await ensureWasm(await readFile('functions/_lib/og/vendor/resvg.wasm'))
  const fonts = await Promise.all(FONT_FILES.map(path => readFile('public' + path)))
  const card = genericCard({ ROOT_LABEL: values.name, AUTH_MODE: 'app' })
  await writeFile(resolve(values.output), svgToPng(cardSvg(card), fonts))
} finally {
  await server.close()
}
