/** End-to-end tier-B render: `treemapCardSvg` → resvg-wasm → PNG, using the
 *  bundled Inter faces and the installed wasm (the same pieces the Worker uses).
 *  Also writes `tmp/og-b-sample.png` for manual inspection. */
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { beforeAll, describe, expect, it } from 'vitest'
import { ensureWasm, svgToPng } from '../ogRender'
import { treemapCardSvg } from '../ogSvg'

const G = 1024 ** 3
// Representative r2://ctbk depth-1 children (from the live scan).
const CTBK = [
  ['gbfs', 206.1], ['.dvc', 204.4], ['avail-v3', 108.4], ['avail-v5', 83.7],
  ['avail-v6', 70.9], ['.reproc', 58.2], ['avail-v4', 39.6], ['rides-v5', 29.4],
  ['avail-v4-engine-check', 25.3], ['rides-v3', 20.9], ['smg-v1', 18.8],
  ['avail-v3-prior', 14.9], ['trips', 10.3], ['avail-v3-test', 10.0], ['avail-v2', 5.0],
  ['avail', 3.7], ['avail-v3-orig-par-7d', 1.7],
].map(([name, gb]) => ({ name: name as string, size: (gb as number) * G }))

describe('svgToPng', () => {
  let fonts: Uint8Array[]
  beforeAll(async () => {
    await ensureWasm(await readFile('node_modules/@resvg/resvg-wasm/index_bg.wasm'))
    fonts = [
      new Uint8Array(await readFile('public/_fonts/Inter-400.ttf')),
      new Uint8Array(await readFile('public/_fonts/Inter-600.ttf')),
    ]
  })

  it('renders a valid 1200x630 PNG from the card SVG', async () => {
    const svg = treemapCardSvg({ uri: 'r2://ctbk', itemCount: 336, children: CTBK })
    const png = svgToPng(svg, fonts)
    // PNG signature + IHDR width/height (bytes 16-23) = 1200 x 630.
    expect([...png.slice(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
    const view = new DataView(png.buffer, png.byteOffset)
    expect(view.getUint32(16)).toBe(1200)
    expect(view.getUint32(20)).toBe(630)
    await mkdir('tmp', { recursive: true })
    await writeFile('tmp/og-b-sample.png', png)
  })
})
