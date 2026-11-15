// `scanDeletions`: which recorded bands a scan's sizes are out of date by.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { withoutDeleted } from '../../src/stagedScan.js'
import type { BatchConfig } from './batchConfig.js'
import { scanDeletions } from './plans.js'
import { reflectRuns } from './runReflect.js'
import { runDir } from './sweepDispatch.js'
import { reflectSweepRuns, type SweepBatchJob } from './sweepReflect.js'
import { RUN_DIR, sqliteD1 } from './testD1.js'

const SCAN = '2026-11-04T0703', NEXT = '2026-11-05T0702'
/** What a run that isn't counted leaves of its band: the prefix, and no amounts. */
const uncounted = (prefix: string) => ({ prefix, bytes: 0, objects: 0, gone: 0 })

async function bandsDb() {
  const { db, raw } = await sqliteD1()
  raw.exec("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)")
  const run = raw.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, plan_digest, log_dir, plan_id) VALUES (?, 'p', ?, 0, 0, 'ann', ?, 100, ?, ?, 'd', 1)`)
  const band = raw.prepare('INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, undone_objects) VALUES (?, ?, 50, 5, 1, ?)')
  const add = (id: string, scan: string, mode: string, finished: number | null, digest: string | null, undone = 0) => {
    run.run(id, scan, mode, finished, digest); band.run(id, `gs://b/${id}/`, undone)
  }
  add('done', SCAN, 'real', 200, 'D')
  add('undone', SCAN, 'real', 200, 'D', 1)
  add('dry', SCAN, 'dry', 200, 'D')
  add('running', SCAN, 'real', null, 'D')
  add('next', NEXT, 'real', 200, 'D')
  add('killed', SCAN, 'real', 200, '')
  add('by-hand', SCAN, 'real', 200, null)
  return { db, raw }
}

describe('scanDeletions', () => {
  it('counts a real run on exactly that scan that ended with a result, its band not undone; any other real run there: its prefixes, no amounts', async () => {
    expect(await scanDeletions((await bandsDb()).db, SCAN)).toEqual([
      uncounted('gs://b/by-hand/'),
      { prefix: 'gs://b/done/', bytes: 50, objects: 5, gone: 1 },
      uncounted('gs://b/killed/'),
      uncounted('gs://b/running/'),
    ])
  })
  it('a run on an earlier scan says nothing about a later one: what is there now may be written back', async () => {
    expect(await scanDeletions((await bandsDb()).db, '2026-11-06T0701')).toEqual([])
  })

  describe('what each kind of run takes off an item every one of whose objects it recorded', () => {
    const stat = { bytes: 50, objects: 6 }
    const left = async (id: string) => withoutDeleted({ [`gs://b/${id}/`]: stat }, await scanDeletions((await bandsDb()).db, SCAN))
    it('a clean run: all of it, so the item is empty at the scan', async () => {
      expect(await left('done')).toEqual({})
    })
    it('a killed run (the empty digest): nothing', async () => {
      expect(await left('killed')).toEqual({ 'gs://b/killed/': stat })
    })
    it('a run by hand, which nothing reflects (no digest): nothing', async () => {
      expect(await left('by-hand')).toEqual({ 'gs://b/by-hand/': stat })
    })
    it('an undone run: nothing', async () => {
      expect(await left('undone')).toEqual({ 'gs://b/undone/': stat })
    })
    it('a dry run, a run still going: nothing', async () => {
      expect([await left('dry'), await left('running')]).toEqual([{ 'gs://b/dry/': stat }, { 'gs://b/running/': stat }])
    })
  })

  describe('beside a run that is not counted, a counted one', () => {
    const P = 'gs://b/x/', stat = { bytes: 100, objects: 10, owners: [['ann', 100]] }
    async function resumed(rest: { bytes: number; objects: number; gone: number }) {
      const { db, raw } = await bandsDb()
      const run = raw.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, plan_digest, log_dir, plan_id) VALUES (?, 'p', ?, 0, 0, 'ann', 'real', 100, 200, ?, 'd', 1)`)
      const band = raw.prepare('INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone) VALUES (?, ?, ?, ?, ?)')
      run.run('stopped', SCAN, ''); band.run('stopped', P, 40, 4, 0)
      run.run('resume', SCAN, 'D'); band.run('resume', P, rest.bytes, rest.objects, rest.gone)
      return withoutDeleted({ [P]: stat }, await scanDeletions(db, SCAN))
    }
    it('that deleted only the rest (a resume, which subtracts what the stopped run settled): no remainder is stated, the item stays whole', async () => {
      expect(await resumed({ bytes: 60, objects: 6, gone: 0 })).toEqual({ [P]: stat })
    })
    it('that saw every object deleted or gone: the item is empty', async () => {
      expect(await resumed({ bytes: 60, objects: 6, gone: 4 })).toEqual({})
    })
  })

  describe('a run as its reflection leaves it', () => {
    const CFG = { dataBucket: 'my-data' }
    const P = 'gs://b/x/', stat = { bytes: 50, objects: 5 }
    const job = (id: string, state: string): SweepBatchJob => ({
      name: `projects/p/locations/us-central1/jobs/${id}`, uid: id, createTime: '2026-11-04T12:00:00Z', updateTime: '2026-11-04T12:30:00Z', region: 'us-central1',
      status: { state }, taskGroups: [{ taskSpec: { environment: { variables: { SWEEP_DATE: SCAN, PLAN_DIGEST: 'D', PLAN_ID: '1' } } } }],
    })
    const NOW = Date.parse('2026-11-04T13:00:00Z') / 1000
    afterEach(() => vi.unstubAllGlobals())

    /** The executor's own records (`sweep_exec.record_run`: the finished row and its bands, written before it exits), then
     *  what Batch says of the job. */
    async function recorded(state: string | null) {
      const { db, raw } = await sqliteD1()
      const id = 'gcs-sweep-real-20261104-120000z'
      raw.exec("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)")
      await db.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, log_dir, plan_id) VALUES ('r', ?, ?, 0, 0, 'ann', 'real', 100, 500, ?, 1)`)
        .bind(runDir(CFG, id), SCAN, runDir(CFG, id)).run()
      raw.exec(`INSERT INTO deletion_bands (run_id, prefix, bytes, objects) VALUES ('r', '${P}', 50, 5)`)
      if (state) await reflectSweepRuns(CFG, db, [job(id, state)], NOW, undefined, RUN_DIR)
      const bands = await scanDeletions(db, SCAN)
      return { digest: raw.prepare('SELECT plan_digest FROM deletion_runs').all(), bands, left: withoutDeleted({ [P]: stat }, bands) }
    }
    it('the job succeeded: counted, and the item it deleted all of is empty', async () => {
      expect(await recorded('SUCCEEDED')).toEqual({ digest: [{ plan_digest: 'D' }], bands: [{ prefix: P, bytes: 50, objects: 5, gone: 0 }], left: {} })
    })
    // `sweep execute*` exits 2 after recording a run with an unanswered delete (`failed_dirs`) or a bucket's error, 130 after a stop.
    it.each(['FAILED', 'CANCELLED'])('the job ended %s (a failed directory, a bucket error, a stop, a kill after its record): not counted, the item stays', async state => {
      expect(await recorded(state)).toEqual({ digest: [{ plan_digest: '' }], bands: [uncounted(P)], left: { [P]: stat } })
    })
    it('not reflected yet: not counted', async () => {
      expect(await recorded(null)).toEqual({ digest: [{ plan_digest: null }], bands: [uncounted(P)], left: { [P]: stat } })
    })
    it('killed before it recorded its end: the row closes with the empty digest and no band', async () => {
      const { db, raw } = await sqliteD1()
      const id = 'gcs-sweep-real-20261104-120000z'
      raw.exec("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)")
      await db.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, log_dir, plan_id) VALUES ('r', ?, ?, 0, 0, 'ann', 'real', 100, ?, 1)`)
        .bind(runDir(CFG, id), SCAN, runDir(CFG, id)).run()
      await reflectSweepRuns(CFG, db, [job(id, 'FAILED')], NOW, undefined, RUN_DIR)
      expect([raw.prepare('SELECT finished_ts, plan_digest FROM deletion_runs').all(), await scanDeletions(db, SCAN)]).toEqual([
        [{ finished_ts: Date.parse('2026-11-04T12:30:00Z') / 1000, plan_digest: '' }], [],
      ])
    })

    /** cw's executor: the site copies the run's summary, bands and all, when Batch says the job ended. */
    async function summarized(state: string, failures: number) {
      const { db, raw } = await sqliteD1()
      raw.exec(`INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1);
        INSERT INTO deletion_runs (run_id, plan_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, log_dir, plan_digest)
        VALUES ('cw-sweep-real-test', 1, 'manifest', '${SCAN}', 0, 0, 'ann', 'real', 100, 'logs', 'D')`)
      vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
        mode: 'real', bucket: 'b', deleted_objects: 5, deleted_bytes: 50, skipped_gone: 0, skipped_overwritten: 0, drift_new: 0, delete_failed: failures, finished_ts: 200,
        bands: [{ prefix: 'x/', bytes: 50, objects: 5, gone: 0, overwritten: 0, drift_new: 0 }],
      }))))
      await reflectRuns({ dataBucket: 'data' } as BatchConfig, db, 'token', [{ name: 'projects/p/locations/r/jobs/cw-sweep-real-test', uid: 'uid', createTime: '2026-11-04T12:00:00Z', status: { state } }], 'b')
      return scanDeletions(db, SCAN)
    }
    it('a summarized run that succeeded with every delete answered: counted', async () => {
      expect(await summarized('SUCCEEDED', 0)).toEqual([{ prefix: 's3://b/x/', bytes: 50, objects: 5, gone: 0 }])
    })
    it('a summarized run whose job succeeded over a failed delete, or failed: not counted', async () => {
      expect([await summarized('SUCCEEDED', 1), await summarized('FAILED', 0)]).toEqual([[uncounted('s3://b/x/')], [uncounted('s3://b/x/')]])
    })
  })
})
