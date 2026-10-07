import { describe, expect, it } from 'vitest'
import {
  commonBucketPrefix, type DeletionRun, EXEC_CAPS, elapsed, elapsedDetails, type ExecJob, fmtDur, joinRuns, loggedOf, plannedOf, runControls,
  progressEstimate, type ProgressFile, runDirHref, runFileHref, runFilesRel, runJobId, runLabel, sumProgress, verifyRun, viewBuckets, viewState,
} from './runs'

const GCS_JOB = 'gcs-sweep-real-20260928-120000z'
const GCS_RUN = '2026-09-27-p3/20260928T121500Z'
const run = (o: Partial<DeletionRun> = {}): DeletionRun => ({
  run_id: GCS_RUN, plan_id: 3, mode: 'real', scan: '2026-09-27', actor: 'ann', started_ts: 1000, finished_ts: 5000,
  deleted_bytes: 600, deleted_objects: 6, skipped_gone: 2, skipped_overwritten: 1, drift_dirs: 0, ledger_drift_dirs: 0,
  undo_deadline: 9000, undo_state: 'none', purge_state: 'none', log_dir: `gs://data/sweep/runs/${GCS_JOB}`, buckets: 'b1', ...o,
})
const job = (o: Partial<ExecJob> & { job_id: string }): ExecJob => ({ op: 'sweep', state: 'SUCCEEDED', created: '2026-09-28T12:00:00Z', ...o })

describe('runJobId — the Batch job behind a run', () => {
  it('gcs: the log dir\'s job; cw: the run id itself; a CLI run: its run id', () => {
    expect([
      runJobId(run()),
      runJobId({ run_id: 'cw-sweep-dry-20260928-120000z', log_dir: 'gs://data/sweep/cw/runs/cw-sweep-dry-20260928-120000z' }),
      runJobId({ run_id: GCS_RUN, log_dir: 'gs://data/sweep/2026-09-27-p3/' }),
      runJobId({ run_id: GCS_RUN }),
    ]).toEqual([GCS_JOB, 'cw-sweep-dry-20260928-120000z', GCS_RUN, GCS_RUN])
  })
})

describe('joinRuns — one row per run (+ its job and ops), then every unrecorded run job', () => {
  it('joins by job id, attaches undo ops, then the unrecorded run jobs (any plan, or none), newest first', () => {
    const older = run({ run_id: '2026-09-26-p3/20260926T000000Z', started_ts: 500, log_dir: 'gs://data/sweep/runs/gcs-sweep-dry-20260926-000000z', mode: 'dry' })
    const jobs = [
      job({ job_id: GCS_JOB, state: 'SUCCEEDED', plan_id: 3 }),
      job({ job_id: 'gcs-undo-20260929-000000z', op: 'undo', target: GCS_RUN, state: 'RUNNING' }),
      job({ job_id: 'gcs-sweep-dry-20260930-000000z', state: 'QUEUED', plan_id: 3, created: '2026-09-30T00:00:00Z' }),
      job({ job_id: 'gcs-sweep-dry-20260930-010000z', state: 'QUEUED', plan_id: 4, created: '2026-09-30T01:00:00Z' }),
      job({ job_id: 'gcs-sweep-dry-20250101-000000z', state: 'SUCCEEDED', plan_id: null, created: '2025-01-01T00:00:00Z' }),
    ]
    const views = joinRuns([older, run()], jobs)
    expect(views.map(v => [v.key, v.job?.job_id ?? null, v.ops.map(o => o.job_id)])).toEqual([
      ['gcs-sweep-dry-20260930-010000z', 'gcs-sweep-dry-20260930-010000z', []],
      ['gcs-sweep-dry-20260930-000000z', 'gcs-sweep-dry-20260930-000000z', []],
      ['gcs-sweep-dry-20250101-000000z', 'gcs-sweep-dry-20250101-000000z', []],
      [GCS_RUN, GCS_JOB, ['gcs-undo-20260929-000000z']],
      ['2026-09-26-p3/20260926T000000Z', null, []],
    ])
    expect(views.map(viewState)).toEqual(['QUEUED', 'QUEUED', 'SUCCEEDED', 'SUCCEEDED', 'DONE'])
  })
})

describe('elapsed / fmtDur / buckets / paths', () => {
  it('elapsed tooltips distinguish recorded executor times from Batch running time', () => {
    expect(elapsedDetails({ key: 'a', run: run(), job: job({ job_id: GCS_JOB, created: '1970-01-01T00:10:00Z', run_secs: 61 }), ops: [] })).toEqual([
      'Dispatched: 1970-01-01 00:10:00 UTC',
      'Executor started: 1970-01-01 00:16:40 UTC',
      'Executor finished: 1970-01-01 01:23:20 UTC',
      'Elapsed is Batch running time, excluding queueing; executor timestamps cover its recorded work.',
    ])
    expect(elapsedDetails({ key: 'a', run: run({ finished_ts: null }), job: job({ job_id: GCS_JOB, state: 'RUNNING', created: undefined }), ops: [] })).toEqual([
      'Executor started: 1970-01-01 00:16:40 UTC',
      'Executor still running',
    ])
    expect(elapsedDetails({ key: 'a', job: job({ job_id: GCS_JOB, created: undefined }), ops: [] })).toEqual(['Executor start/end not recorded'])
  })
  it('elapsed: Batch\'s run time, else the recorded span, else (live) since start', () => {
    expect([
      elapsed({ key: 'a', run: run(), job: job({ job_id: GCS_JOB, run_secs: 61 }), ops: [] }, 9999),
      elapsed({ key: 'a', run: run(), ops: [] }, 9999),
      elapsed({ key: 'a', run: run({ finished_ts: null }), job: job({ job_id: GCS_JOB, state: 'RUNNING' }), ops: [] }, 1600),
      elapsed({ key: 'a', run: run({ finished_ts: null }), ops: [] }, 1600),
    ]).toEqual([61, 4000, 600, null])
  })
  it('fmtDur', () => {
    expect([fmtDur(59), fmtDur(125), fmtDur(3 * 3600 + 5 * 60), fmtDur(50 * 3600)]).toEqual(['0m', '2m', '3h 05m', '2d 2h'])
  })
  it('runLabel: both run-id families at minute precision, else a readable fallback', () => {
    expect([
      runLabel('2026-10-03-p1/20261004T034042Z'),
      runLabel('gcs-sweep-dry-20261004-020914z'),
      runLabel('cw-sweep-real-custom'),
    ]).toEqual(['2026-10-04 03:40Z', '2026-10-04 02:09Z', 'custom'])
  })
  it('viewBuckets: the recorded cut, else the job\'s', () => {
    expect([
      viewBuckets({ key: 'a', run: run({ buckets: 'b2,b1' }), ops: [] }),
      viewBuckets({ key: 'a', run: run({ buckets: null }), job: job({ job_id: GCS_JOB, buckets: ['b3'] }), ops: [] }),
      viewBuckets({ key: 'a', run: run({ buckets: null }), ops: [] }),
    ]).toEqual([['b2', 'b1'], ['b3'], []])
  })
  it('commonBucketPrefix: a shared `<word>-` only', () => {
    expect([
      commonBucketPrefix(['fleet-us-east1', 'fleet-eu-west4', 'fleet-us-central2']),
      commonBucketPrefix(['fleet-us-east1', 'fleet-us-east5']),
      commonBucketPrefix(['alpha', 'beta']),
      commonBucketPrefix(['only-one']),
    ]).toEqual(['fleet-', 'fleet-us-', '', ''])
  })
  it('runFilesRel: the run dir below its bucket, slash-terminated', () => {
    expect([runFilesRel(`gs://data/sweep/runs/${GCS_JOB}`), runFilesRel('gs://data/sweep/x/')]).toEqual([`sweep/runs/${GCS_JOB}/`, 'sweep/x/'])
  })
  it('run artifact links use the files proxy, not the retired SPA route', () => {
    expect([
      runFileHref('sweep/runs/a b/plan.json'),
      runDirHref('sweep/runs/a b/would-delete/'),
    ]).toEqual([
      '/v1/files/get?path=sweep%2Fruns%2Fa%20b%2Fplan.json',
      '/v1/files/list?prefix=sweep%2Fruns%2Fa%20b%2Fwould-delete%2F',
    ])
  })
})

describe('run-dir files: planned, progress, logged', () => {
  it('rough ETA counts decisions, excludes finished bucket rates and rejects stale/empty samples', () => {
    const ps: ProgressFile[] = [
      { roots: 2, roots_done: 2, decisions: { delete: 100, skipped_gone: 20 }, delete_bytes: 1000, started: '2026-09-28T12:00:00Z', updated: '2026-09-28T12:00:10Z', done: true },
      { roots: 10, roots_done: 0, decisions: { delete: 40, skipped_overwritten: 10 }, delete_bytes: 500, started: '2026-09-28T12:01:00Z', updated: '2026-09-28T12:01:10Z', done: false },
    ]
    const now = Date.parse('2026-09-28T12:01:10Z') / 1000
    expect([
      progressEstimate(ps, 1000, now),
      progressEstimate(ps, 1000, now + 91),
      progressEstimate(ps, undefined, now),
      progressEstimate([], 1000, now),
      progressEstimate(ps, 100, now),
    ]).toEqual([
      { decided: 170, percent: 17, secondsLeft: 166, stale: false },
      { decided: 170, percent: 17, secondsLeft: null, stale: true },
      { decided: 170, percent: null, secondsLeft: null, stale: false },
      { decided: 0, percent: 0, secondsLeft: null, stale: false },
      { decided: 170, percent: 100, secondsLeft: null, stale: false },
    ])
  })
  it('plannedOf: the total, else the buckets\' sum', () => {
    expect([
      plannedOf({ buckets: {}, total: { eligible: { bytes: 10, objects: 2 } } }),
      plannedOf({ buckets: { b1: { eligible: { bytes: 10, objects: 2 } }, b2: { eligible: { bytes: 5, objects: 1 } }, b3: {} } }),
    ]).toEqual([{ bytes: 10, objects: 2 }, { bytes: 15, objects: 3 }])
  })
  it('sumProgress: totals over buckets; the rate counts only buckets still running', () => {
    expect(sumProgress([
      { roots: 4, roots_done: 4, decisions: { delete: 100, skipped_gone: 3 }, delete_bytes: 1000, started: '2026-09-28T12:00:00Z', updated: '2026-09-28T12:00:10Z', done: true },
      { roots: 10, roots_done: 2, decisions: { delete: 50 }, delete_bytes: 500, started: '2026-09-28T12:01:00Z', updated: '2026-09-28T12:01:10Z', done: false },
    ])).toEqual({ deletes: 150, gone: 3, bytes: 1500, roots: 14, roots_done: 6, rate: 5 })
  })
  it('loggedOf: decisions, bytes, drifted / failed dirs and stopped roots over buckets', () => {
    expect(loggedOf({ for_real: true, buckets: {
      b1: { decisions: { delete: 4, skipped_gone: 1 }, delete_bytes: 400, drift_dirs: [{}], failed_dirs: [], soft_delete_days: 7 },
      b2: { decisions: { delete: 2, skipped_overwritten: 1 }, delete_bytes: 200, interrupted: { roots_skipped: 3, roots: 9 } },
    } })).toEqual({ decisions: { delete: 6, skipped_gone: 1, skipped_overwritten: 1 }, bytes: 600, driftDirs: 1, failedDirs: 0, rootsSkipped: 3 })
  })
})

describe('verifyRun — log against D1, planned against decided', () => {
  const logged = { decisions: { delete: 6, skipped_gone: 2, skipped_overwritten: 1 }, bytes: 600, driftDirs: 0, failedDirs: 0, rootsSkipped: 0 }
  it('all agree', () => {
    expect(verifyRun(run(), { bytes: 900, objects: 9 }, logged).map(c => [c.label, c.ok])).toEqual([
      ['deleted objects: log = D1', true],
      ['deleted bytes: log = D1', true],
      ['gone: log = D1', true],
      ['overwritten: log = D1', true],
      ['drifted dirs: log = D1', true],
      ['planned objects = decided', true],
    ])
  })
  it('a lost record and a drift gap show their numbers; no log = no checks', () => {
    expect(verifyRun(run({ deleted_objects: 5 }), { bytes: 900, objects: 12 }, logged).filter(c => !c.ok)).toEqual([
      { label: 'deleted objects: log = D1', expected: 6, actual: 5, ok: false },
      { label: 'planned objects = decided', expected: 12, actual: 9, ok: false },
    ])
    expect(verifyRun(run(), { bytes: 900, objects: 9 }, null)).toEqual([])
  })
})

describe('runControls — per executor capabilities', () => {
  const view = (r: DeletionRun, j?: Partial<ExecJob>, ops: ExecJob[] = []) => ({ key: r.run_id, run: r, ...(j ? { job: job({ job_id: GCS_JOB, ...j }) } : {}), ops })
  const gcs = EXEC_CAPS.sweep
  const cw = EXEC_CAPS['plan-sweep']
  it('a live run: stop (admins only)', () => {
    expect([
      runControls(view(run({ finished_ts: null }), { state: 'RUNNING' }), gcs, true, 2000),
      runControls(view(run({ finished_ts: null }), { state: 'RUNNING' }), gcs, false, 2000),
    ]).toEqual([
      { stop: true, undo: false, purge: false, undoing: false },
      { stop: false, undo: false, purge: false, undoing: false },
    ])
  })
  it('undo inside the window; not once it closed, once undone, while an undo runs, or for a dry run', () => {
    const undoJob = job({ job_id: 'gcs-undo-x', op: 'undo', target: GCS_RUN, state: 'RUNNING' })
    expect([
      runControls(view(run()), gcs, true, 8999).undo,
      runControls(view(run()), gcs, true, 9000).undo,
      runControls(view(run({ undo_state: 'full' })), gcs, true, 2000).undo,
      runControls(view(run(), undefined, [undoJob]), gcs, true, 2000),
      runControls(view(run({ mode: 'dry' })), gcs, true, 2000).undo,
    ]).toEqual([true, false, false, { stop: false, undo: false, purge: false, undoing: true }, false])
  })
  it('no recorded window: cw offers undo, gcs doesn\'t; purge only where the executor has it, after the window', () => {
    expect([
      runControls(view(run({ undo_deadline: null })), cw, true, 2000).undo,
      runControls(view(run({ undo_deadline: null })), gcs, true, 2000).undo,
      runControls(view(run({ purge_state: 'pending' })), cw, true, 9000).purge,
      runControls(view(run({ purge_state: 'pending' })), cw, true, 8999).purge,
      runControls(view(run({ purge_state: 'pending' })), gcs, true, 9000).purge,
    ]).toEqual([true, false, true, false, false])
  })
})
