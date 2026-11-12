// specs/done/scan-runs-ui.md: the derivations /scans shows from the D1 rows.
import { describe, expect, it } from 'vitest'
import {
  batchHref, failedAt, logsHref, mapHref, metaHref, outputIndex, phaseIssues, phaseOutcome, phaseOrder, phaseSpans, runTime, type ScanRun, type ScanRunOutput,
  type ScanRunPhase, manualScans, scanRunOf, structureOf, summarize, withDeltas, runOutcome, runOutcomeLine, phaseSkip, overlappingFailures, runTasks, taskLogsHref,
  isMaintenance, liveness, LOST_S, maintStore, phaseStatus, processingSecs, QUIET_S, issueLeaves, issueNext, issueState, issueWhat, outcomeParts, outcomeSteps, passingSince, runOutcomeLabel, statusMatches,
  stepFailure, STATUS_FILTERS, typicalScanSecs, aboutDur,
  type PhaseIssue,
} from './scanRunsModel'

export const run = (o: Partial<ScanRun> & Pick<ScanRun, 'run_id' | 'scan'>): ScanRun => ({
  store: 'primary', kind: 'scan', parent: null, status: 'succeeded', failed_phase: null, error: null, started_ts: 1000,
  finished_ts: 5000, job_name: null, region: null, machine: null, spot: null, tasks: null, image: null, cost_usd: null,
  gen: null, source: 'backfill', updated_ts: 0, ...o,
})
export const out = (run_id: string, key: string, bytes: number | null, rows: number | null = null): ScanRunOutput =>
  ({ run_id, key, uri: `gs://d/${key}/`, bytes, objects: 1, rows, gen: null })
export const phase = (run_id: string, name: string, seq: number, started_ts: number, finished_ts: number): ScanRunPhase =>
  ({ run_id, phase: name, seq, started_ts, finished_ts, status: 'done', note: null })

const RUNS = [
  run({ run_id: 'a', scan: '2026-10-06', started_ts: 100, finished_ts: 400, cost_usd: 1 }),
  run({ run_id: 'b-fail', scan: '2026-10-07', status: 'failed', started_ts: 1000, finished_ts: 1100 }),
  run({ run_id: 'b', scan: '2026-10-07', started_ts: 2000, finished_ts: 2600, cost_usd: 2 }),
  run({ run_id: 'b-list', scan: '2026-10-07', kind: 'listing', parent: 'b', started_ts: 2010, finished_ts: 2300, cost_usd: 0.5 }),
  run({ run_id: 'c', scan: '2026-10-08', status: 'running', started_ts: 3000, finished_ts: null, updated_ts: 3460 }),
]
const OUTS = [
  out('a', 'listing', 100, 10), out('a', 'listing/b1', 100, 10), out('a', 'static', 5), out('a', 'static/deltas/2026-10-06', 5),
  out('b-fail', 'listing', 999),
  out('b', 'listing', 130, 12), out('b', 'listing/b1', 130, 12), out('b', 'snapshot', 7), out('b', 'static', 8), out('b', 'static/deltas/2026-10-07', 8),
  out('c', 'listing', 120, null),
]
const PHASES = [
  phase('b', 'listing', 0, 2000, 2300), phase('b', 'ingest', 1, 2000, 2310), phase('b', 'index', 2, 2310, 2500),
  phase('b-fail', 'listing', 0, 1000, 1050),
]

describe('processing time excludes a delayed capture-to-ingest queue', () => {
  it('shows a two-hour retry independently of four days waiting to start', () => {
    const before = 4 * 86400
    const r = run({ run_id: 'retry', scan: '2026-11-03', started_ts: 0 })
    const spans = phaseSpans(r, [phase('retry', 'capture', 0, 0, 360), phase('retry', 'queue', 1, 360, before)])
    expect(processingSecs(before + 7200, spans)).toEqual(7200)
    expect(processingSecs(before - 1, spans)).toEqual(0)
    expect(processingSecs(null, spans)).toEqual(null)
    expect(processingSecs(7200, [])).toEqual(null)
    expect(processingSecs(7200, [{ ...spans[1], status: 'running' }])).toEqual(null)
  })
})

describe('deltas against the previous scan', () => {
  it('compares each structure to the newest earlier scan that has it; a failed run is no baseline', () => {
    const idx = outputIndex(RUNS, OUTS)
    expect(withDeltas(RUNS[2], OUTS, idx).map(o => [o.key, o.prev_scan, o.delta_bytes, o.delta_rows])).toEqual([
      ['listing', '2026-10-06', 30, 2],
      ['listing/b1', '2026-10-06', 30, 2],
      ['snapshot', null, null, null],
      ['static', '2026-10-06', 3, null],
      ['static/deltas/2026-10-07', '2026-10-06', 3, null],
    ])
    expect(withDeltas(RUNS[4], OUTS, idx).map(o => [o.key, o.prev_scan, o.delta_bytes, o.delta_rows])).toEqual([
      ['listing', '2026-10-07', -10, null],
    ])
  })
  it('abstracts the scan id out of a key', () => {
    expect([structureOf('static/deltas/2026-10-07', '2026-10-07'), structureOf('listing/b1', '2026-10-07')]).toEqual(['static/deltas/{scan}', 'listing/b1'])
  })
})

describe('summarize: one row per orchestrating run, newest first', () => {
  it('folds downstream jobs in, totals top-level outputs, names failures', () => {
    expect(summarize(RUNS, PHASES, OUTS, 3500).map(s => [s.run.run_id, s.secs, s.out_bytes, s.delta_bytes, s.downstream, s.cost_usd, s.failed_at, s.spans.length])).toEqual([
      ['c', 500, 120, -10, 0, null, null, 0],
      ['b', 600, 145, 33, 1, 2.5, null, 3],
      ['b-fail', 100, 999, 899, 0, null, 'after listing', 1],
      ['a', 300, 105, null, 0, 1, null, 0],
    ])
  })
})

describe('scanRunOf: the run behind a scan (the map\'s "scanned … ago" tip links it)', () => {
  it('its newest succeeded run (a failed attempt then a resume), else its newest; none for an unknown scan', () => {
    const rows = summarize(RUNS, PHASES, OUTS, 3500)
    expect(['2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'].map(scan => scanRunOf(rows, scan)?.run_id ?? null)).toEqual([
      'a', 'b', 'c', null,
    ])
  })
})

describe('manualScans: the scans a hand submit started (the size chart marks them)', () => {
  it('a scan takes its first run\'s trigger: a manual resume of a scheduled scan stays scheduled; no trigger is neither', () => {
    const rows = summarize([
      run({ run_id: 's1', scan: '2026-10-17', trigger: 'schedule', started_ts: 100 }),
      run({ run_id: 's1r', scan: '2026-10-17', trigger: 'manual', started_ts: 200 }),
      run({ run_id: 'm1', scan: '2026-10-18T1354', trigger: 'manual', started_ts: 300 }),
      run({ run_id: 'm2', scan: '2026-10-18T2009', trigger: 'manual', started_ts: null }),
      run({ run_id: 'old', scan: '2026-10-15', started_ts: 50 }),
    ], [], [], 3500)
    expect([...manualScans(rows)].sort()).toEqual(['2026-10-18T1354', '2026-10-18T2009'])
  })
})

describe('typicalScanSecs: the confirm\'s "about N hours" for a manual scan', () => {
  it('the median of the newest succeeded scans; failures, other kinds, downstream jobs and running ones don\'t count', () => {
    const h = 3600
    const rows = summarize([
      run({ run_id: 's1', scan: '2026-11-01', started_ts: 1 * 86400, finished_ts: 1 * 86400 + 5 * h }),
      run({ run_id: 's2', scan: '2026-11-02', started_ts: 2 * 86400, finished_ts: 2 * 86400 + 4 * h }),
      run({ run_id: 's3', scan: '2026-11-03', started_ts: 3 * 86400, finished_ts: 3 * 86400 + 6 * h }),
      run({ run_id: 's4', scan: '2026-11-04', started_ts: 4 * 86400, finished_ts: 4 * 86400 + 7 * h }),
      run({ run_id: 'f', scan: '2026-11-05', status: 'failed', started_ts: 5 * 86400, finished_ts: 5 * 86400 + 1 * h }),
      run({ run_id: 'c', scan: '2026-11-05', kind: 'compact', started_ts: 5 * 86400, finished_ts: 5 * 86400 + 20 * h }),
      run({ run_id: 'r', scan: '2026-11-06', status: 'running', started_ts: 6 * 86400, finished_ts: null, updated_ts: 6 * 86400 }),
    ], [], [], 6 * 86400 + 60)
    expect([typicalScanSecs(rows), typicalScanSecs(rows, 3), typicalScanSecs(rows, 1), typicalScanSecs([])]).toEqual([5.5 * h, 6 * h, 7 * h, null])
  })
  it('reads as hours to the half, else minutes', () => {
    expect([aboutDur(5.4 * 3600), aboutDur(3600), aboutDur(1.2 * 3600), aboutDur(40 * 60), aboutDur(20)]).toEqual(
      ['about 5.5 hours', 'about 1 hour', 'about 1 hour', 'about 40 minutes', 'about 1 minute'])
  })
})

describe('runs whose job left `started_ts` NULL (the in-job runs before 6842ed63)', () => {
  // The real gcs order: 10-09T1236 recorded its start; 10-10 (succeeded) and 10-10T1514 (running) didn't, and sorted
  // last, so /health's "last scan job" showed 10-09T1236.
  const T = (iso: string) => Date.parse(iso) / 1000
  const NULLS = [
    run({ run_id: 'r1236', scan: '2026-10-09T1236', started_ts: T('2026-10-09T12:36:30Z'), finished_ts: T('2026-10-09T14:00:00Z'), updated_ts: T('2026-10-09T14:00:00Z') }),
    run({ run_id: 'r1010', scan: '2026-10-10', started_ts: null, finished_ts: T('2026-10-10T02:00:00Z'), updated_ts: T('2026-10-10T02:00:00Z') }),
    run({ run_id: 'r1514', scan: '2026-10-10T1514', status: 'running', started_ts: null, finished_ts: null, updated_ts: T('2026-10-10T15:40:00Z') }),
    run({ run_id: 'odd', scan: 'not-a-scan', started_ts: null, finished_ts: null, updated_ts: T('2026-10-08T00:00:00Z') }),
  ]
  it('orders by `started_ts`, else the scan id\'s time (a date-only id is 00:00 UTC), else `updated_ts`', () => {
    expect(NULLS.map(r => [r.run_id, runTime(r)])).toEqual([
      ['r1236', T('2026-10-09T12:36:30Z')], ['r1010', T('2026-10-10T00:00:00Z')], ['r1514', T('2026-10-10T15:14:00Z')], ['odd', T('2026-10-08T00:00:00Z')],
    ])
    expect(summarize(NULLS, [], [], T('2026-10-10T16:00:00Z')).map(s => s.run.run_id)).toEqual(['r1514', 'r1010', 'r1236', 'odd'])
  })
})

describe('phase spans', () => {
  it('keeps failed overlapping attempts visible in compact charts', () => {
    const ps = [
      phase('b', 'listing', 0, 2000, 2300),
      { ...phase('b', 'names', 1, 2000, 2500), note: 'failed (exit 124)' },
      phase('b', 'ingest', 2, 2000, 2400),
    ]
    expect(overlappingFailures(phaseSpans(RUNS[2], ps))).toEqual([
      { phase: 'names', start: 0, end: 500, status: 'failed', note: 'failed (exit 124)', lane: 1 },
    ])
  })
  it('does not present nonfatal phase failures as clean success or rewrite a failed Batch job', () => {
    const issue = { phase: 'names', outcome: 'timed out', recovery: null }
    expect([
      runOutcome(RUNS[2], []), runOutcome(RUNS[2], [issue]),
      runOutcome(RUNS[2], [{ ...issue, recovery: RUNS[2] }]),
      runOutcome(RUNS[2], [issue, { ...issue, phase: 'iv', recovery: RUNS[2] }]),
      runOutcome(RUNS[1], [{ ...issue, recovery: RUNS[2] }]), runOutcome({ status: 'nop' }),
    ]).toEqual(['succeeded', 'incomplete', 'recovered', 'incomplete', 'failed', 'no-op'])
  })
  it('recognizes historical failure notes and distinguishes timeouts from failures', () => {
    const ps = [
      { ...phase('b', 'names', 0, 2000, 2500), note: 'failed (exit 124)' },
      { ...phase('b', 'iv', 1, 2500, 2600), note: 'wall, failed (exit 1' },
      { ...phase('b', 'ok', 2, 2500, 2600), note: '0 failed dirs' },
    ]
    expect(phaseSpans(RUNS[2], ps).map(p => [p.status, phaseOutcome(p)])).toEqual([
      ['failed', 'timed out'], ['failed', 'failed'], ['done', 'done'],
    ])
  })
  it('only a later same-scan/store, linked successful phase attempt resolves a failure', () => {
    const failed = { ...phase('b', 'names', 0, 2000, 2500), note: 'failed (exit 124)' }
    const good = run({ run_id: 'retry', scan: RUNS[2].scan, kind: 'phase-recovery', parent: 'b', started_ts: 2800, finished_ts: 3000 })
    const candidates = [
      good,
      { ...good, run_id: 'other-scan', scan: '2026-10-08' },
      { ...good, run_id: 'other-store', store: 'meta' },
      { ...good, run_id: 'other-parent', parent: 'a' },
      { ...good, run_id: 'older', started_ts: 2100, finished_ts: 2400 },
      { ...good, run_id: 'failed-retry', status: 'failed' as const, started_ts: 3100, finished_ts: 3200 },
    ]
    const ps = [failed, ...candidates.map(r => phase(r.run_id, 'names', 0, r.started_ts!, r.finished_ts!))]
    expect(phaseIssues(RUNS[2], [RUNS[2], ...candidates], ps)).toEqual([{ phase: 'names', outcome: 'timed out', recovery: good }])
    expect(phaseIssues(RUNS[2], candidates.slice(1), ps)).toEqual([{ phase: 'names', outcome: 'timed out', recovery: null }])
  })
  it('a skipped static-names step leaves the run incomplete, with its recorded reason; the probe\'s skips that follow do not', () => {
    const skip = 'skipped (busy): another coordinator or a compaction holds the static-names store\'s lease; search for this scan comes with its append or the next run'
    const ps: ScanRunPhase[] = [
      phase('b', 'listing', 0, 2000, 2300),
      { ...phase('b', 'static-names', 1, 2300, 2310), note: `overlapped, ${skip}` },
      { ...phase('b', 'probe', 2, 2400, 2500), note: `4 passed, 2 skipped (static-names ${skip}), 0 failed` },
    ]
    const issues = phaseIssues(RUNS[2], [RUNS[2]], ps)
    expect(issues).toEqual([{ phase: 'static-names', outcome: 'skipped', recovery: null, reason: skip }])
    expect([runOutcome(RUNS[2], issues), runOutcomeLine(RUNS[2], issues)]).toEqual(['incomplete', `incomplete: static-names ${skip}`])
    expect(phaseSpans(RUNS[2], ps).map(s => s.status)).toEqual(['done', 'done', 'done'])
  })
  it('a skip clause only at a closed note\'s start or after a comma', () => {
    expect([
      phaseSkip({ status: 'done', note: 'overlapped, skipped (busy): held' }),
      phaseSkip({ status: 'done', note: 'skipped: no current generation' }),
      phaseSkip({ status: 'done', note: 'overlapped' }),
      phaseSkip({ status: 'done', note: '4 passed, 2 skipped (static-names skipped (busy): held), 0 failed' }),
      phaseSkip({ status: 'running', note: 'skipped: a heartbeat line' }),
      phaseSkip({ status: 'done', note: null }),
    ]).toEqual(['skipped (busy): held', 'skipped: no current generation', null, null, null, null])
  })
  it('a failed probe stays a failure beside nothing skipped', () => {
    const ps: ScanRunPhase[] = [
      { ...phase('b', 'static-names', 0, 2300, 2310), note: 'overlapped' },
      { ...phase('b', 'probe', 1, 2400, 2500), note: 'failed (exit 1), 4 passed, 0 skipped, 2 failed' },
    ]
    const issues = phaseIssues(RUNS[2], [RUNS[2]], ps)
    expect(issues).toEqual([{ phase: 'probe', outcome: 'failed', recovery: null, passing: null, detail: '4 passed, 0 skipped, 2 failed' }])
    expect(runOutcomeLine(RUNS[2], issues)).toEqual('incomplete: probe failed')
  })
  it('a phase failed twice is one issue, its attempts counted, the last one\'s verdict its detail', () => {
    const ps: ScanRunPhase[] = [
      { ...phase('b', 'probe', 0, 2400, 2500), note: 'failed (exit 1), 4 passed, 0 skipped, 2 failed' },
      { ...phase('b', 'probe', 1, 2600, 2700), note: 'failed (exit 1), 5 passed, 0 skipped, 1 failed' },
    ]
    expect(phaseIssues(RUNS[2], [RUNS[2]], ps)).toEqual([{ phase: 'probe', outcome: 'failed', recovery: null, passing: null, attempts: 2, detail: '5 passed, 0 skipped, 1 failed' }])
  })
  it('a probe whose only failures are the index\'s lag leaves the run "search behind", not incomplete', () => {
    const lag = 'the static name index covers scans through 2026-10-23T0702, 2 published scans behind (20.1 h); 3 of 9 search requests refused'
    const ps: ScanRunPhase[] = [{ ...phase('b', 'probe', 0, 2400, 2500), note: `search behind: ${lag}` }]
    const issues = phaseIssues(RUNS[2], [RUNS[2]], ps)
    expect(issues).toEqual([{ phase: 'probe', outcome: 'search behind', recovery: null, passing: null, reason: lag }])
    expect([runOutcome(RUNS[2], issues), runOutcomeLine(RUNS[2], issues), phaseSpans(RUNS[2], ps).map(s => s.status)])
      .toEqual(['search behind', `search behind: probe ${lag}`, ['done']])
    const failed = { phase: 'iv', outcome: 'failed', recovery: null }
    expect(runOutcome(RUNS[2], [...issues, failed])).toEqual('incomplete')
  })
  it('puts an overlapped phase on its own lane, on the run clock', () => {
    expect(phaseSpans(RUNS[2], PHASES.filter(p => p.run_id === 'b')).map(s => [s.phase, s.start, s.end, s.lane])).toEqual([
      ['listing', 0, 300, 0], ['ingest', 0, 310, 1], ['index', 310, 500, 0],
    ])
  })
  it('extends an explicitly started phase to now, retaining its overlap with completed work', () => {
    const r = run({ run_id: 'live', scan: '2026-10-13T1300', status: 'running', started_ts: 100, finished_ts: null })
    const ps: ScanRunPhase[] = [
      phase('live', 'publish-r2', 0, 100, 200),
      { ...phase('live', 'static-names', 1, 205, 205), finished_ts: null, status: 'running' },
      phase('live', 'interval-store', 2, 210, 300),
    ]
    expect(phaseSpans(r, ps, 400)).toEqual([
      { phase: 'publish-r2', start: 0, end: 100, status: 'done', note: null, lane: 0 },
      { phase: 'static-names', start: 105, end: 300, status: 'running', note: null, lane: 0 },
      { phase: 'interval-store', start: 110, end: 200, status: 'done', note: null, lane: 1 },
    ])
    expect(phaseSpans(r, ps, 430).map(s => [s.phase, s.end])).toEqual([
      ['publish-r2', 100], ['static-names', 330], ['interval-store', 200],
    ])
  })
  it('orders phase names by their mean position', () => {
    expect(phaseOrder([...PHASES, phase('x', 'index', 1, 0, 1)])).toEqual(['listing', 'ingest', 'index'])
  })
  it('a recorded failed phase wins over "after"', () => {
    expect([failedAt({ ...RUNS[1], failed_phase: 'index' }, PHASES), failedAt(RUNS[2], PHASES)]).toEqual(['index', null])
  })
})

describe('a succeeded run\'s issues, by what their steps declare (`scanSteps.json`: `dt_cloud.scan_steps`)', () => {
  const failed = (name: string, seq = 0, at = 2500): ScanRunPhase => ({ ...phase('b', name, seq, at - 100, at), note: 'failed (exit 1)' })
  const issue = (name: string, recovery: ScanRun | null = null): PhaseIssue => ({ phase: name, outcome: 'failed', recovery })
  const later = run({ run_id: 'c', scan: '2026-10-08', started_ts: 3000, finished_ts: 3600 })

  it('reads a step\'s failure fields; a phase no soft step names has none', () => {
    expect([stepFailure('warm-cache'), stepFailure('digest'), stepFailure('interval-store'), stepFailure('probe'), stepFailure('publish'), stepFailure('toString')]).toEqual([
      { leaves: 'cache not pre-warmed: this scan\'s first views load cold', side: true, converges: false, checks: false },
      { leaves: 'usage digest not posted', side: true, converges: true, checks: false },
      { leaves: 'the interval store doesn\'t hold this scan', side: false, converges: true, checks: false },
      { leaves: 'the serving probe failed: some of this scan\'s views may not load', side: false, converges: false, checks: true },
      undefined, undefined,
    ])
  })
  it('a failed check reads `passing` once a later run\'s same check passed, `incomplete` until one does; a stale search-behind likewise', () => {
    const behind: PhaseIssue = { phase: 'probe', outcome: 'search behind', recovery: null, passing: null, reason: 'lag' }
    expect([
      issueState({ ...issue('probe'), passing: null }), issueState({ ...issue('probe'), passing: later }), issueState({ ...issue('healthcheck'), passing: later }),
      issueState({ ...issue('probe', later), passing: null }), issueState(behind), issueState({ ...behind, passing: later }),
    ]).toEqual(['incomplete', 'passing', 'passing', 'recovered', 'behind', 'passing'])
  })
  it('a run whose worst issue is a check passing since reads what failed and since which scan; an open issue or search behind outranks it, a side step doesn\'t', () => {
    const latest = run({ run_id: 'd', scan: '2026-10-09', started_ts: 4000, finished_ts: 4600 })
    const probe: PhaseIssue = { ...issue('probe'), passing: later }
    const behind: PhaseIssue = { phase: 'probe', outcome: 'search behind', recovery: null, passing: later, reason: 'lag' }
    const cases: PhaseIssue[][] = [
      [probe],
      [issue('warm-cache'), probe, issue('digest', later)],
      [{ ...issue('healthcheck'), passing: latest }, probe],
      [behind],
      [probe, issue('interval-store')],
      [{ ...issue('healthcheck'), passing: later }, { phase: 'probe', outcome: 'search behind', recovery: null, passing: null, reason: 'lag' }],
      [{ ...issue('probe'), passing: null }],
    ]
    expect(cases.map(c => [runOutcome(RUNS[2], c), outcomeSteps(c, runOutcome(RUNS[2], c)), outcomeParts(RUNS[2], c), runOutcomeLabel(RUNS[2], c), passingSince(c)?.run_id ?? null])).toEqual([
      ['passing since', ['probe'], { head: 'probe failed', tail: 'passing since 2026-10-08' }, 'probe failed · passing since 2026-10-08', 'c'],
      ['passing since', ['probe'], { head: 'probe failed', tail: 'passing since 2026-10-08' }, 'probe failed · passing since 2026-10-08', 'c'],
      ['passing since', ['healthcheck', 'probe'], { head: 'healthcheck, probe failed', tail: 'passing since 2026-10-09' }, 'healthcheck, probe failed · passing since 2026-10-09', 'd'],
      ['passing since', ['probe'], { head: 'search behind', tail: 'passing since 2026-10-08' }, 'search behind · passing since 2026-10-08', 'c'],
      ['incomplete', ['interval-store'], { head: 'incomplete', tail: 'interval-store' }, 'incomplete · interval-store', 'c'],
      ['search behind', [], { head: 'search behind', tail: null }, 'search behind', 'c'],
      ['incomplete', ['probe'], { head: 'incomplete', tail: 'probe' }, 'incomplete · probe', null],
    ])
    expect([
      runOutcomeLine(RUNS[2], [issue('warm-cache'), probe]), runOutcomeLine(RUNS[2], [probe, issue('interval-store')]), runOutcomeLine(RUNS[2], [issue('warm-cache')]),
    ]).toEqual(['probe failed · passing since 2026-10-08', 'incomplete: interval-store failed', 'partial: warm-cache failed'])
  })
  it('a side step\'s failure is `side`, any other step\'s (or an unknown phase\'s) `incomplete`; recovered and behind as they are', () => {
    expect([
      issueState(issue('warm-cache')), issueState(issue('digest')), issueState(issue('interval-store')), issueState(issue('publish')),
      issueState(issue('interval-store', later)), issueState(issue('warm-cache', later)),
      issueState({ phase: 'probe', outcome: 'search behind', recovery: null }),
    ]).toEqual(['side', 'side', 'incomplete', 'incomplete', 'recovered', 'recovered', 'behind'])
  })
  it('the run reads as its worst open issue: incomplete, then search behind, then partial, else recovered', () => {
    const behind: PhaseIssue = { phase: 'probe', outcome: 'search behind', recovery: null, reason: 'lag' }
    const cases: PhaseIssue[][] = [
      [issue('warm-cache')],
      [issue('digest', later)],
      [issue('interval-store')],
      [issue('warm-cache'), issue('digest', later)],
      [issue('warm-cache'), issue('probe'), issue('digest', later)],
      [issue('warm-cache'), behind],
      [issue('interval-store'), behind],
      [issue('warm-cache'), issue('gcp-cost'), issue('digest')],
      [issue('static-names', later), issue('digest', later)],
    ]
    expect(cases.map(c => [runOutcome(RUNS[2], c), outcomeSteps(c, runOutcome(RUNS[2], c)), runOutcomeLabel(RUNS[2], c)])).toEqual([
      ['partial', ['warm-cache'], 'partial · warm-cache'],
      ['recovered', ['digest'], 'recovered · digest'],
      ['incomplete', ['interval-store'], 'incomplete · interval-store'],
      ['partial', ['warm-cache'], 'partial · warm-cache'],
      ['incomplete', ['probe'], 'incomplete · probe'],
      ['search behind', [], 'search behind'],
      ['incomplete', ['interval-store'], 'incomplete · interval-store'],
      ['partial', ['warm-cache', 'gcp-cost', 'digest'], 'partial · warm-cache +2'],
      ['recovered', ['static-names', 'digest'], 'recovered · static-names, digest'],
    ])
    expect([runOutcomeLabel(RUNS[2], []), runOutcomeLabel(RUNS[1], [issue('warm-cache')]), runOutcomeLabel({ status: 'nop' })]).toEqual(['succeeded', 'failed', 'no-op'])
    expect(runOutcomeLine(RUNS[2], [issue('warm-cache'), issue('digest', later)])).toEqual('partial: warm-cache failed')
  })
  it('a converging step\'s failure is recovered by the first later run of its kind whose same step then ran and is done', () => {
    const peers = [
      later,
      run({ run_id: 'before', scan: '2026-10-08', started_ts: 2100, finished_ts: 2450 }),
      run({ run_id: 'overlap', scan: '2026-10-08', started_ts: 2400, finished_ts: 2700 }),
      run({ run_id: 'refailed', scan: '2026-10-08', started_ts: 2600, finished_ts: 2900 }),
      run({ run_id: 'older-scan', scan: '2026-10-06', started_ts: 2600, finished_ts: 2800 }),
      run({ run_id: 'reproc', scan: '2026-10-08', kind: 'reproc', started_ts: 2600, finished_ts: 2800 }),
      run({ run_id: 'meta', scan: '2026-10-08', store: 'meta', started_ts: 2600, finished_ts: 2800 }),
      run({ run_id: 'child', scan: '2026-10-08', parent: 'c', started_ts: 2600, finished_ts: 2800 }),
      run({ run_id: 'd', scan: '2026-10-09', started_ts: 4000, finished_ts: 4600 }),
    ]
    const ps: ScanRunPhase[] = [
      failed('digest'), failed('warm-cache', 1), failed('interval-store', 2),
      phase('before', 'digest', 0, 2300, 2400),
      phase('overlap', 'digest', 0, 2450, 2600),
      { ...phase('refailed', 'digest', 0, 2700, 2800), note: 'failed (exit 1)' },
      phase('older-scan', 'digest', 0, 2700, 2750), phase('reproc', 'digest', 0, 2700, 2750), phase('meta', 'digest', 0, 2700, 2750), phase('child', 'digest', 0, 2700, 2750),
      phase('c', 'digest', 0, 3400, 3500), phase('c', 'warm-cache', 1, 3500, 3550),
      { ...phase('c', 'interval-store', 2, 3100, 3300), note: 'skipped: no current generation' },
      phase('d', 'digest', 0, 4400, 4500), phase('d', 'interval-store', 1, 4100, 4300),
    ]
    expect(phaseIssues(RUNS[2], [RUNS[2], ...peers], ps)).toEqual([
      { phase: 'digest', outcome: 'failed', recovery: later },
      { phase: 'warm-cache', outcome: 'failed', recovery: null },
      { phase: 'interval-store', outcome: 'failed', recovery: peers[8] },
    ])
    expect(phaseIssues(RUNS[2], [RUNS[2], ...peers.slice(1, 8)], ps).map(i => [i.phase, i.recovery])).toEqual([
      ['digest', null], ['warm-cache', null], ['interval-store', null],
    ])
  })
  it('a check\'s failure carries the first later run of its kind whose same check then ran and passed, skipping nothing; no other step carries one, and a re-check of this scan is a recovery', () => {
    const retry = run({ run_id: 'retry', scan: RUNS[2].scan, kind: 'phase-recovery', parent: 'b', started_ts: 2800, finished_ts: 2900 })
    const peers = [
      later,
      run({ run_id: 'refailed', scan: '2026-10-08', started_ts: 2600, finished_ts: 2900 }),
      run({ run_id: 'behind', scan: '2026-10-08', started_ts: 2600, finished_ts: 2900 }),
      run({ run_id: 'skipping', scan: '2026-10-08', started_ts: 2600, finished_ts: 2900 }),
      run({ run_id: 'older-scan', scan: '2026-10-06', started_ts: 2600, finished_ts: 2800 }),
      run({ run_id: 'reproc', scan: '2026-10-08', kind: 'reproc', started_ts: 2600, finished_ts: 2800 }),
      run({ run_id: 'meta', scan: '2026-10-08', store: 'meta', started_ts: 2600, finished_ts: 2800 }),
      run({ run_id: 'd', scan: '2026-10-09', started_ts: 4000, finished_ts: 4600 }),
    ]
    const ps: ScanRunPhase[] = [
      { ...failed('probe'), note: 'failed (exit 1), 4 passed, 0 skipped, 2 failed' }, failed('healthcheck', 1), failed('warm-cache', 2),
      { ...phase('refailed', 'probe', 0, 2700, 2800), note: 'failed (exit 1)' },
      { ...phase('behind', 'probe', 0, 2700, 2850), note: 'search behind: the static name index covers scans through 2026-10-07' },
      { ...phase('skipping', 'probe', 0, 2700, 2860), note: '4 passed, 2 skipped (static-names skipped (busy): the lease is held), 0 failed' },
      phase('older-scan', 'probe', 0, 2700, 2750), phase('reproc', 'probe', 0, 2700, 2750), phase('meta', 'probe', 0, 2700, 2750),
      { ...phase('c', 'probe', 0, 3400, 3500), note: '6 passed, 0 skipped, 0 failed' }, phase('c', 'warm-cache', 1, 3500, 3550),
      phase('d', 'probe', 0, 4400, 4500), phase('d', 'healthcheck', 1, 4500, 4510),
      phase('retry', 'healthcheck', 0, 2800, 2900),
    ]
    expect([phaseIssues(RUNS[2], [RUNS[2], ...peers], ps), phaseIssues(RUNS[2], [RUNS[2], ...peers.slice(1, 7)], ps), phaseIssues(RUNS[2], [RUNS[2], ...peers, retry], ps)]).toEqual([
      [
        { phase: 'probe', outcome: 'failed', recovery: null, passing: later, detail: '4 passed, 0 skipped, 2 failed' },
        { phase: 'healthcheck', outcome: 'failed', recovery: null, passing: peers[7] },
        { phase: 'warm-cache', outcome: 'failed', recovery: null },
      ],
      [
        { phase: 'probe', outcome: 'failed', recovery: null, passing: null, detail: '4 passed, 0 skipped, 2 failed' },
        { phase: 'healthcheck', outcome: 'failed', recovery: null, passing: null },
        { phase: 'warm-cache', outcome: 'failed', recovery: null },
      ],
      [
        { phase: 'probe', outcome: 'failed', recovery: null, passing: later, detail: '4 passed, 0 skipped, 2 failed' },
        { phase: 'healthcheck', outcome: 'failed', recovery: retry, passing: null },
        { phase: 'warm-cache', outcome: 'failed', recovery: null },
      ],
    ])
    expect(phaseIssues(RUNS[2], [RUNS[2], ...peers], ps).map(i => Object.keys(i))).toEqual([
      ['phase', 'outcome', 'recovery', 'passing', 'detail'], ['phase', 'outcome', 'recovery', 'passing'], ['phase', 'outcome', 'recovery'],
    ])
  })
  it('a scan its store\'s append passed over is never recovered by a later run\'s append, and says so', () => {
    const over = 'passed over: the store\'s append order is past this scan, or its path sort isn\'t published; no later run appends it'
    const ps: ScanRunPhase[] = [
      { ...failed('static-names'), note: `failed (exit 3), ${over}` }, failed('interval-store', 1),
      { ...phase('c', 'static-names', 0, 3100, 3300), note: 'overlapped' }, phase('c', 'interval-store', 1, 3100, 3300),
    ]
    const issues = phaseIssues(RUNS[2], [RUNS[2], later], ps)
    expect(issues).toEqual([
      { phase: 'static-names', outcome: 'failed', recovery: null, detail: over },
      { phase: 'interval-store', outcome: 'failed', recovery: later },
    ])
    expect([issueState(issues[0]), issueNext(issues[0]), runOutcomeLabel(RUNS[2], issues)]).toEqual([
      'incomplete', 'Not retried: the store appends in scan order and is past this scan. A rebuild or backfill of the store adds it.', 'incomplete · static-names',
    ])
  })
  it('a skipped converging step is recovered the same way; a linked attempt of the run\'s own wins over a later run', () => {
    const skip = 'skipped (busy): the store\'s lease is held'
    const retry = run({ run_id: 'retry', scan: RUNS[2].scan, kind: 'phase-recovery', parent: 'b', started_ts: 2800, finished_ts: 2900 })
    const ps: ScanRunPhase[] = [
      { ...phase('b', 'static-names', 0, 2300, 2310), note: `overlapped, ${skip}` },
      { ...phase('c', 'static-names', 0, 3100, 3300), note: 'overlapped' },
      phase('retry', 'static-names', 0, 2800, 2900),
    ]
    expect([phaseIssues(RUNS[2], [RUNS[2], later], ps), phaseIssues(RUNS[2], [RUNS[2], later, retry], ps)]).toEqual([
      [{ phase: 'static-names', outcome: 'skipped', recovery: later, reason: skip }],
      [{ phase: 'static-names', outcome: 'skipped', recovery: retry, reason: skip }],
    ])
  })
  it('says what happened to an issue\'s phase, and where an open one goes from here', () => {
    const issues: PhaseIssue[] = [
      issue('warm-cache'), { ...issue('probe'), attempts: 2 }, { ...issue('static-names'), outcome: 'timed out' },
      { phase: 'static-names', outcome: 'skipped', recovery: null, reason: 'skipped (busy): the lease is held' },
      { phase: 'probe', outcome: 'search behind', recovery: null, reason: 'lag' }, issue('publish'),
    ]
    expect(issues.map(i => [issueWhat(i), issueNext(i)])).toEqual([
      ['failed', 'Not retried: no later run redoes it.'],
      ['failed 2x', 'A check: no later run redoes it for this scan, and no later run\'s probe has passed yet.'],
      ['timed out', 'A later run\'s static-names step does this run\'s work too; none has yet.'],
      ['skipped: the lease is held', 'A later run\'s static-names step does this run\'s work too; none has yet.'],
      ['search behind', 'Search catches up when the static name index appends this scan.'],
      ['failed', 'Not retried: no later run redoes it.'],
    ])
  })
  it('says what an open issue leaves short; nothing once its work is done or its check passes since, for search behind (its reason says it) or for an unknown phase', () => {
    expect([
      issueLeaves(issue('warm-cache')), issueLeaves(issue('static-names')), issueLeaves(issue('static-names', later)), issueLeaves(issue('publish')),
      issueLeaves({ phase: 'probe', outcome: 'search behind', recovery: null }),
      issueLeaves({ ...issue('probe'), passing: null }), issueLeaves({ ...issue('probe'), passing: later }),
    ]).toEqual([
      'cache not pre-warmed: this scan\'s first views load cold', 'search doesn\'t cover this scan', undefined, undefined, undefined,
      'the serving probe failed: some of this scan\'s views may not load', undefined,
    ])
  })
  it('the status filter\'s choices: `needs attention` keeps failed and incomplete runs, never a harmless partial nor a check passing since', () => {
    expect(STATUS_FILTERS.map(f => [f.key, f.label])).toEqual([
      ['attention', 'needs attention (failed, incomplete)'], ['succeeded', 'succeeded'], ['partial', 'partial (a side step failed)'],
      ['passing', 'passing since (a check failed, and has passed since)'],
      ['incomplete', 'incomplete'], ['behind', 'search behind'], ['recovered', 'recovered'], ['failed', 'failed'], ['running', 'running'], ['nop', 'no-op'],
    ])
    const outcomes = ['succeeded', 'partial', 'passing since', 'incomplete', 'search behind', 'recovered', 'failed', 'running', 'no-op']
    expect(['', 'attention', 'partial', 'passing', 'behind', 'nop', 'bogus'].map(k => outcomes.filter(o => statusMatches(k, o)))).toEqual([
      outcomes, ['incomplete', 'failed'], ['partial'], ['passing since'], ['search behind'], ['no-op'], [],
    ])
  })
})

describe('links', () => {
  it('meta tree: a prefix opens itself, an object or split part its directory; no base, no link', () => {
    expect([
      metaHref('https://h.test/meta', 'gs://data/listing/2026-10-08/'),
      metaHref('https://h.test/meta/', 'gs://data/listing/2026-10-08/index/g/path-index'),
      metaHref('https://h.test/meta', 'gs://data/top.json'),
      metaHref(null, 'gs://data/listing/'),
      metaHref('https://h.test/meta', 'not a uri'),
    ]).toEqual([
      'https://h.test/meta/data/listing/2026-10-08',
      'https://h.test/meta/data/listing/2026-10-08/index/g',
      'https://h.test/meta/data',
      null,
      null,
    ])
  })
  it('map, Batch and logs', () => {
    expect([mapHref('2026-10-08'), mapHref('2026-10-09T1236'), mapHref('nope')]).toEqual(['/?d=2610080000', '/?d=2610091236', null])
    expect(batchHref('p', 'us-central1', 'job-1')).toBe('https://console.cloud.google.com/batch/jobsDetail/regions/us-central1/jobs/job-1/details?project=p')
    expect(batchHref('p', 'us-central1', 'job-1', 'logs')).toBe('https://console.cloud.google.com/batch/jobsDetail/regions/us-central1/jobs/job-1/logs?project=p')
    expect(logsHref('p', { run_id: 'uid-1', started_ts: 1_800_000_000, job_name: 'job-1', region: 'us-central1' })).toBe(
      'https://console.cloud.google.com/batch/jobsDetail/regions/us-central1/jobs/job-1/logs?project=p')
    expect(logsHref('p', { run_id: 'uid-1', started_ts: 1_800_000_000, job_name: null, region: null })).toBe(
      'https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22uid-1%22%0Alog_id%28%22batch_task_logs%22%29%0Atimestamp%20%3E%3D%20%222027-01-15T07%3A50%3A00.000Z%22?project=p')
  })
})

describe('runTasks', () => {
  const task = (name: string, seq: number, start: number, end: number | null, job: string | null, machine: string | null, status: ScanRunPhase['status'] = 'done'): ScanRunPhase =>
    ({ ...phase('nf', name, seq, start, end ?? 0), finished_ts: end, status, job_name: job, machine })
  it('groups a Nextflow run\'s phases by their Batch job, in start order', () => {
    expect(runTasks([
      task('import b1', 3, 700, 800, 'nf-b1', 'n2-standard-16'),
      task('list b1', 0, 120, 700, 'nf-b1', 'n2-standard-16'),
      task('bucket-check', 1, 130, 135, 'nf-bc', 'e2-standard-2'),
      task('index', 4, 900, null, 'nf-ix', 'n2-standard-8', 'running'),
      task('head-only', 2, 100, 110, null, null),
    ])).toEqual([
      { job_name: 'nf-b1', machine: 'n2-standard-16', phases: ['list b1', 'import b1'], start: 120, end: 800, status: 'done' },
      { job_name: 'nf-bc', machine: 'e2-standard-2', phases: ['bucket-check'], start: 130, end: 135, status: 'done' },
      { job_name: 'nf-ix', machine: 'n2-standard-8', phases: ['index'], start: 900, end: null, status: 'running' },
    ])
  })
  it('is empty for a one-VM run', () => {
    expect(runTasks([phase('u1', 'listing', 0, 0, 10)])).toEqual([])
  })
})

describe('taskLogsHref', () => {
  it('queries the task job\'s logs by name, which outlive the deleted job', () => {
    const q = 'log_id("batch_task_logs")\nlabels.task_group_name:"/jobs/nf-e8646fb0-1792179471443/"\ntimestamp >= "2027-01-15T07:50:00.000Z"'
    expect(taskLogsHref('p', 'nf-e8646fb0-1792179471443', 1_800_000_000)).toBe(
      `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(q).replace(/\(/g, '%28').replace(/\)/g, '%29')}?project=p`,
    )
  })
})

describe('maintenance runs (compactions, merges: `dt_cloud.maint_runs`)', () => {
  const scanRun = run({ run_id: 'scan-1', scan: '2026-10-20T0700', kind: 'scan', started_ts: 1000, finished_ts: 2000 })
  const compact = run({
    run_id: 'iv-1', scan: '2026-10-20T0700', kind: 'compact', status: 'running', started_ts: 3000, finished_ts: null,
    gen: 'interval-store/2026-10-09b.g2', source: 'job', updated_ts: 9500, machine: 'n2-highmem-32',
  })
  const merged = run({ run_id: 'sn-1', scan: '2026-10-20T0700', kind: 'merge', status: 'nop', started_ts: 2500, finished_ts: 2600, gen: 'static-names/2026-10-08c' })
  const phases: ScanRunPhase[] = [
    { ...phase('iv-1', 'read', 0, 3000, 3010), note: '57 files, 12.00 GiB from oa-gcs-usage-dvx (mount) (in place)' },
    { ...phase('iv-1', 'due', 1, 3010, 3011), note: 'level 5: two L4 runs (2 runs: L4 L4)' },
    { ...phase('iv-1', 'fold', 2, 3011, 7000), note: '64 ranges, 1,234 rows' },
    { ...phase('iv-1', 'sorts', 3, 7000, 9400), status: 'running', finished_ts: null as unknown as number, note: '12/64 ranges' },
  ]

  it('rows of their own beside the scan (never folded into it), with their kinds in the filter', () => {
    const rows = summarize([scanRun, compact, merged], phases, [], 10_000)
    expect(rows.map(r => [r.run.run_id, r.run.kind, r.secs, r.spans.map(s => [s.phase, s.status, s.end - s.start])])).toEqual([
      ['iv-1', 'compact', 7000, [['read', 'done', 10], ['due', 'done', 1], ['fold', 'done', 3989], ['sorts', 'running', 3000]]],
      ['sn-1', 'merge', 100, []],
      ['scan-1', 'scan', 1000, []],
    ])
    expect([...new Set(rows.map(r => r.run.kind))].sort()).toEqual(['compact', 'merge', 'scan'])
  })

  it('a store per run (its `gen`\'s first segment); none for a scan', () => {
    expect([compact, merged, scanRun].map(r => [isMaintenance(r.kind), maintStore(r)])).toEqual([
      [true, 'interval-store'], [true, 'static-names'], [false, null],
    ])
  })

  it('a running one shows its running phase, elapsed time and live note, and goes quiet without a heartbeat', () => {
    const spans = phaseSpans(compact, phases, 10_000)
    expect(liveness(compact, spans, 9560)).toEqual({ phase: 'sorts', elapsed: 3000, note: '12/64 ranges', quiet: null, lost: false })
    expect(liveness(compact, spans, 9500 + QUIET_S)).toEqual({ phase: 'sorts', elapsed: 3000, note: '12/64 ranges', quiet: null, lost: false })
    expect(liveness(compact, spans, 9500 + QUIET_S + 1)).toEqual({ phase: 'sorts', elapsed: 3000, note: '12/64 ranges', quiet: QUIET_S + 1, lost: false })
    expect(liveness(merged, [], 10_000)).toBeNull()
  })

  it('quiet after five missed 60 s heartbeats, lost after thirty', () => {
    expect([QUIET_S, LOST_S]).toEqual([300, 1800])
  })

  it('a running phase whose live note mentions a failure is still running; a closed one is failed by it', () => {
    const note = '41m · merge: drill/a · 3 shards, failed to stat x (2m ago)'
    expect([phaseStatus({ status: 'running', note }), phaseStatus({ status: 'done', note }), phaseStatus({ status: 'done', note: 'failed (exit 1)' })])
      .toEqual(['running', 'failed', 'failed'])
  })

  it('lost past `LOST_S` without a write: its clock stops at its last write, so it never stretches the duration chart', () => {
    const live = 9500 + LOST_S, lost = live + 1, days = 9500 + 4 * 86400
    expect([live, lost, days].map(now => {
      const [row] = summarize([compact], phases, [], now)
      return [row.secs, row.spans.map(s => [s.phase, s.end - s.start]).at(-1), liveness(compact, row.spans, now)]
    })).toEqual([
      [live - 3000, ['sorts', live - 7000], { phase: 'sorts', elapsed: live - 7000, note: '12/64 ranges', quiet: LOST_S, lost: false }],
      [9500 - 3000, ['sorts', 9500 - 7000], { phase: 'sorts', elapsed: 9500 - 7000, note: '12/64 ranges', quiet: LOST_S + 1, lost: true }],
      [9500 - 3000, ['sorts', 9500 - 7000], { phase: 'sorts', elapsed: 9500 - 7000, note: '12/64 ranges', quiet: days - 9500, lost: true }],
    ])
    // a finished run keeps its own end, however old its last write
    expect(summarize([{ ...compact, status: 'failed', finished_ts: 9600 }], phases, [], days).map(r => r.secs)).toEqual([6600])
  })

  it('is never the run behind a scan (the map\'s "scanned … ago" link)', () => {
    const rows = summarize([scanRun, compact, merged], phases, [], 10_000)
    expect(scanRunOf(rows, '2026-10-20T0700')?.run_id).toBe('scan-1')
    expect(scanRunOf(summarize([compact], phases, [], 10_000), '2026-10-20T0700')).toBeUndefined()
  })
})
