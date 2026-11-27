// specs/done/health-page.md: the /health computation over a fixture of both stores. The interval store missed scan 04 (an
// out-of-order append can never join) and has two level-0 runs due to carry; the static index's run for 05 has no
// drill, which cuts the drill stack there (06's drill is built but unserved), and the two level-0 runs never carry
// (one drilled, one not). Scan 07's job is still running.
import { describe, expect, it } from 'vitest'
import {
  compaction, coverage, coverageEvents, freshness, type HealthRun, healthDoc, type MergeRecord, mergeLine, mergeState, planCarries,
  stackTiers, type StackRun, storeHealth, type StoreRead,
  attempt, foldDue, peakLabel, type Upkeep, type UpkeepInputs, upkeepAnchor, upkeepChip, upkeepLabel, upkeepStrip,
} from './healthModel'
import { machineMemBytes, notedPeakBytes, type ScanRun, type ScanRunPhase, type ScanRunStatus } from './scanRunsModel'

const D = (d: string) => `2026-10-${d}`
const run = (first: string, last: string, level: number, scans: string[], extra: Partial<HealthRun> = {}): HealthRun => ({
  key: first === last ? `deltas/${D(first)}` : `deltas/${D(first)}_${D(last)}`, first: D(first), last: D(last), level, scans: scans.map(D), r2: true, ...extra,
})
const all = { light: true, drill: true, anchors: true }

const IV: StoreRead = {
  kind: 'interval', prefix: 'interval-store', gen: 'iv1', manifest: '2026-10-06.json', rev: 0, revises: null, manifest_ts: 1_791_000_000, compact_level: 5,
  base: ['01', '02', '03'].map(D),
  runs: [run('05', '05', 0, ['05']), run('06', '06', 0, ['06'])],
}
const ST: StoreRead = {
  kind: 'static', prefix: 'static-names', gen: 'sn1', manifest: '2026-10-06.m001.json', rev: 1, revises: 'manifests/2026-10-06.json', manifest_ts: 1_791_100_000, compact_level: 5,
  base: ['01', '02'].map(D), base_tiers: all,
  runs: [
    run('03', '04', 1, ['03', '04'], { tiers: all }),
    run('05', '05', 0, ['05'], { tiers: { ...all, drill: false } }),
    run('06', '06', 0, ['06'], { tiers: all }),
  ],
}
const NOW = Date.UTC(2026, 9, 8) / 1000
const PER_SCAN = ['01', '02', '03', '04', '05'].map(D)
const JOBS = [{ scan: D('06'), status: 'succeeded' }, { scan: D('07'), status: 'running' }]

it('scan availability is a set of instants, with no invented duration between snapshots', () => {
  const doc = healthDoc([], ['2026-10-13T0221', '2026-10-13T0716'], [], NOW)
  expect(coverageEvents(doc.coverage, ['path', 'interval'])).toEqual([
    { column: 'path', events: [
      { scan: '2026-10-13T0221', time: Date.UTC(2026, 9, 13, 2, 21), status: 'present' },
      { scan: '2026-10-13T0716', time: Date.UTC(2026, 9, 13, 7, 16), status: 'present' },
    ] },
    { column: 'interval', events: [] },
  ])
})

describe('planCarries (append_runner.plan_carries)', () => {
  const r = (k: string, level: number, n = 1): StackRun => ({ key: `deltas/${k}`, first: k, last: k, level, scans: Array.from({ length: n }, (_, i) => `${k}.${i}`) })
  it('replays the binary counter: chained carries fold into one N-way merge', () => {
    const { after, carries } = planCarries([r('a', 1, 2), r('b', 0), r('c', 0)])
    expect(after.map(x => [x.key, x.level, x.scans.length])).toEqual([['deltas/a_c', 2, 4]])
    expect(carries).toEqual([{ inputs: ['deltas/a', 'deltas/b', 'deltas/c'], output: 'deltas/a_c', level: 2, first: 'a', last: 'c', scans: 4 }])
  })
  it('never mixes a drilled run with an undrilled one, nor carries into the compaction level', () => {
    expect(planCarries([r('a', 0), r('b', 0)], new Set(['deltas/a'])).carries).toEqual([])
    expect(planCarries([r('a', 4, 16), r('b', 4, 16)]).carries).toEqual([])
    expect(compaction(planCarries([r('a', 4, 16), r('b', 4, 16)]).after, 5)).toEqual({ level: 5, run_scans: 32, capacity: 32, due: true, max_level: 4 })
  })
})

describe('storeHealth', () => {
  it('interval: a due carry, the gauge, every scan served', () => {
    const h = storeHealth(IV)
    expect([h.carries, h.compaction, h.newest, h.served, h.cut]).toEqual([
      [{ inputs: ['deltas/2026-10-05', 'deltas/2026-10-06'], output: 'deltas/2026-10-05_2026-10-06', level: 1, first: D('05'), last: D('06'), scans: 2 }],
      { level: 5, run_scans: 2, capacity: 32, due: false, max_level: 1 },
      D('06'),
      { light: ['01', '02', '03', '05', '06'].map(D), drill: null, anchors: null },
      { light: null, drill: null, anchors: null },
    ])
  })
  it('static: the drill stack is cut at the drill-less run; no carry across it', () => {
    const h = storeHealth(ST)
    expect([h.carries, h.compaction, h.served, h.cut]).toEqual([
      [],
      { level: 5, run_scans: 4, capacity: 32, due: false, max_level: 1 },
      { light: ['01', '02', '03', '04', '05', '06'].map(D), drill: ['01', '02', '03', '04'].map(D), anchors: ['01', '02', '03', '04', '05', '06'].map(D) },
      { light: null, drill: 'deltas/2026-10-05', anchors: null },
    ])
  })
})

describe('coverage', () => {
  const scans = ['01', '02', '03', '04', '05', '06', '07'].map(D)
  const cov = (st: StoreRead = ST) => coverage(scans, new Set(PER_SCAN), new Set([D('07')]), [storeHealth(IV), storeHealth(st)])
  const grid = (c: ReturnType<typeof cov>) => c.map(x => [x.scan.slice(8), x.cells.path, x.cells.interval, x.cells.light, x.cells.drill, x.cells.anchors, x.cells.r2, x.gaps.join(',')])
  it('flags the scan missing from the interval store and the drill gaps; the running scan is pending', () => {
    expect(grid(cov())).toEqual([
      ['01', 'present', 'present', 'present', 'present', 'present', 'present', ''],
      ['02', 'present', 'present', 'present', 'present', 'present', 'present', ''],
      ['03', 'present', 'present', 'present', 'present', 'present', 'present', ''],
      ['04', 'present', 'missing', 'present', 'present', 'present', 'present', 'interval'],
      ['05', 'present', 'present', 'present', 'missing', 'present', 'present', 'drill'],
      ['06', 'present', 'present', 'present', 'missing', 'present', 'present', 'drill'],
      ['07', 'pending', 'pending', 'pending', 'pending', 'pending', 'na', ''],
    ])
  })
  it('a listed run not whole on R2 cuts the light stack there: its scan and every later one', () => {
    const broken = { ...ST, runs: ST.runs.map(r => (r.key === 'deltas/2026-10-05' ? { ...r, r2: false } : r)) }
    expect(grid(cov(broken)).slice(4, 6)).toEqual([
      ['05', 'present', 'present', 'missing', 'missing', 'missing', 'missing', 'light,drill,anchors,r2'],
      ['06', 'present', 'present', 'missing', 'missing', 'missing', 'present', 'light,drill,anchors'],
    ])
  })
  it('a store that isn\'t configured is `na` in its columns', () => {
    const c = coverage([D('01')], new Set([D('01')]), new Set(), [storeHealth(IV)])
    expect(grid(c)).toEqual([['01', 'present', 'present', 'na', 'na', 'na', 'present', '']])
  })
})

describe('freshness and the document', () => {
  it('ages from the scan ids and the manifests\' uploads', () => {
    const doc = healthDoc([IV, ST], PER_SCAN, JOBS, NOW)
    expect([doc.scans, doc.gaps, doc.freshness]).toEqual([
      ['01', '02', '03', '04', '05', '06', '07'].map(D),
      { path: 0, interval: 1, light: 0, drill: 2, anchors: 0, r2: 0 },
      {
        newest_scan: D('07'), newest_scan_age: 86_400,
        stores: [
          { kind: 'interval', newest: D('06'), newest_age: 172_800, manifest_ts: 1_791_000_000, manifest_age: NOW - 1_791_000_000 },
          { kind: 'static', newest: D('06'), newest_age: 172_800, manifest_ts: 1_791_100_000, manifest_age: NOW - 1_791_100_000 },
        ],
      },
    ])
    expect(freshness([], [], NOW)).toEqual({ newest_scan: null, newest_scan_age: null, stores: [] })
  })
})

describe('a running job whose rows have `started_ts` NULL (gcs, 2026-10-10)', () => {
  // T1514's job has published its per-scan path index; its interval and static appends haven't landed. Neither 10-10
  // run recorded a start (the job bug fixed in 6842ed63).
  const T = (iso: string) => Date.parse(iso) / 1000
  const X = (id: string) => `2026-10-${id}`
  const iv: StoreRead = { ...IV, manifest_ts: T('2026-10-10T03:00:00Z'), base: ['09T1236'].map(X), runs: [{ key: 'deltas/2026-10-10', first: X('10'), last: X('10'), level: 0, scans: [X('10')], r2: true }] }
  const st: StoreRead = { ...ST, manifest_ts: T('2026-10-10T03:10:00Z'), base: ['09T1236', '10'].map(X), runs: [] }
  const per = ['09T1236', '10', '10T1514'].map(X)
  const NOW10 = T('2026-10-10T16:00:00Z')
  const jobs = (status1514: string) => [
    { scan: X('09T1236'), status: 'succeeded', started_ts: T('2026-10-09T12:36:30Z') },
    { scan: X('10'), status: 'succeeded', started_ts: null },
    { scan: X('10T1514'), status: status1514, started_ts: null },
  ]
  const grid = (d: ReturnType<typeof healthDoc>) => d.coverage.map(x => [x.scan, x.cells.path, x.cells.interval, x.cells.light, x.cells.drill, x.cells.anchors, x.cells.r2])
  it('the running scan is pending in the stores it hasn\'t reached, not a gap', () => {
    const doc = healthDoc([iv, st], per, jobs('running'), NOW10)
    expect([grid(doc), doc.gaps]).toEqual([
      [
        [X('09T1236'), 'present', 'present', 'present', 'present', 'present', 'present'],
        [X('10'), 'present', 'present', 'present', 'present', 'present', 'present'],
        [X('10T1514'), 'present', 'pending', 'pending', 'pending', 'pending', 'na'],
      ],
      { path: 0, interval: 0, light: 0, drill: 0, anchors: 0, r2: 0 },
    ])
  })
  it('mutation: once its job has stopped, the same scan\'s absences are gaps', () => {
    const doc = healthDoc([iv, st], per, jobs('failed'), NOW10)
    expect([grid(doc).slice(2), doc.gaps]).toEqual([
      [[X('10T1514'), 'present', 'missing', 'missing', 'missing', 'missing', 'na']],
      { path: 0, interval: 1, light: 1, drill: 1, anchors: 1, r2: 0 },
    ])
  })
  it('a finished job\'s scan that nothing serves is `unserved`, not a coverage row with gaps', () => {
    const doc = healthDoc([iv, st], per, [...jobs('running'), { scan: '2026-08-10', status: 'succeeded', started_ts: T('2026-08-10T07:01:25Z') }], NOW10)
    expect([doc.scans, doc.unserved, doc.gaps]).toEqual([
      [X('09T1236'), X('10'), X('10T1514')],
      ['2026-08-10'],
      { path: 0, interval: 0, light: 0, drill: 0, anchors: 0, r2: 0 },
    ])
  })
  it('a running job that names no scan (its id unmatched) still makes a scan past every store\'s newest pending — only that one', () => {
    const scans = ['09T1236', '10', '10T1514'].map(X)
    const stores = [{ ...iv, base: [X('09T1236')], runs: [] }, st].map(s => storeHealth(s))
    const c = coverage(scans, new Set(), new Set(), stores, true)
    expect(c.map(x => [x.scan, x.cells.path, x.cells.interval, x.cells.light])).toEqual([
      [X('09T1236'), 'present', 'present', 'present'],
      [X('10'), 'missing', 'missing', 'present'],
      [X('10T1514'), 'pending', 'pending', 'pending'],
    ])
    expect(coverage(scans, new Set(), new Set(), stores, false).map(x => x.cells.interval)).toEqual(['present', 'missing', 'missing'])
  })
  it('ages a scan from its job\'s start when known, else its id\'s time', () => {
    const withStart = [...jobs('running').slice(0, 1), { scan: X('10'), status: 'succeeded', started_ts: T('2026-10-10T01:00:00Z') }, ...jobs('running').slice(2)]
    expect(healthDoc([iv, st], per, withStart, NOW10).freshness).toEqual({
      newest_scan: X('10T1514'), newest_scan_age: T('2026-10-10T16:00:00Z') - T('2026-10-10T15:14:00Z'),
      stores: [
        { kind: 'interval', newest: X('10'), newest_age: 15 * 3600, manifest_ts: T('2026-10-10T03:00:00Z'), manifest_age: 13 * 3600 },
        { kind: 'static', newest: X('10'), newest_age: 15 * 3600, manifest_ts: T('2026-10-10T03:10:00Z'), manifest_age: 13 * 3600 - 600 },
      ],
    })
    expect(healthDoc([iv, st], per, jobs('running'), NOW10).freshness.stores.map(f => f.newest_age)).toEqual([16 * 3600, 16 * 3600])
  })
})

describe('stack → shards (CoverTimeline rows)', () => {
  const axis = ['01', '02', '03', '04', '05', '06', '07'].map(D)
  const t = (d: string) => `${D(d)}T00:00:00.000Z`
  const shape = (rows: ReturnType<typeof stackTiers>) => rows.map(r => [r.tier, r.segments.map(s => [s.start.slice(8, 10), s.end.slice(8, 10), s.status, s.shardDur, s.key])])
  it('interval: the base, a row per level down from the compaction\'s, the due carry pending on its level', () => {
    const rows = stackTiers(storeHealth(IV), axis, NOW * 1000)
    expect(shape(rows)).toEqual([
      ['base', [['01', '04', 'present', '3 scans', 'iv1 (base)']]],
      ['L4', []], ['L3', []], ['L2', []],
      ['L1', [['05', '07', 'pending', 'due carry of 2 runs · 2 scans', 'deltas/2026-10-05_2026-10-06']]],
      ['L0', [['05', '06', 'present', '1 scan', 'deltas/2026-10-05'], ['06', '07', 'present', '1 scan', 'deltas/2026-10-06']]],
    ])
    expect(rows[0].segments[0]).toEqual({ start: t('01'), end: t('04'), shardDur: '3 scans', status: 'present', key: 'iv1 (base)' })
    expect(rows.map(r => [r.totalExpected, r.totalPresent, r.totalPending, r.complete])).toEqual([[1, 1, 0, true], [0, 0, 0, true], [0, 0, 0, true], [0, 0, 0, true], [1, 0, 1, true], [2, 2, 0, true]])
  })
  it('static: drill and anchors rows mark the drill-less run missing', () => {
    expect(shape(stackTiers(storeHealth(ST), axis, NOW * 1000)).slice(4)).toEqual([
      ['L1', [['03', '05', 'present', '2 scans', 'deltas/2026-10-03_2026-10-04']]],
      ['L0', [['05', '06', 'present', '1 scan', 'deltas/2026-10-05'], ['06', '07', 'present', '1 scan', 'deltas/2026-10-06']]],
      ['drill', [['01', '03', 'present', 'drill · base', 'sn1 (base)'], ['03', '05', 'present', 'drill · 2 scans', 'deltas/2026-10-03_2026-10-04'], ['05', '06', 'missing', 'drill · 1 scan', 'deltas/2026-10-05'], ['06', '07', 'present', 'drill · 1 scan', 'deltas/2026-10-06']]],
      ['anch', [['01', '03', 'present', 'anchors · base', 'sn1 (base)'], ['03', '05', 'present', 'anchors · 2 scans', 'deltas/2026-10-03_2026-10-04'], ['05', '06', 'present', 'anchors · 1 scan', 'deltas/2026-10-05'], ['06', '07', 'present', 'anchors · 1 scan', 'deltas/2026-10-06']]],
    ])
  })
})

describe('merges in progress (the status record, `merging.json`)', () => {
  const T = NOW + 7 * 3600  // 2026-10-08 07:00 UTC
  const AB = { inputs: ['deltas/2026-10-05', 'deltas/2026-10-06'], output: 'deltas/2026-10-05_2026-10-06', level: 1, first: D('05'), last: D('06'), scans: 2 }
  const rec = (over: Partial<MergeRecord> = {}): MergeRecord => ({
    holder: 'iv-merge-2026-10-06-060000', owner: 'uid:7', state: 'merging', started_ts: T - 3600, updated_ts: T - 120, heartbeat_s: 300, lease_s: 14400,
    manifest: 'manifests/2026-10-06.json', merges: [AB], done: [], ...over,
  })
  const status = (r: MergeRecord) => { const m = mergeState(r, T)!; return [m.status, m.age, m.beat_age, m.why] }

  it('merging while the heartbeat is fresh; stuck once it stops (30 min) or the lease runs out; failed as recorded', () => {
    expect([
      status(rec()),
      status(rec({ updated_ts: T - 1800 })),
      status(rec({ updated_ts: T - 1801 })),
      status(rec({ started_ts: T - 14401, updated_ts: T - 60 })),
      status(rec({ started_ts: T - 7200, updated_ts: T - 3700, lease_s: undefined })),
      status(rec({ state: 'failed', updated_ts: T - 600, error: 'Boom: killed while building' })),
      mergeState(null, T),
    ]).toEqual([
      ['merging', 3600, 120, null],
      ['merging', 3600, 1800, null],
      ['stuck', 3600, 1801, 'no heartbeat for 30m'],
      ['stuck', 14401, 60, 'held 4h00m, past its 4h00m lease'],
      ['stuck', 7200, 3700, 'no heartbeat for 1h01m'],
      ['failed', 3600, 600, 'Boom: killed while building'],
      null,
    ])
  })

  it('one line: since when, each planned merge (runs → output, level); the date when it isn\'t today', () => {
    const two = rec({ merges: [AB, { inputs: ['deltas/a_b', 'deltas/c', 'deltas/d'], output: 'deltas/a_d', level: 2, first: 'a', last: 'd', scans: 4 }] })
    expect([
      mergeLine(mergeState(rec(), T)!, T),
      mergeLine(mergeState(two, T)!, T),
      mergeLine(mergeState(rec({ started_ts: T - 8 * 3600, updated_ts: T - 3 * 3600 }), T)!, T),
      mergeLine(mergeState(rec({ state: 'failed', updated_ts: T - 600 }), T)!, T),
    ]).toEqual([
      'merging since 06:00 UTC: 2 runs → deltas/2026-10-05_2026-10-06 (L1)',
      'merging since 06:00 UTC: 2 runs → deltas/2026-10-05_2026-10-06 (L1); 3 runs → deltas/a_d (L2)',
      'merging since 2026-10-07 23:00 UTC: 2 runs → deltas/2026-10-05_2026-10-06 (L1)',
      'merge failed at 06:50 UTC: 2 runs → deltas/2026-10-05_2026-10-06 (L1)',
    ])
  })

  it('the planned output is a pending shard on its level, in place of the due carry it is', () => {
    const axis = ['01', '02', '03', '04', '05', '06', '07'].map(D)
    const l1 = (r: MergeRecord | null) => {
      const rows = stackTiers(storeHealth({ ...IV, merging: r }, T), axis, T * 1000)
      return rows.find(x => x.tier === 'L1')!.segments.map(x => [x.start.slice(8, 10), x.end.slice(8, 10), x.status, x.shardDur, x.key])
    }
    expect([l1(null), l1(rec()), l1(rec({ updated_ts: T - 3600 })), l1(rec({ state: 'failed' }))]).toEqual([
      [['05', '07', 'pending', 'due carry of 2 runs · 2 scans', AB.output]],
      [['05', '07', 'pending', 'merging of 2 runs · 2 scans', AB.output]],
      [['05', '07', 'pending', 'stuck merge of 2 runs · 2 scans', AB.output]],
      // a failed merge holds nothing: its carry is due again
      [['05', '07', 'pending', 'due carry of 2 runs · 2 scans', AB.output]],
    ])
    // a planned merge R2's manifest doesn't list yet (it lags GCS) is drawn on its own level; one already listed is not
    const ahead = { inputs: ['x', 'y'], output: 'deltas/2026-10-01_2026-10-02', level: 3, first: D('01'), last: D('02'), scans: 8 }
    const rows = stackTiers(storeHealth({ ...IV, merging: rec({ merges: [ahead, { ...AB, output: 'deltas/2026-10-05' }] }) }, T), axis, T * 1000)
    expect(rows.filter(x => x.segments.some(g => g.shardDur.startsWith('merging'))).map(x => [x.tier, x.segments.map(g => g.key)])).toEqual([['L3', [ahead.output]]])
  })

  it('healthDoc ages each store\'s merge at its `now`', () => {
    const doc = healthDoc([{ ...IV, merging: rec({ updated_ts: T - 2000 }) }, ST], PER_SCAN, JOBS, T)
    expect(doc.stores.map(s => s.merge && [s.merge.status, s.merge.why, s.merge.holder])).toEqual([['stuck', 'no heartbeat for 33m', 'iv-merge-2026-10-06-060000'], null])
  })
})

// specs/store-upkeep-monitoring.md phase 1: each store's upkeep from its stack, D1's maintenance rows and R2's manifest
// uploads and `compaction.json`, over fixtures rebuilt from the incidents.
describe('upkeep', () => {
  const T = (iso: string) => Date.parse(iso) / 1000
  /** `n` scan ids from `from`, `stepH` hours apart: dates when daily, else `YYYY-MM-DDTHHMM`. */
  const ids = (from: string, n: number, stepH = 24) => Array.from({ length: n }, (_, i) => {
    const iso = new Date(Date.parse(from) + i * stepH * 3_600_000).toISOString()
    return stepH === 24 ? iso.slice(0, 10) : `${iso.slice(0, 10)}T${iso.slice(11, 13)}${iso.slice(14, 16)}`
  })
  const stackRun = (scans: string[], level: number): HealthRun => ({
    key: scans.length === 1 ? `deltas/${scans[0]}` : `deltas/${scans[0]}_${scans[scans.length - 1]}`, first: scans[0], last: scans[scans.length - 1], level, scans, r2: true,
  })
  /** A `scan_runs` row: finished `secs` after its start unless running. */
  function R(run_id: string, kind: string, status: ScanRunStatus, scan: string, gen: string | null, start: string | number, secs: number | null, over: Partial<ScanRun> = {}): ScanRun {
    const started_ts = typeof start === 'number' ? start : T(start), finished_ts = status === 'running' || secs == null ? null : started_ts + secs
    return {
      run_id, store: 'primary', scan, kind, parent: null, status, failed_phase: null, error: null, started_ts, finished_ts,
      job_name: null, region: null, machine: null, spot: null, tasks: null, image: null, cost_usd: null, gen, source: 'job',
      updated_ts: finished_ts ?? started_ts, ...over,
    }
  }
  const upk = (s: StoreRead, inputs: UpkeepInputs, now: number): Upkeep => storeHealth(s, now, inputs).upkeep
  const inputs = (runs: ScanRun[], over: Partial<UpkeepInputs> = {}): UpkeepInputs => ({ runs, phases: [], uploads: new Map(), compaction: null, ...over })

  // gcs, 11-25 → 11-26: the static index's level-5 fold due (two L4 runs) and never submitted
  const G = ids('2026-10-25', 32)
  const GEN_G = 'static-names/2026-10-08c.g4'
  const GCS: StoreRead = {
    kind: 'static', prefix: 'static-names', gen: '2026-10-08c.g4', manifest: '2026-11-25.m001.json', rev: 1, revises: 'manifests/2026-11-25.json',
    manifest_ts: T('2026-11-25T09:00:00Z'), compact_level: 5, base: ids('2026-10-01', 24), runs: [stackRun(G.slice(0, 16), 4), stackRun(G.slice(16), 4)],
  }
  const G_UPLOADS = new Map([['2026-11-24.json', T('2026-11-24T08:05:00Z')], ['2026-11-25.json', T('2026-11-25T08:10:00Z')], ['2026-11-25.m001.json', T('2026-11-25T09:00:00Z')]])
  const G_COMPACTION = { doc: { from: '2026-10-08c.g3', scans: 24, level: 5 }, ts: T('2026-10-24T12:00:00Z') }
  const G_FOLD = R('g-fold-1024', 'compact', 'succeeded', '2026-10-24', 'static-names/2026-10-08c.g3 → 2026-10-08c.g4', '2026-10-24T09:00:00Z', 3 * 3600, { machine: 'n2-highmem-32', spot: 1, cost_usd: 2.5 })
  const G_MERGE = R('g-merge-1125', 'merge', 'succeeded', '2026-11-25', GEN_G, '2026-11-25T08:40:00Z', 720)
  const G_SCAN = R('g-scan-1125', 'scan', 'succeeded', '2026-11-25', null, '2026-11-25T07:00:00Z', 3600)
  const NOW_G = T('2026-11-26T20:00:00Z')
  const WHY_G = 'level 5: two runs at level 4 (deltas/2026-10-25_2026-11-09, deltas/2026-11-10_2026-11-25)'
  const gIn = (runs: ScanRun[], phases: ScanRunPhase[] = []) => inputs(runs, { phases, uploads: G_UPLOADS, compaction: G_COMPACTION })

  it('gcs 11-25: a fold due since the scan that made the L4 pair, no attempt since: `fold-due`, overdue from its manifest\'s upload', () => {
    const A = (r: ScanRun) => attempt(r, [], NOW_G)
    expect(upk(GCS, gIn([G_SCAN, G_MERGE, G_FOLD]), NOW_G)).toEqual({
      policy: 'level:5', status: 'fold-due', why: WHY_G, overdue_s: 129_000,
      fold: {
        due: { why: WHY_G, since_scan: '2026-11-25', since_ts: T('2026-11-25T08:10:00Z') }, attempts: [], failing: 0, running: null, usual_s: 10_800,
        last: { from: '2026-10-08c.g3', gen: '2026-10-08c.g4', ts: T('2026-10-24T12:00:00Z'), scans: 24, rule: 'level 5', run: A(G_FOLD) },
      },
      carry: { due: [], failing: 0, running: null, last: A(G_MERGE) },
      history: [A(G_MERGE), A(G_FOLD)],
    })
    // its manifest's upload unknown: due all the same, with no overdue figure
    const bare = upk(GCS, inputs([]), NOW_G)
    expect([bare.status, bare.overdue_s, foldDue(GCS.runs, 5, new Map())]).toEqual(['fold-due', null, { why: WHY_G, since_scan: '2026-11-25', since_ts: null }])
  })

  // cw, 11-22 → 11-26: the static fold OOM-killed every attempt, 12 h apart; the last lost outright (exit 137)
  const C = ids('2026-11-14T12:00:00Z', 47, 6)
  const GEN_C = 'static-names/2026-10-15cw.c003'
  const CW: StoreRead = {
    kind: 'static', prefix: 'static-names', gen: '2026-10-15cw.c003', manifest: '2026-11-26T0000.json', rev: 0, revises: null,
    manifest_ts: T('2026-11-26T00:40:00Z'), compact_level: 5, base: ids('2026-10-15T00:00:00Z', 32, 6),
    runs: [stackRun(C.slice(0, 16), 4), stackRun(C.slice(16, 32), 4), stackRun(C.slice(32, 40), 3), stackRun(C.slice(40, 44), 2), stackRun(C.slice(44, 46), 1), stackRun(C.slice(46), 0)],
  }
  const OOM = 'lost: Batch job FAILED; last event: Task state is updated from RUNNING to FAILED on zone us-east5-a: exit code 137'
  const cf = (n: number, scan: string, start: string, over: Partial<ScanRun> = {}) => R(`c-fold-${n}`, 'compact', 'failed', scan, GEN_C, start, 9060,
    { failed_phase: 'fold', machine: 'n2-highmem-48', spot: 0, cost_usd: 4.1, error: 'OutOfMemoryException: failed to allocate 2.0 GiB', ...over })
  const C_F1 = cf(1, '2026-11-22T1200', '2026-11-22T12:05:00Z')
  const C_F2 = cf(2, '2026-11-23T0000', '2026-11-23T00:05:00Z')
  const C_F3 = cf(3, '2026-11-25T1200', '2026-11-25T12:02:00Z')
  const C_F4 = cf(4, '2026-11-26T0000', '2026-11-26T00:03:00Z', { error: OOM })
  const C_NOP = R('c-fold-0', 'compact', 'nop', '2026-11-20T0000', GEN_C, '2026-11-20T01:00:00Z', 600)
  const C_MERGE = R('c-merge', 'merge', 'succeeded', '2026-11-25T1200', GEN_C, '2026-11-25T12:40:00Z', 360, { machine: 'n2-highmem-16' })
  const C_PREV = R('c-fold-prev', 'compact', 'succeeded', '2026-11-14T0600', 'static-names/2026-10-15cw.c002 → 2026-10-15cw.c003', '2026-11-14T06:04:00Z', 15_120,
    { machine: 'n2-highmem-48', cost_usd: 11.2 })
  const C_PHASES: ScanRunPhase[] = [
    { run_id: 'c-fold-4', phase: 'read', seq: 0, started_ts: T('2026-11-26T00:05:00Z'), finished_ts: T('2026-11-26T00:12:00Z'), status: 'done', note: '812 files, 214.1 GiB from r2 in 410s' },
    { run_id: 'c-fold-4', phase: 'fold', seq: 2, started_ts: T('2026-11-26T00:13:00Z'), finished_ts: null, status: 'running', note: 'drill 3/7; work disk peak 640 GiB, RSS peak 371 GiB' },
    { run_id: 'c-fold-3', phase: 'fold', seq: 2, started_ts: T('2026-11-25T12:10:00Z'), finished_ts: null, status: 'failed', note: 'failed: drill 3/7; work disk peak 630 GiB, RSS peak 369 GiB' },
  ]
  const NOW_C = T('2026-11-26T03:00:00Z')

  it('an attempt: its row\'s facts, its notes\' peak against its machine\'s memory', () => {
    const a = attempt(C_F4, C_PHASES, NOW_C)
    expect(a).toEqual({
      run_id: 'c-fold-4', kind: 'compact', status: 'failed', scan: '2026-11-26T0000', gen: GEN_C,
      started_ts: T('2026-11-26T00:03:00Z'), finished_ts: T('2026-11-26T00:03:00Z') + 9060, secs: 9060,
      machine: 'n2-highmem-48', spot: false, cost_usd: 4.1, error: OOM, failed_phase: 'fold', note: null, lost: false,
      mem_peak_bytes: 371 * 2 ** 30, mem_limit_bytes: 384 * 2 ** 30,
    })
    expect([peakLabel(a), peakLabel({ mem_peak_bytes: 371 * 2 ** 30, mem_limit_bytes: null }), peakLabel(attempt(C_MERGE, C_PHASES, NOW_C))]).toEqual(['371/384G', '371G', null])
  })

  it('cw 11-22 → 11-26: four failed folds since due (one lost, exit 137) are `failing ×4`; the not-due attempt before is none of them', () => {
    const A = (r: ScanRun) => attempt(r, C_PHASES, NOW_C)
    const why = `level 5: two runs at level 4 (${CW.runs[0].key}, ${CW.runs[1].key})`
    const u = upk(CW, inputs([C_F1, C_F2, C_NOP, C_F3, C_MERGE, C_F4, C_PREV], {
      phases: C_PHASES, uploads: new Map([['2026-11-22T0600.json', T('2026-11-22T06:40:00Z')], ['2026-11-26T0000.json', T('2026-11-26T00:40:00Z')]]),
      compaction: { doc: { from: '2026-10-15cw.c002', scans: 32, level: 5 }, ts: T('2026-11-14T10:20:00Z') },
    }), NOW_C)
    expect(u).toEqual({
      policy: 'level:5', status: 'failing', why: `fold: 4 failed attempts in a row; the last: ${OOM}`, overdue_s: null,
      fold: {
        due: { why, since_scan: '2026-11-22T0600', since_ts: T('2026-11-22T06:40:00Z') }, attempts: [C_F4, C_F3, C_F2, C_F1].map(A), failing: 4, running: null, usual_s: 15_120,
        last: { from: '2026-10-15cw.c002', gen: '2026-10-15cw.c003', ts: T('2026-11-14T10:20:00Z'), scans: 32, rule: 'level 5', run: A(C_PREV) },
      },
      carry: { due: [], failing: 0, running: null, last: A(C_MERGE) },
      history: [C_F4, C_MERGE, C_F3, C_F2, C_F1, C_NOP, C_PREV].map(A),
    })
    expect([upkeepLabel(u), upkeepChip(u)]).toEqual(['failing ×4', 'failed'])
  })

  // cw: `cw-sn-merge` failed 8 times; `merging.json` keeps only the newest failure
  const M_CARRY = {
    inputs: ['deltas/2026-11-20T0000_2026-11-20T0600', 'deltas/2026-11-20T1200', 'deltas/2026-11-20T1800'], output: 'deltas/2026-11-20T0000_2026-11-20T1800', level: 2,
    first: '2026-11-20T0000', last: '2026-11-20T1800', scans: 4,
  }
  const M_ERR = "RuntimeError: deltas/2026-11-20T0000_2026-11-20T1800/ holds 3 objects this merge doesn't write (e.g. deltas/2026-11-20T0000_2026-11-20T1800/catalog/meta.json): not merging into it"
  const MS: StoreRead = {
    kind: 'static', prefix: 'static-names', gen: 'snm', manifest: '2026-11-20T1800.json', rev: 0, revises: null, manifest_ts: T('2026-11-20T19:00:00Z'), compact_level: 5,
    base: ['2026-11-19T0000'], runs: [stackRun(['2026-11-20T0000', '2026-11-20T0600'], 1), stackRun(['2026-11-20T1200'], 0), stackRun(['2026-11-20T1800'], 0)],
    merging: { holder: 'cw-sn-merge-8', state: 'failed', started_ts: T('2026-11-25T21:00:00Z'), updated_ts: T('2026-11-25T21:10:00Z'), merges: [M_CARRY], error: M_ERR },
  }
  const M_ROWS = [
    R('m-0', 'merge', 'succeeded', '2026-11-20T0600', 'static-names/snm', '2026-11-24T12:00:00Z', 900),
    ...Array.from({ length: 8 }, (_, i) => R(`m-${i + 1}`, 'merge', 'failed', '2026-11-20T1800', 'static-names/snm', T('2026-11-25T00:00:00Z') + i * 3 * 3600, 600, { error: M_ERR, failed_phase: 'carry L2' })),
  ]
  const NOW_M = T('2026-11-26T03:00:00Z')

  it('cw\'s merges: eight failed in a row since the last success are carries `failing ×8`, the carry still due', () => {
    const A = (r: ScanRun) => attempt(r, [], NOW_M)
    const u = upk(MS, inputs(M_ROWS), NOW_M)
    expect(u).toEqual({
      policy: 'level:5', status: 'failing', why: `carries: 8 failed merges in a row; the last: ${M_ERR}`, overdue_s: null,
      fold: { due: null, attempts: [], failing: 0, running: null, usual_s: null, last: null },
      carry: { due: [M_CARRY], failing: 8, running: null, last: A(M_ROWS[8]) },
      history: [...M_ROWS].reverse().map(A),
    })
    expect(upkeepLabel(u)).toEqual('failing ×8')
    // no rows recorded (a merge job without `record_secrets`): the failed record alone is one failure
    const bare = upk(MS, inputs([]), NOW_M)
    expect([bare.status, bare.why, bare.carry.failing]).toEqual(['failing', `carries: 1 failed merge in a row; the last: ${M_ERR}`, 1])
  })

  it('ok, carry due, carrying: the stack alone', () => {
    expect(storeHealth({ ...IV, runs: IV.runs.slice(0, 1) }).upkeep).toEqual({
      policy: 'level:5', status: 'ok', why: null, overdue_s: null,
      fold: { due: null, attempts: [], failing: 0, running: null, usual_s: null, last: null },
      carry: { due: [], failing: 0, running: null, last: null },
      history: [],
    })
    const now = NOW + 3600
    const merging: MergeRecord = { holder: 'iv-merge', state: 'merging', started_ts: now - 600, updated_ts: now - 60, merges: [storeHealth(IV).carries[0]] }
    expect([storeHealth(IV).upkeep.status, storeHealth({ ...IV, merging }, now).upkeep.status, storeHealth({ ...IV, merging }, now).upkeep.carry.due]).toEqual(['carry-due', 'carrying', []])
  })

  describe('a fold\'s attempts (gcs\'s due fold)', () => {
    const fresh = (over: Partial<ScanRun> = {}) => R('r1', 'compact', 'running', '2026-11-26', GEN_G, '2026-11-26T18:00:00Z', null, { updated_ts: NOW_G - 60, ...over })
    const PH: ScanRunPhase[] = [
      { run_id: 'r1', phase: 'read', seq: 0, started_ts: T('2026-11-26T18:01:00Z'), finished_ts: T('2026-11-26T18:10:00Z'), status: 'done', note: '300 files' },
      { run_id: 'r1', phase: 'fold', seq: 2, started_ts: T('2026-11-26T18:11:00Z'), finished_ts: null, status: 'running', note: 'fold: 2/7 parts; work disk peak 80 GiB, RSS peak 41.5 GiB' },
    ]
    const failed = (id: string, start: string) => R(id, 'compact', 'failed', '2026-11-25', GEN_G, start, 600, { error: `Boom ${id}` })
    const view = (u: Upkeep) => [u.status, u.why, u.fold.attempts.map(a => a.run_id), u.fold.failing, u.fold.running?.run_id ?? null, u.overdue_s]

    it('folding: a running attempt with a fresh record, its live note and peak', () => {
      const u = upk(GCS, gIn([fresh(), G_FOLD], PH), NOW_G)
      expect([view(u), u.fold.running]).toEqual([
        ['folding', null, ['r1'], 0, 'r1', null],
        {
          run_id: 'r1', kind: 'compact', status: 'running', scan: '2026-11-26', gen: GEN_G, started_ts: T('2026-11-26T18:00:00Z'), finished_ts: null, secs: 7200,
          machine: null, spot: null, cost_usd: null, error: null, failed_phase: null, note: 'fold: 2/7 parts; work disk peak 80 GiB, RSS peak 41.5 GiB', lost: false,
          mem_peak_bytes: 41.5 * 2 ** 30, mem_limit_bytes: null,
        },
      ])
      // a retry running after failures is folding; the failures behind it still count
      expect(view(upk(GCS, gIn([fresh(), failed('f1', '2026-11-26T06:00:00Z')], PH), NOW_G))).toEqual(['folding', null, ['r1', 'f1'], 1, 'r1', null])
    })

    it('stuck: quiet past `LOST_S` (31 min), or running past twice the median of the last three succeeded folds', () => {
      expect(view(upk(GCS, gIn([fresh({ updated_ts: NOW_G - 1860 })], PH), NOW_G))).toEqual(['stuck', 'fold r1: no record write for 31m', ['r1'], 0, 'r1', null])
      expect(upk(GCS, gIn([fresh({ updated_ts: NOW_G - 1800 })], PH), NOW_G).status).toEqual('folding')
      const prior = [100_000, 10_800, 7200, 3600].map((s, i) => R(`p${i}`, 'compact', 'succeeded', `2026-10-0${i + 1}`, `static-names/a${i} → b${i}`, `2026-10-0${i + 1}T00:00:00Z`, s))
      const long = (secs: number) => upk(GCS, gIn([...prior, fresh({ started_ts: NOW_G - secs, updated_ts: NOW_G - 30 })]), NOW_G)
      expect([long(14_401).fold.usual_s, long(14_401).status, long(14_401).why, long(14_400).status]).toEqual([7200, 'stuck', 'fold running 4h00m, past 2× its usual 2h00m', 'folding'])
    })

    it('a superseded `nop` counts as neither an attempt nor a failure; a success ends the streak, a candidate build doesn\'t', () => {
      const sup = R('n1', 'compact', 'nop', '2026-11-25', `${GEN_G} (superseded: 2026-11-26, not published)`, '2026-11-26T09:00:00Z', 3000)
      expect(view(upk(GCS, gIn([sup]), NOW_G))).toEqual(['fold-due', WHY_G, ['n1'], 0, null, 129_000])
      expect(view(upk(GCS, gIn([failed('f1', '2026-11-26T06:00:00Z'), sup]), NOW_G))).toEqual(['failing', 'fold: 1 failed attempt in a row; the last: Boom f1', ['n1', 'f1'], 1, null, null])
      const rows = [
        failed('x1', '2026-11-26T12:00:00Z'),
        R('x2', 'compact', 'succeeded', '2026-11-25', `${GEN_G} → 2026-10-08c.g5 (a candidate, not promoted)`, '2026-11-26T10:00:00Z', 600),
        R('x3', 'compact', 'succeeded', '2026-11-25', `${GEN_G} → 2026-10-08c.g5`, '2026-11-26T08:00:00Z', 600),
        failed('x4', '2026-11-26T06:00:00Z'),
        failed('x5', '2026-11-26T04:00:00Z'),
      ]
      expect(view(upk(GCS, gIn(rows), NOW_G))).toEqual(['failing', 'fold: 1 failed attempt in a row; the last: Boom x1', ['x1', 'x2', 'x3', 'x4', 'x5'], 1, null, null])
      // mutation: without the success the streak runs on through x4 and x5
      expect(upk(GCS, gIn(rows.filter(r => r.run_id !== 'x3')), NOW_G).fold.failing).toEqual(3)
    })

    it('the last fold is the succeeded row ending `→ <gen>`, never a candidate build\'s', () => {
      const cand = R('cand', 'compact', 'succeeded', '2026-10-24', 'static-names/2026-10-08c.g3 → 2026-10-08c.g4 (a candidate, not promoted)', '2026-10-24T13:00:00Z', 900)
      const u = upk(GCS, gIn([cand, G_FOLD]), NOW_G)
      expect([u.fold.last?.run?.run_id, u.fold.usual_s, u.history.map(a => a.run_id)]).toEqual(['g-fold-1024', 10_800, ['cand', 'g-fold-1024']])
      // built, not folded: no last fold, whatever the rows say
      expect(upk(GCS, inputs([G_FOLD]), NOW_G).fold.last).toEqual(null)
    })
  })

  it('chips, labels and the strip: worst first; a fold due turns red at 30 min overdue', () => {
    expect([
      upkeepChip({ status: 'fold-due', overdue_s: null }), upkeepChip({ status: 'fold-due', overdue_s: 1799 }), upkeepChip({ status: 'fold-due', overdue_s: 1800 }),
      ...(['failing', 'stuck', 'folding', 'carrying', 'fold-queued', 'carry-due', 'ok', 'off'] as const).map(status => upkeepChip({ status, overdue_s: null })),
    ]).toEqual(['warn', 'warn', 'failed', 'failed', 'warn', 'running', 'running', 'running', '', 'succeeded', 'cancelled'])
    const ok = storeHealth({ ...IV, runs: IV.runs.slice(0, 1) }).upkeep, failing = upk(MS, inputs(M_ROWS), NOW_M), due = upk(GCS, inputs([]), NOW_G)
    expect([upkeepLabel(ok), upkeepLabel(due), upkeepLabel(storeHealth(IV).upkeep)]).toEqual(['ok', 'fold due', 'carry due'])
    expect(upkeepStrip([{ kind: 'interval', upkeep: ok }, { kind: 'static', upkeep: failing }]).map(s => [s.kind, s.upkeep.status, s.anchor])).toEqual([
      ['static', 'failing', 'upkeep-static'], ['interval', 'ok', 'upkeep-interval'],
    ])
    expect(['static-names/sn1', 'interval-store/iv1 → iv2', 'history/meta/interval-store/iv1', 'other/x', null].map(g => upkeepAnchor(g))).toEqual(['upkeep-static', 'upkeep-interval', 'upkeep-interval', null, null])
  })

  it('machine memory and noted peaks', () => {
    expect(['n2-highmem-48', 'n2d-standard-16', 'n2-highcpu-32', 'e2-medium', null].map(m => machineMemBytes(m))).toEqual([384 * 2 ** 30, 64 * 2 ** 30, 32 * 2 ** 30, null, null])
    expect([notedPeakBytes(['read; RSS peak 12 GiB', null, 'RSS peak 3.5 GiB; then RSS peak 40 GiB', 'no peak']), notedPeakBytes([]), notedPeakBytes([null])]).toEqual([40 * 2 ** 30, null, null])
  })
})
