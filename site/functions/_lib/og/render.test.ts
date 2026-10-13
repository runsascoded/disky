/** End to end: a card SVG → resvg-wasm → PNG, with the site's Inter faces and
 * the vendored wasm (the pieces the Function uses). Writes
 * `tmp/og-sample-{anon,full}.png` for a look. */
import { beforeAll, describe, expect, it } from 'vitest'
import { cardSvg, svgColor, type CardData } from './card'
import { ensureWasm, svgToPng } from './render'
import { ownerColors, tilesOf } from './data'
import type { TreeNode } from '../../../src/types'
import { Resvg } from '@resvg/resvg-wasm'
import { slotColor } from '../../../src/colors'

// Node's fs, loaded dynamically: the Functions typecheck has no Node types.
interface Fs { mkdir(p: string, o: { recursive: boolean }): Promise<unknown>; readFile(p: string): Promise<Uint8Array>; writeFile(p: string, d: Uint8Array): Promise<void> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>

const T = 2 ** 40
const sample = (tier: 'anon' | 'full'): CardData => ({
  tier, site: 'marin GCS', title: 'marin-us-central2/checkpoints', subtitle: 'scan 2026-10-02', total: '208 TiB · 9,104,210 objects',
  tiles: [
    { name: 'isoflop', b: 90 * T, color: '#4269d0', kids: [{ name: 'a', b: 50 * T, color: '#4269d0' }, { name: 'b', b: 40 * T, color: '#ff725c' }] },
    { name: 'sft', b: 60 * T, color: '#efb118', kids: [{ name: 'c', b: 30 * T, color: '#efb118' }, { name: 'd', b: 30 * T, color: '#6cc5b0' }] },
    { name: 'llama-32b-tootsie-2', b: 40 * T, color: '#3ca951' },
    { name: 'grug', b: 18 * T, color: '#4a4943' },
  ],
  legend: [{ label: 'Margaret', color: '#4269d0', b: 90 * T }, { label: 'Hedy', color: '#efb118', b: 60 * T }],
})

describe('svgToPng', () => {
  let fonts: Uint8Array[]
  beforeAll(async () => {
    const { readFile } = await fsp()
    await ensureWasm(await readFile('functions/_lib/og/vendor/resvg.wasm'))
    fonts = await Promise.all(['public/_fonts/Inter-400.ttf', 'public/_fonts/Inter-600.ttf'].map(async f => new Uint8Array(await readFile(f))))
  })
  it('renders 1200×630 PNGs for both tiers', async () => {
    const { mkdir, writeFile } = await fsp()
    await mkdir('tmp', { recursive: true })
    const dims: [number, number][] = []
    for (const tier of ['anon', 'full'] as const) {
      const png = svgToPng(cardSvg(sample(tier)), fonts)
      expect([...png.slice(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
      const v = new DataView(png.buffer, png.byteOffset)
      dims.push([v.getUint32(16), v.getUint32(20)])
      await writeFile(`tmp/og-sample-${tier}.png`, png)
    }
    expect(dims).toEqual([[1200, 630], [1200, 630]])
  })

  it('renders every fractional tree hue as its intended RGB, not black', () => {
    const fills = Array.from({ length: 7 }, (_, i) => slotColor(0, i, 7))
    const rects = fills.map((fill, i) => `<rect x="${i * 10}" width="10" height="10" fill="${svgColor(fill)}"/>`).join('')
    const renderer = new Resvg(`<svg xmlns="http://www.w3.org/2000/svg" width="70" height="10">${rects}</svg>`)
    try {
      const pixels = renderer.render().pixels
      expect(fills.map((_, i) => Array.from(pixels.slice((5 * 70 + i * 10 + 5) * 4, (5 * 70 + i * 10 + 5) * 4 + 4)))).toEqual([
        [25, 142, 204, 255], [27, 137, 215, 255], [28, 132, 225, 255],
        [37, 126, 228, 255], [48, 121, 229, 255], [58, 118, 231, 255], [69, 115, 232, 255],
      ])
    } finally {
      renderer.free()
    }
  })

  it('renders each selected map palette through the real SVG and WASM pipeline', async () => {
    const { mkdir, writeFile } = await fsp()
    await mkdir('tmp', { recursive: true })
    const tree: TreeNode = { n: 'demo', b: 100, o: 10, c: [
      { n: 'checkpoints', b: 70, o: 7, c: [
        { n: 'old', b: 40, o: 4, d: 20000, a: 20100, us: [['ann', 40]] },
        { n: 'new', b: 30, o: 3, d: 20100 },
      ] },
      { n: 'data', b: 30, o: 3, d: 20050, a: 20090, us: [['ann', 30]] },
    ] }
    const sizes: [number, number][] = []
    for (const mode of ['tree', 'date', 'read', 'user'] as const) {
      const tiles = tilesOf(tree, ownerColors([{ u: 'ann', b: 70 }]), mode, { from: 20080, to: 20100 })
      const data: CardData = { tier: 'anon', site: 'Color-mode regression demo', title: `Color by ${mode}`, subtitle: 'Synthetic fixture · same sizes, different color axes', total: '100 B · 10 objects', tiles }
      const png = svgToPng(cardSvg(data), fonts)
      const view = new DataView(png.buffer, png.byteOffset)
      sizes.push([view.getUint32(16), view.getUint32(20)])
      await writeFile(`tmp/og-color-${mode}.png`, png)
    }
    expect(sizes).toEqual([[1200, 630], [1200, 630], [1200, 630], [1200, 630]])
  })
})
