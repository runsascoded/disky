/** The digest image: its chart and map texts, and the PNG it rasterizes to
 * (written to `tmp/digest-sample.png` for a look). */
import { beforeAll, describe, expect, it } from 'vitest'
import { digestSvg, niceTicks, type DigestCardInput } from './digestCard'
import { ensureWasm, svgToPng } from './render'
import { diffRoot } from '../../../src/diffStyle'
import type { DiffData } from '../../../src/diffModel'

interface Fs { mkdir(p: string, o: { recursive: boolean }): Promise<unknown>; readFile(p: string): Promise<Uint8Array>; writeFile(p: string, d: Uint8Array): Promise<void> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>

const T = 2 ** 40
const day = (d: number, h = 0) => Date.UTC(2026, 9, d, h)

const data: DiffData = {
  prev: '2026-09-30T1200', curr: '2026-10-14T1200',
  total_a: 800 * T, total_b: 830 * T, objects_a: 10, objects_b: 12, expansions: 0,
  truncated: false, threshold: 1, lookups: 0, lookups_capped: false,
  rows: [
    { p: 'checkpoints', d: 1, k: 'dir', s: 'changed', a: 500 * T, b: 560 * T, oa: 5, ob: 7 },
    { p: 'checkpoints/run-a', d: 2, k: 'dir', s: 'added', a: 0, b: 60 * T, oa: 0, ob: 2 },
    { p: 'scratch', d: 1, k: 'dir', s: 'changed', a: 300 * T, b: 270 * T, oa: 5, ob: 5 },
  ],
}

const input = (diff: boolean): DigestCardInput => ({
  title: 'Usage — October 2026', note: 'example.org · bucket · TiB',
  points: [{ t: day(1), bytes: 800 * T }, { t: day(7), bytes: 850 * T }, { t: day(14), bytes: 830 * T }],
  from: day(1) - 16 * 3600_000, to: day(31, 16),
  quota: { bytes: 1000 * T, name: '1 PiB' },
  diff: diff ? { root: diffRoot(data, 'delta', true, false, 'bucket')!.root, title: 'What changed in bucket — 9/30 → 10/14' } : null,
})

const texts = (svg: string) => [...svg.matchAll(/<text [^>]*>([^<]*)<\/text>/g)].map(m => m[1])

describe('niceTicks', () => {
  it('steps by 1, 2, 2.5 or 5 × 10^k', () => {
    expect(niceTicks(720, 920)).toEqual([750, 800, 850, 900])
    expect(niceTicks(0, 1)).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1])
    expect(niceTicks(3, 17)).toEqual([5, 7.5, 10, 12.5, 15])
  })
})

describe('digestSvg', () => {
  it('draws the chart: y ticks, Monday x ticks, the quota and the latest call-out', () => {
    expect(texts(digestSvg(input(false)))).toEqual([
      'Usage — October 2026', 'example.org · bucket · TiB',
      '800', '850', '900', '950', '1,000',
      '10/5', '10/12', '10/19', '10/26',
      '1 PiB quota', '830 TiB · 83%',
    ])
  })
  it('adds the diff map: its title, the movement, and the cells labelled with signed changes', () => {
    expect(texts(digestSvg(input(true))).slice(13)).toEqual([
      'What changed in bucket — 9/30 → 10/14', 'grew +60.0 TiB · shrank −30.0 TiB · area = |Δ|',
      'checkpoints/run-a', '+60.0 TiB',
      'scratch', '−30.0 TiB',
    ])
  })
})

describe('digest PNG', () => {
  beforeAll(async () => {
    const { readFile } = await fsp()
    await ensureWasm(await readFile('functions/_lib/og/vendor/resvg.wasm'))
  })
  it('rasterizes at 1.5×', async () => {
    const { mkdir, readFile, writeFile } = await fsp()
    const fonts = await Promise.all(['public/_fonts/Inter-400.ttf', 'public/_fonts/Inter-600.ttf'].map(async f => new Uint8Array(await readFile(f))))
    const png = svgToPng(digestSvg(input(true)), fonts, 1.5)
    const v = new DataView(png.buffer, png.byteOffset)
    expect([v.getUint32(16), v.getUint32(20)]).toEqual([1350, 1251])
    await mkdir('tmp', { recursive: true })
    await writeFile('tmp/digest-sample.png', png)
  })
})
