/** The digest image: its chart and map texts, and the PNG it rasterizes to
 * (written to `tmp/digest-sample.png` for a look). */
import { beforeAll, describe, expect, it } from 'vitest'
import { digestSvg, fmtIn, niceTicks, TIER_COLORS, tiersSvg, yRange, type DigestCardInput, type TiersCardInput } from './digestCard'
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
    expect(Object.is(niceTicks(-1, 10)[0], 0)).toBe(true)  // never -0 (labelled `-0`)
  })
})

describe('yRange', () => {
  it('spans at least 1% of the top value, centred on the data (a +520 MiB change on 262 TiB is no cliff)', () => {
    // 1% of 261.9 = 2.619 around the data's middle 261.87 → [260.5605, 263.1795], then the 8% / 6% margins.
    expect(yRange([261.84, 261.9], null).map(v => +v.toFixed(5))).toEqual([260.35098, 263.33664])
  })
  it('a range wider than 1% is the data plus margins, unchanged', () => {
    expect(yRange([10, 20], null)).toEqual([10 - 0.8, 20 + 0.6])
    expect(yRange([10, 12], 20)).toEqual([10 - 0.8, 20 + 0.6])
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
  it('in decimal TB (`units: TB`): the axis, quota share and call-out', () => {
    const TB = 1e12
    expect(texts(digestSvg({
      ...input(false), note: 'example.org · TB', units: 'TB',
      points: [{ t: day(1), bytes: 15 * TB }, { t: day(7), bytes: 15.2 * TB }, { t: day(14), bytes: 15.4 * TB }],
      quota: { bytes: 253 * TB, name: '253 TB public' },
    }))).toEqual([
      'Usage — October 2026', 'example.org · TB',
      '0', '50', '100', '150', '200', '250',
      '10/5', '10/12', '10/19', '10/26',
      '253 TB public quota', '15.4 TB · 6%',
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
  it('rasterizes the storage-class card at 1.5×', async () => {
    const { mkdir, readFile, writeFile } = await fsp()
    const fonts = await Promise.all(['public/_fonts/Inter-400.ttf', 'public/_fonts/Inter-600.ttf'].map(async f => new Uint8Array(await readFile(f))))
    const png = svgToPng(tiersSvg(tiersInput()), fonts, 1.5)
    const v = new DataView(png.buffer, png.byteOffset)
    expect([v.getUint32(16), v.getUint32(20)]).toEqual([1350, 813])
    await mkdir('tmp', { recursive: true })
    await writeFile('tmp/digest-tiers-sample.png', png)
    await writeFile('tmp/digest-tiers-sample-1.png', svgToPng(tiersSvg(tiersInput(1)), fonts, 1.5))  // a month's first scan
  })
})

// A storage-class month: 10/1 → 10/17 daily, Coldline shrinking by a 280 TiB cleanup on 10/9, Standard growing.
const tierNames = ['Archive', 'Coldline', 'Nearline', 'Standard']
const tiersInput = (n = 17): TiersCardInput => ({
  title: 'marin GCS — October 2026', note: 'gcs.oa.dev · TiB',
  tiers: tierNames.map((name, i) => ({ name, color: TIER_COLORS[i] })),
  points: Array.from({ length: n }, (_, i) => ({
    t: day(i + 1, 7),
    bytes: [120, i < 8 ? 2700 + 6 * i : 2420 + 6 * i, 15, 280 + 4 * i].map(v => v * T),
  })),
  from: day(1) - 16 * 3600_000, to: day(31, 16),
})

describe('fmtIn', () => {
  it('formats in the one unit, never rescaling', () => {
    expect([3030.4 * T, 15.25 * T, 0].map(fmtIn('TiB'))).toEqual(['3,030 TiB', '15.3 TiB', '0 TiB'])
    expect([1.5e15, 15.4e12].map(fmtIn('TB'))).toEqual(['1,500 TB', '15.4 TB'])
  })
})

describe('tiersSvg', () => {
  it('draws the total line, the stack and the legend (latest sizes, top tier first)', () => {
    expect(texts(tiersSvg(tiersInput()))).toEqual([
      'marin GCS — October 2026', 'gcs.oa.dev · TiB',
      // the total line: autofit y ticks, no x labels, the latest total called out in the axis' unit
      '2,900', '3,000', '3,100', '3,200', '2,995 TiB',
      // the stack: y from 0, Monday x ticks
      '0', '1,000', '2,000', '3,000',
      '10/5', '10/12', '10/19', '10/26',
      'Standard 344 TiB', 'Nearline 15 TiB', 'Coldline 2,516 TiB', 'Archive 120 TiB',
    ])
  })
  it('stacks one band per tier, bottom → top, each between its cumulative edges', () => {
    const svg = tiersSvg(tiersInput(2))
    expect([...svg.matchAll(/<polygon points="([^"]*)" fill="(#[0-9a-f]+)" fill-opacity="0.92"\/>/g)].map(m => [m[2], m[1]])).toEqual([
      ['#8a897f', '92.7,462.8 118.5,462.8 118.5,472 92.7,472'],
      ['#3987e5', '92.7,255.1 118.5,254.6 118.5,462.8 92.7,462.8'],
      ['#e3b341', '92.7,253.9 118.5,253.5 118.5,254.6 92.7,255.1'],
      ['#f2635a', '92.7,232.4 118.5,231.6 118.5,253.5 92.7,253.9'],
    ])
  })
  it('a lone scan is a stacked bar', () => {
    const svg = tiersSvg(tiersInput(1))
    expect([...svg.matchAll(/<rect x="([^"]*)" y="([^"]*)" width="([^"]*)" height="([^"]*)" fill="(#[0-9a-f]+)" fill-opacity="0.92"\/>/g)].map(m => m.slice(1))).toEqual([
      ['82.4', '462.7', '20.6', '9.3', '#8a897f'],
      ['82.4', '254.4', '20.6', '208.4', '#3987e5'],
      ['82.4', '253.2', '20.6', '1.2', '#e3b341'],
      ['82.4', '231.6', '20.6', '21.6', '#f2635a'],
    ])
    expect([...svg.matchAll(/<polygon [^>]*fill-opacity="0.92"\/>/g)]).toEqual([])  // no zero-width bands
  })
})
