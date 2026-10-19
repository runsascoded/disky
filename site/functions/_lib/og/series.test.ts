/** The size-over-time card: its series query, its data from a fixture series, its SVG, and a real PNG through resvg
 * (`tmp/og-series-{full,anon,roots,long,many}.png` for a look). */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { cardSvg, type CardData } from './card'
import { ensureWasm, svgToPng } from './render'
import { seriesCard, seriesCardData, seriesQuery, type SeriesBody } from './series'
import { slotColor } from '../../../src/colors'

const mocks = vi.hoisted(() => ({ read: vi.fn(), scan: vi.fn() }))
vi.mock('../../api/series', () => ({ seriesRead: mocks.read }))
vi.mock('./data', () => ({ resolveScan: mocks.scan }))
vi.mock('../scanTimes', () => ({ scanTimes: vi.fn(async () => ({})) }))
vi.mock('../identity', () => ({ canonId: (u: string) => (u === 'Alan' ? 'alan-turing' : u), loadRegistry: vi.fn(async () => ({})) }))

interface Fs { mkdir(p: string, o: { recursive: boolean }): Promise<unknown>; readFile(p: string): Promise<Uint8Array>; writeFile(p: string, d: Uint8Array): Promise<void> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>

const T = 2 ** 40
const SITE = { name: 'marin GCS', scheme: 'gs://' }
const SCANS = ['2026-06-01T1200', '2026-07-01T1200', '2026-08-01T1200', '2026-09-01T1200', '2026-10-01T1200', '2026-10-12T1200']
const at = (id: string) => Date.UTC(+id.slice(0, 4), +id.slice(5, 7) - 1, +id.slice(8, 10), 12)
/** A store root's series: bucket `a` grows 100 → 150 TiB; `b` appears in July at 20 TiB and grows to 45. */
const A = [100, 110, 120, 130, 140, 150]
const B = [null, 20, 25, 35, 40, 45]
const rootsBody: SeriesBody = {
  points: SCANS.map((date, i) => ({ date, b: (A[i] + (B[i] ?? 0)) * T, o: 1000 * (i + 1) })),
  roots: [
    { path: 'a', points: SCANS.map((date, i) => ({ date, b: A[i] * T, o: 600 * (i + 1) })) },
    { path: 'b', points: SCANS.flatMap((date, i) => B[i] == null ? [] : [{ date, b: B[i]! * T, o: 400 * (i + 1) }]) },
  ],
}
const pathBody: SeriesBody = { points: SCANS.map((date, i) => ({ date, b: A[i] * T, o: 600 * (i + 1) })) }
/** The chart without its label function (compared on its own). */
const shape = (d: CardData) => d.chart ? { ...d, chart: { ...d.chart, fmtX: undefined } } : d
const textNodes = (svg: string) => [...svg.matchAll(/<text\b[^>]*>(.*?)<\/text>/g)].map(m => m[1])

describe('seriesQuery: the `/api/series` query the page sends for the view', () => {
  it('maps the view params to the series API as `SizeOverTime` scopes it', () => {
    expect([
      seriesQuery({}),
      seriesQuery({ path: 'a/b' }),
      seriesQuery({ path: 'a', o: 'Alan', cl: 'hot' }, u => (u === 'Alan' ? 'alan-turing' : u)),
      seriesQuery({ o: '' }),
      seriesQuery({ path: 'a', o: '*', f: 'ckpt', qs: 'g' }),
      seriesQuery({ o: '!alan,bob' }),
      seriesQuery({ path: 'a', o: 'me' }),
    ].map(q => q?.toString() ?? null)).toEqual([
      'path=&split=roots',
      'path=a%2Fb',
      'path=a&lens=user%3Aalan-turing&cl=hot',
      'path=&o=unowned',
      'path=a&o=owned&q=ckpt&qs=g',
      'path=&o=%21alan%2Cbob',
      null,
    ])
  })
})

describe('seriesCardData', () => {
  it('a path: one trace, its span and latest size, through the selected scan', () => {
    const d = seriesCardData(SITE, 'a · size over time', 'full', pathBody, {}, SCANS[5])
    expect(shape(d)).toEqual({
      tier: 'full', site: 'marin GCS', title: 'a · size over time', tiles: [],
      subtitle: '2026-06-01 → 2026-10-12 · 6 scans · +50.0 TiB',
      total: '150 TiB · 3,600 objects',
      chart: { total: SCANS.map((s, i) => [at(s), A[i] * T]), fmtX: undefined },
    })
    expect(SCANS.map(s => d.chart!.fmtX(at(s)))).toEqual(['Jun 1', 'Jul 1', 'Aug 1', 'Sep 1', 'Oct 1', 'Oct 12'])
  })
  it('an older scan selected: later points are left off (that card stays that card)', () => {
    const d = seriesCardData(SITE, 'a · size over time', 'anon', pathBody, {}, SCANS[2])
    expect([d.subtitle, d.total, d.chart!.total]).toEqual([
      '2026-06-01 → 2026-08-01 · 3 scans · +20.0 TiB', '120 TiB · 1,800 objects',
      SCANS.slice(0, 3).map((s, i) => [at(s), A[i] * T]),
    ])
  })
  it('the store root: the buckets stacked as the page stacks them (largest at the latest scan = slot 0), named in the legend', () => {
    const d = seriesCardData(SITE, 'marin GCS · size over time', 'full', rootsBody, {}, SCANS[5])
    expect(shape(d)).toEqual({
      tier: 'full', site: 'marin GCS', title: 'marin GCS · size over time', tiles: [],
      subtitle: '2026-06-01 → 2026-10-12 · 6 scans · +95.0 TiB',
      total: '195 TiB · 6,000 objects',
      chart: {
        total: SCANS.map((s, i) => [at(s), (A[i] + (B[i] ?? 0)) * T]),
        bands: [
          { color: slotColor(0), points: SCANS.map((s, i) => [at(s), 0, A[i] * T]) },
          { color: slotColor(1), points: SCANS.slice(1).map((s, i) => [at(s), A[i + 1] * T, (A[i + 1] + B[i + 1]!) * T]) },
        ],
        fmtX: undefined,
      },
      legend: [{ label: 'a', color: slotColor(0), b: 150 * T }, { label: 'b', color: slotColor(1), b: 45 * T }],
    })
  })
  it('too few scans, or nothing in any, is said instead of a chart', () => {
    expect([
      seriesCardData(SITE, 't', 'full', { points: pathBody.points.slice(0, 1) }, {}, SCANS[5]),
      seriesCardData(SITE, 't', 'full', { points: SCANS.map(date => ({ date, b: 0, o: 0 })) }, {}, SCANS[5]),
    ]).toEqual([
      { tier: 'full', site: 'marin GCS', title: 't', tiles: [], subtitle: '', total: '100 TiB · 600 objects', empty: 'not enough scans yet' },
      { tier: 'full', site: 'marin GCS', title: 't', tiles: [], subtitle: '2026-06-01 → 2026-10-12 · 6 scans · +0 B', total: '0 B · 0 objects', empty: 'nothing here in any scan' },
    ])
  })
  it('a span under two days labels its axis with times; over a year, with months', () => {
    const short = seriesCardData(SITE, 't', 'full', { points: [{ date: '2026-10-12T0100', b: T, o: 1 }, { date: '2026-10-12T1300', b: 2 * T, o: 2 }] }, {}, '2026-10-12T1300')
    const long = seriesCardData(SITE, 't', 'full', { points: [{ date: '2025-01-15T0000', b: T, o: 1 }, { date: '2026-10-12T0000', b: 2 * T, o: 2 }] }, {}, '2026-10-12T0000')
    expect([short.chart!.fmtX(Date.UTC(2026, 9, 12, 13)), long.chart!.fmtX(Date.UTC(2025, 0, 15))]).toEqual(['Oct 12 13:00', 'Jan ’25'])
  })
})

describe('cardSvg: the chart', () => {
  it('a labelled card: y ticks, date ticks, the latest value; the anon card the same shape, no numbers on the chart', () => {
    const full = seriesCardData(SITE, 'a · size over time', 'full', pathBody, {}, SCANS[5])
    const anon = seriesCardData(SITE, 'a · size over time', 'anon', pathBody, {}, SCANS[5])
    const marks = (svg: string) => ['grid', 'area', 'line', 'pt', 'last', 'band', 'total'].map(c => [c, svg.split(`class="${c}"`).length - 1])
    expect([textNodes(cardSvg(full)), textNodes(cardSvg(anon))]).toEqual([
      ['marin GCS', 'a · size over time', '2026-06-01 → 2026-10-12 · 6 scans · +50.0 TiB', '0', '50.0 TiB', '100 TiB', '150 TiB',
        'Jun 1', 'Jul 4', 'Aug 7', 'Sep 9', 'Oct 12', '150 TiB', '150 TiB · 3,600 objects'],
      ['marin GCS', 'a · size over time', '2026-06-01 → 2026-10-12 · 6 scans · +50.0 TiB',
        'Jun 1', 'Jul 4', 'Aug 7', 'Sep 9', 'Oct 12', '150 TiB · 3,600 objects'],
    ])
    expect([marks(cardSvg(full)), marks(cardSvg(anon))]).toEqual([
      [['grid', 4], ['area', 1], ['line', 1], ['pt', 6], ['last', 1], ['band', 0], ['total', 0]],
      [['grid', 4], ['area', 1], ['line', 1], ['pt', 6], ['last', 1], ['band', 0], ['total', 0]],
    ])
  })
  it('the store root: a band per bucket and the total over them; the legend names them on a labelled card only', () => {
    const full = cardSvg(seriesCardData(SITE, 'marin GCS · size over time', 'full', rootsBody, {}, SCANS[5]))
    const anon = cardSvg(seriesCardData(SITE, 'marin GCS · size over time', 'anon', rootsBody, {}, SCANS[5]))
    expect([full.split('class="band"').length - 1, full.split('class="total"').length - 1, textNodes(full).slice(-2), textNodes(anon).slice(-1)])
      .toEqual([2, 1, ['a 150 TiB', 'b 45.0 TiB'], ['195 TiB · 6,000 objects']])
  })
  it('every drawn band is named: long bucket names shrink to fit beside the footer, and what still can\'t is counted', () => {
    const T = 2 ** 40
    const legend = (names: [string, number][]) => names.map(([label, b], i) => ({ label, color: slotColor(i), b: b * T }))
    const chart = { total: [[0, 1], [1, 2]] as [number, number][], fmtX: String }
    const card = (names: [string, number][]) => {
      const svg = cardSvg({ tier: 'full', site: 'cw', title: 'cw', subtitle: '', total: '964 TiB · 400,000,000 objects', tiles: [], chart, legend: legend(names) })
      return textNodes(svg).filter(t => !/^(\d|Jan|0$|1$)/.test(t)).slice(2)
    }
    const cw: [string, number][] = [['marin-us-east-02a', 811], ['marin-us-east-06a', 77.4], ['hero-checkpoints', 75]]
    expect([
      card(cw),
      card([...cw, ['marin-us-east-04a', 0.5], ['marin-us-east-05a', 0.1]]),
      card([...Array(20)].map((_, i) => [`marin-bucket-${i}`, 20 - i] as [string, number])),
    ]).toEqual([
      ['marin-us-east-02a 811 TiB', 'marin-us-east-06a 77.4 TiB', 'hero-checkpoints 75.0 TiB'],
      ['marin-us-east-02a 811 TiB', 'marin-us-east-06a 77.4 TiB', 'hero-checkpoints 75.0 TiB', 'marin-us-east-04a 512 GiB', 'marin-us-east-05a 102 GiB'],
      ['marin-bucket-0 20.0 TiB', 'marin-bucket-1 19.0 TiB', 'marin-bucket-2 18.0 TiB', 'marin-bucket-3 17.0 TiB', 'marin-bucket-4 16.0 TiB', 'marin-bucket-5 15.0 TiB', 'marin-bucket-6 14.0 TiB', '+13 more'],
    ])
  })
})

describe('seriesCard: the page\'s series, read in-process', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.scan.mockResolvedValue(SCANS[5])
  })
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  it('asks `seriesRead` the page\'s query on the card\'s env, with no identity (the card\'s own credential gated it)', async () => {
    mocks.read.mockResolvedValue(json(pathBody))
    const env = { STORE_KEY: 'meta' } as never
    const d = await seriesCard(env, SITE, 'a · size over time', { path: 'a', o: 'Alan', d: SCANS[5] }, 'full', 'https://x.example')
    const [ctx, gate] = mocks.read.mock.calls[0]
    expect([ctx.request.url, ctx.env, await gate(ctx), mocks.scan.mock.calls, d.total]).toEqual([
      'https://x.example/api/series?path=a&lens=user%3Aalan-turing', env, null, [[env, SCANS[5]]], '150 TiB · 3,600 objects',
    ])
  })
  it('refusals are drawn as such; read errors as "series unavailable", never cached; `o=me` asks nothing', async () => {
    const out: (Pick<CardData, 'empty' | 'failed'>)[] = []
    for (const [res, params] of [
      [json({ error: 'path not found' }, 404), { path: 'gone' }],
      [json({ error: 'a filtered series needs its match roots (paths=)' }, 400), { f: 'x y' }],
      [json({ error: 'boom' }, 503), {}],
      [json({ ...pathBody, failed: [SCANS[2]] }), { path: 'a' }],
    ] as [Response, Record<string, string>][]) {
      mocks.read.mockResolvedValueOnce(res)
      const d = await seriesCard({} as never, SITE, 't', params, 'full', 'https://x.example')
      out.push({ empty: d.empty, failed: d.failed })
    }
    const me = await seriesCard({} as never, SITE, 't', { o: 'me' }, 'full', 'https://x.example')
    expect([out, me.empty, mocks.read.mock.calls.length]).toEqual([[
      { empty: 'path not found', failed: undefined },
      { empty: 'filter not charted over time', failed: undefined },
      { empty: 'series unavailable', failed: true },
      { empty: undefined, failed: true },
    ], 'choose a named owner before sharing', 4])
  })
})

describe('the series card as a PNG (resvg, the site\'s fonts)', () => {
  let fonts: Uint8Array[]
  beforeAll(async () => {
    const { readFile } = await fsp()
    await ensureWasm(await readFile('functions/_lib/og/vendor/resvg.wasm'))
    fonts = await Promise.all(['public/_fonts/Inter-400.ttf', 'public/_fonts/Inter-600.ttf'].map(async f => new Uint8Array(await readFile(f))))
  })
  it('renders 1200×630 PNGs: a path (full, anon) and the store root\'s stack', async () => {
    const { mkdir, writeFile } = await fsp()
    await mkdir('tmp', { recursive: true })
    const cards: [string, CardData][] = [
      ['full', seriesCardData(SITE, 'marin-us-central2/checkpoints · size over time', 'full', pathBody, {}, SCANS[5])],
      ['anon', seriesCardData(SITE, 'marin-us-central2/checkpoints · size over time', 'anon', pathBody, {}, SCANS[5])],
      ['roots', seriesCardData(SITE, 'marin GCS · size over time', 'full', rootsBody, {}, SCANS[5])],
      ...([['long', 5], ['many', 20]] as [string, number][]).map(([name, k]): [string, CardData] => {
        const d = seriesCardData(SITE, 'marin GCS · size over time', 'full', rootsBody, {}, SCANS[5])
        return [name, { ...d, total: '964 TiB · 400,000,000 objects', legend: [...Array(k)].map((_, i) => ({ label: `marin-us-east-0${i}a`, color: slotColor(i), b: (k - i) * 2 ** 40 })) }]
      }),
    ]
    const dims: [number, number][] = []
    for (const [name, d] of cards) {
      const png = svgToPng(cardSvg(d), fonts)
      const v = new DataView(png.buffer, png.byteOffset)
      dims.push([v.getUint32(16), v.getUint32(20)])
      await writeFile(`tmp/og-series-${name}.png`, png)
    }
    expect(dims).toEqual([[1200, 630], [1200, 630], [1200, 630], [1200, 630], [1200, 630]])
  })
})
