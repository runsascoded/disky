import { describe, expect, it } from 'vitest'
import { type DeletionRun, reusableDryRun } from '../../src/runs.js'
import { realRun, type RunRow } from './plans.js'
import { reusableRunDir } from './sweepDispatch.js'

const CFG = { dataBucket: 'data' }
const SCAN = '2026-09-27'
const dryDir = (stamp: string) => `gs://data/sweep/runs/gcs-sweep-dry-${stamp}z`
const GOOD = dryDir('20260928-120000')
const run = (o: Partial<Omit<DeletionRun, 'log_dir'>> & { log_dir?: string | null }): DeletionRun => ({
  run_id: 'r', plan_id: 3, mode: 'dry', scan: SCAN, actor: 'ann', started_ts: 100, finished_ts: 200, deleted_bytes: 0, deleted_objects: 0, skipped_gone: 0,
  undo_deadline: null, undo_state: 'none', plan_digest: 'D', log_dir: GOOD, ...o,
} as DeletionRun)

/** Dispatch's decision (`realRun`, then `launch`'s check of the dir), and the confirm's, over the same rows. */
function decisions(runs: DeletionRun[]) {
  const server = realRun(runs as unknown as RunRow[], 'D', 1, SCAN)
  if (!server.ok) throw new Error(server.reason)
  return { dispatch: reusableRunDir(CFG, server.reviewed) ?? null, confirm: reusableDryRun(runs, 3, 'D', SCAN)?.log_dir ?? null }
}

describe('the real-delete confirm and dispatch make one statement', () => {
  it.each([
    ['a dry run\'s own dir', GOOD, GOOD],
    ['no dir', null, null],
    ['an empty dir', '', null],
    ['outside the runs dir', 'gs://data/elsewhere', null],
    ['a CLI run\'s plan dir', 'gs://data/sweep/2026-09-27-p3/', null],
    ['a real run\'s dir', 'gs://data/sweep/runs/gcs-sweep-real-20260928-120000z', null],
    ['a short stamp', 'gs://data/sweep/runs/gcs-sweep-dry-20260928-1200z', null],
    ['an upper-case stamp', 'gs://data/sweep/runs/gcs-sweep-dry-20260928-120000Z', null],
    ['a trailing slash', `${GOOD}/`, null],
    ['a dir under the run\'s', `${GOOD}/source`, null],
  ])('%s', (_, log_dir, reused) => {
    expect(decisions([run({ log_dir })])).toEqual({ dispatch: reused, confirm: reused })
  })

  it('the newest matching run decides: an unusable newest isn\'t passed over for an older one', () => {
    const older = run({ run_id: 'a', started_ts: 100, log_dir: dryDir('20260928-110000') })
    const newest = run({ run_id: 'b', started_ts: 200, log_dir: 'gs://data/sweep/runs/not-a-dry-run' })
    const unrelated = [
      run({ run_id: 'c', started_ts: 300, scan: 'another scan' }),
      run({ run_id: 'd', started_ts: 400, plan_digest: 'E' }),
      run({ run_id: 'e', started_ts: 500, plan_digest: '' }),
      run({ run_id: 'f', started_ts: 600, mode: 'real' }),
    ]
    expect(decisions([older, newest, ...unrelated])).toEqual({ dispatch: null, confirm: null })
    expect(decisions([older, ...unrelated])).toEqual({ dispatch: older.log_dir, confirm: older.log_dir })
  })

  it('the one row they read apart: the page doesn\'t know the deployment\'s data bucket, so it can\'t refuse another\'s', () => {
    const elsewhere = 'gs://other/sweep/runs/gcs-sweep-dry-20260928-120000z'
    expect(decisions([run({ log_dir: elsewhere })])).toEqual({ dispatch: null, confirm: elsewhere })
  })
})
