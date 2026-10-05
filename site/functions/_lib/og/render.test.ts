/** End to end: a card SVG → resvg-wasm → PNG, with the site's Inter faces and
 * the vendored wasm (the pieces the Function uses). Writes
 * `tmp/og-sample-{anon,full}.png` for a look. */
import { beforeAll, describe, expect, it } from 'vitest'
import { cardSvg, type CardData } from './card'
import { ensureWasm, svgToPng } from './render'

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
})
