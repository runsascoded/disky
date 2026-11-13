/** The run card's shaping as pure functions, its text nodes exactly, and one PNG per state for a look
 * (`tmp/og-run-<name>.png`). */
import { beforeAll, describe, expect, it } from 'vitest'
import type { ProgressSample } from '../../../src/runHistory'
import type { ProgressFile } from '../../../src/runs'
import { ensureWasm, svgToPng } from './render'
import { MAX_POINTS, percent, rateSeries, runBuckets, runCardSvg, runIdOfPath, runImagePath, runMaxAge, runVersion, shapeRun, validRunId, type RunRow } from './run'

interface Fs { mkdir(p: string, o: { recursive: boolean }): Promise<unknown>; readFile(p: string): Promise<Uint8Array>; writeFile(p: string, d: Uint8Array): Promise<void> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>

const T0 = 1793796765
const run: RunRow = { mode: 'real', scan: '2026-11-03T1622', started_ts: T0, finished_ts: null, deleted_objects: 0, buckets: 'a,b,c', log_dir: 'gs://x/sweep/p/r' }
const sample = (bucket: string, ts: number, deletes: number, failed = 0, done = 0): ProgressSample => ({ bucket, ts, deletes, bytes: 7, gone: 0, overwritten: 0, failed, done, bytes_exact: 1 })
const file = (updated: string | null, del: number, failed = 0): ProgressFile => ({ roots: 1, roots_done: 0, decisions: { delete: del, delete_failed: failed, skipped_gone: 9 }, delete_bytes: 7, started: '2026-11-04T12:52:45Z', updated, done: false })
const iso = (s: number) => new Date(s * 1000).toISOString()

describe('run ids', () => {
  it('validates, and never throws on a bad pathname', () => {
    expect(['2026-11-03T1622-p1/20261104T125245Z', 'a', '../x', 'a//b', 'a/', "a'b", 'a b', 'x'.repeat(129)].map(validRunId)).toEqual([true, true, false, false, false, false, false, false])
    expect(['/runs/2026-11-03T1622-p1%2F20261104T125245Z', '/runs/a/b', '/runs', '/runs/', '/runs/%E0%A4%A', '/runs/%2E%2E%2Fx'].map(runIdOfPath)).toEqual(['2026-11-03T1622-p1/20261104T125245Z', 'a/b', null, null, null, null])
  })
  it('keeps only key-safe buckets', () => {
    expect([runBuckets('a-1,b.2'), runBuckets(null), runBuckets('../x,ok')]).toEqual([['a-1', 'b.2'], [], ['ok']])
  })
})

describe('runVersion: a new image per few minutes of samples, final once finished', () => {
  it('buckets the newest sample; rolls with the clock without a fresh one; finished is final', () => {
    const now = 1793800200
    expect([
      runVersion({ finished_ts: 123 }, 99, now),
      runVersion({ finished_ts: null }, now - 10, now),
      runVersion({ finished_ts: null }, now - 301, now),
      runVersion({ finished_ts: null }, now - 3600, now),
      runVersion({ finished_ts: null }, null, now),
      runVersion({ finished_ts: null }, null, now + 300),
    ]).toEqual(['f123', 'r5979333', 'r5979332', 'r5979334', 'r5979334', 'r5979335'])
    expect([runMaxAge({ finished_ts: null }), runMaxAge({ finished_ts: 1 })]).toEqual([300, 2592000])
    expect(runImagePath({ id: 'p/r', version: 'r1' })).toEqual('/og/run.png?id=p%2Fr&v=r1&c=2')
  })
})

describe('rateSeries: the drawn series is capped', () => {
  const pt = (ts: number, rate: number | null) => ({ ts, objects: 0, bytes: 0, rate, byteRate: null })
  it('leaves what fits as it is', () => {
    expect(rateSeries([pt(10.4, null), pt(40, 3.14159)])).toEqual([[10, null], [40, 3.1]])
  })
  it('means even time bins past the cap; an all-gap bin stays a gap, an empty one is left out', () => {
    expect(rateSeries([pt(0, 1), pt(1, 3), pt(40, null), pt(41, null), pt(99, 5), pt(100, 7)], 4)).toEqual([[1, 2], [41, null], [100, 6]])
    const long = Array.from({ length: 2000 }, (_, i) => pt(i * 30, 1))
    expect(rateSeries(long).length).toEqual(MAX_POINTS)
  })
})

describe('shapeRun', () => {
  const now = T0 + 600
  it('running: done sums each bucket\'s newer of file and sample, total from the plan, as-of the newest', () => {
    const samples = [sample('a', T0 + 30, 0), sample('a', T0 + 60, 300), sample('b', T0 + 60, 50), sample('c', T0 + 60, 70), sample('b', T0 + 90, 80, 2)]
    const files = new Map([['a', file(iso(T0 + 120), 900, 1)], ['b', file(iso(T0 + 30), 1)], ['c', file(null, 0)]])
    expect(shapeRun(run, 5000, files, samples, ['a', 'b', 'c', 'd'], now)).toEqual({
      mode: 'real', state: 'running', started_ts: T0,
      done: 900 + 80 + 70, total: 5000, unanswered: 3, n_buckets: 3, as_of: T0 + 120,
      series: [[T0 + 30, null], [T0 + 60, 10], [T0 + 90, 11]], peak: 11, latest: 11, avg: (300 + 80 + 70) / 60,
    })
  })
  it('no progress: planning, no done, no as-of; and stalled once it has been too long', () => {
    const planning = { mode: 'real', started_ts: T0, done: null, unanswered: 0, as_of: null, series: [], peak: null, latest: null, avg: null }
    expect(shapeRun(run, null, new Map(), [], [], now)).toEqual({ ...planning, state: 'planning', total: null, n_buckets: 3 })
    expect(shapeRun({ ...run, buckets: null }, 0, new Map(), [], ['a'], now)).toEqual({ ...planning, state: 'planning', total: null, n_buckets: 1 })
    expect(shapeRun(run, 9, new Map(), [], [], T0 + 1801)).toEqual({ ...planning, state: 'stalled', total: 9, n_buckets: 3 })
  })
  it('a quiet run stalls; a finished one takes its row and newest sample, or its finish with none', () => {
    const samples = [sample('a', T0 + 30, 0), sample('a', T0 + 60, 60, 0, 1)]
    expect(shapeRun(run, 100, new Map(), samples, [], T0 + 60 + 1801).state).toEqual('stalled')
    const finished = { ...run, mode: 'dry' as const, finished_ts: T0 + 90, deleted_objects: 60 }
    expect(shapeRun(finished, 100, new Map(), samples, [], now)).toEqual({
      mode: 'dry', state: 'finished', started_ts: T0, done: 60, total: 100, unanswered: 0, n_buckets: 3, as_of: T0 + 60,
      series: [[T0 + 30, null], [T0 + 60, 2]], peak: 2, latest: 2, avg: 2,
    })
    expect(shapeRun(finished, null, new Map(), [], [], now)).toEqual({
      mode: 'dry', state: 'finished', started_ts: T0, done: 60, total: null, unanswered: 0, n_buckets: 3, as_of: T0 + 90,
      series: [], peak: null, latest: null, avg: null,
    })
  })
  it('percent floors: unfinished never reads 100', () => {
    expect([percent(999999, 1000000), percent(1, 8), percent(9, 8)]).toEqual(['99.9%', '12.5%', '100.0%'])
  })
})

const texts = (svg: string) => [...svg.matchAll(/<text[^>]*>(.*?)<\/text>/g)].map(m => m[1].replace(/<[^>]+>/g, ''))
const rising = Array.from({ length: 200 }, (_, i) => sample('a', T0 + 30 * i, i * i * 40))
const stateOf = {
  running: shapeRun(run, 36062888, new Map([['a', file(iso(T0 + 6000), 4889000)]]), rising, [], T0 + 6010),
  planning: shapeRun(run, null, new Map(), [], [], T0 + 60),
  planned: shapeRun(run, 36062888, new Map(), [], [], T0 + 60),
  finished: shapeRun({ ...run, finished_ts: T0 + 6000, deleted_objects: 175466921, buckets: 'a' }, 179330790, new Map(), rising, [], T0 + 9000),
  dry: shapeRun({ ...run, mode: 'dry', finished_ts: T0 + 6000, deleted_objects: 36062888 }, 36062888, new Map(), [], [], T0 + 9000),
}

describe('runCardSvg: counts, rates, times and a bucket count, exactly', () => {
  it('draws each state\'s words', () => {
    expect(Object.fromEntries(Object.entries(stateOf).map(([k, s]) => [k, texts(runCardSvg(s, 'Marin GCS'))]))).toEqual({
      running: ['Marin GCS', 'Deletion running', '4,889,000 / 36,062,888 attempted', '13.5%', 'objects/s', 'latest 529 · peak 529', 'Nov 4 12:52', 'Nov 4 14:32', 'as of 2026-11-04 14:32 UTC', '3 buckets'],
      planning: ['Marin GCS', 'Deletion planning', 'no manifest yet', 'no progress yet', 'started 2026-11-04 12:52 UTC', '3 buckets'],
      planned: ['Marin GCS', 'Deletion planning', '36,062,888 objects planned', 'no progress yet', 'started 2026-11-04 12:52 UTC', '3 buckets'],
      finished: ['Marin GCS', 'Deletion finished', '175,466,921 / 179,330,790 attempted', '97.8%', 'objects/s', 'avg 265 · peak 529', 'Nov 4 12:52', 'Nov 4 14:32', 'as of 2026-11-04 14:32 UTC', '1 bucket'],
      dry: ['Marin GCS', 'Dry run finished', '36,062,888 / 36,062,888 would delete', '100.0%', 'no rate history recorded', 'as of 2026-11-04 14:32 UTC', '3 buckets'],
    })
  })
  let fonts: Uint8Array[]
  beforeAll(async () => {
    const { readFile } = await fsp()
    await ensureWasm(await readFile('functions/_lib/og/vendor/resvg.wasm'))
    fonts = await Promise.all(['public/_fonts/Inter-400.ttf', 'public/_fonts/Inter-600.ttf'].map(async f => new Uint8Array(await readFile(f))))
  })
  it('rasterizes every state at 1200×630', async () => {
    const { mkdir, writeFile } = await fsp()
    await mkdir('tmp', { recursive: true })
    const dims: [number, number][] = []
    for (const [k, s] of Object.entries(stateOf)) {
      const png = svgToPng(runCardSvg(s, 'Marin GCS'), fonts)
      const v = new DataView(png.buffer, png.byteOffset)
      dims.push([v.getUint32(16), v.getUint32(20)])
      await writeFile(`tmp/og-run-${k}.png`, png)
    }
    expect(dims).toEqual(Object.keys(stateOf).map(() => [1200, 630]))
  })
})
