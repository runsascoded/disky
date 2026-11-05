// What the server knows of the buckets' soft delete now: the job's newest reading (`snapshots/soft-delete/<date>.json`); and
// what that says of a run's recorded undo: unavailable, one it can't vouch for, or neither.
import { describe, expect, it } from 'vitest'
import { bucketSoftDelete, runUndoNow } from './softDeleteNow'
import { RUN_DIR, sqliteD1 } from './testD1'
import { fakeR2, softDeleteDoc } from './testR2'

const KEY = (date: string) => `snapshots/soft-delete/${date}.json`
const env = (files: Record<string, string>, broken: string[] = []) => ({ SERVED_R2: '1', INDEX_R2: fakeR2(files, broken) }) as never

describe('bucketSoftDelete — the job\'s newest reading', () => {
  it('reads the newest day\'s document, past a listing page, and nothing else under the prefix', async () => {
    expect(await bucketSoftDelete(env({
      [KEY('2026-11-03')]: softDeleteDoc('2026-11-03', { a: false, b: false }),
      [KEY('2026-11-04')]: softDeleteDoc('2026-11-04', { a: false, b: false }),
      [KEY('2026-11-05')]: softDeleteDoc('2026-11-05', { a: true, b: false }),
      'snapshots/soft-delete/notes.txt': 'x',
      'snapshots/soft-delete/old/2026-11-09.json': softDeleteDoc('2026-11-09', { a: false }),
      'snapshots/cost/gcp/2026-11-06.json': JSON.stringify({ version: 1, date: '2026-11-06', storage: { buckets: [{ bucket: 'a', soft_delete_off: false }] } }),
    }))).toEqual({ asOf: '2026-11-05 07:12Z', off: { a: true, b: false } })
  })
  it.each<[string, Record<string, string>]>([
    ['none written (the step isn\'t run here)', {}],
    ['only a cost snapshot: it covers the deployment\'s own buckets, not the swept ones', { 'snapshots/cost/gcp/2026-11-05.json': JSON.stringify({ version: 1, date: '2026-11-05', storage: { buckets: [{ bucket: 'a', soft_delete_off: true }] } }) }],
    ['the newest is of a version this build doesn\'t know', { [KEY('2026-11-04')]: softDeleteDoc('2026-11-04', { a: true }), [KEY('2026-11-05')]: softDeleteDoc('2026-11-05', { a: true }, 2) }],
  ])('no reading: %s', async (_, files) => expect(await bucketSoftDelete(env(files))).toBeNull())
  it('no reading when the store isn\'t configured, or the read fails: never throws', async () => {
    expect(await bucketSoftDelete({} as never)).toBeNull()
    expect(await bucketSoftDelete(env({ [KEY('2026-11-05')]: softDeleteDoc('2026-11-05', { a: true }) }, [KEY('2026-11-05')]))).toBeNull()
  })
})

describe('runUndoNow — a run\'s recorded undo against the reading', () => {
  const RUN = '2026-11-01-p1/20261101T120000Z'
  const NOW = 1_000_000
  const run = (o: Record<string, unknown> = {}) => ({ run_id: RUN, mode: 'real' as const, undo_deadline: NOW + 100, buckets: 'a,b', ...o })
  const reading = (off: Record<string, boolean>) => env({ [KEY('2026-11-05')]: softDeleteDoc('2026-11-05', off) })
  const NEITHER = { unavailable: null, unseen: null }

  it('a bucket off: unavailable, naming it; every bucket on: neither; one the reading doesn\'t name, or no reading: unseen', async () => {
    const { db } = await sqliteD1()
    expect([
      await runUndoNow(reading({ a: true, b: true }), db, run(), NOW),
      await runUndoNow(reading({ a: false, b: true }), db, run(), NOW),
      await runUndoNow(reading({ a: false, b: false }), db, run(), NOW),
      await runUndoNow(reading({ a: false }), db, run(), NOW),
      await runUndoNow(reading({ a: true }), db, run(), NOW),
      await runUndoNow(env({}), db, run(), NOW),
    ]).toEqual([
      { unavailable: { buckets: ['a', 'b'], asOf: '2026-11-05 07:12Z' }, unseen: null },
      { unavailable: { buckets: ['b'], asOf: '2026-11-05 07:12Z' }, unseen: null },
      NEITHER,
      { unavailable: null, unseen: { unseen: ['b'] } },
      { unavailable: { buckets: ['a'], asOf: '2026-11-05 07:12Z' }, unseen: null },
      { unavailable: null, unseen: { unseen: ['a', 'b'] } },
    ])
  })
  it('a run with no recorded cut: the buckets its bands name; none recorded: its buckets aren\'t known, so unseen', async () => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    await db.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, deleted_bytes, deleted_objects, skipped_gone, log_dir, plan_id)
      VALUES (?, 'gs://d/run', '2026-11-01', 0, 0, 'ann', 'real', 100, 0, 0, 0, 'gs://d/run', 1)`).bind(RUN).run()
    await db.prepare("INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects) VALUES (?, 'gs://b/x/', 1, 1, 0, 0, 0, 0)").bind(RUN).run()
    expect([
      await runUndoNow(reading({ a: false, b: true }), db, run({ buckets: null }), NOW),
      await runUndoNow(reading({ a: false, b: true }), db, run({ buckets: null, run_id: 'other' }), NOW),
    ]).toEqual([
      { unavailable: { buckets: ['b'], asOf: '2026-11-05 07:12Z' }, unseen: null },
      { unavailable: null, unseen: { unseen: [] } },
    ])
  })
  it('a dry run, a window closed or never recorded: no undo to qualify, and nothing is read', async () => {
    const { db } = await sqliteD1()
    const failing = { SERVED_R2: '1', INDEX_R2: { list: () => { throw new Error('read') }, get: () => { throw new Error('read') } } } as never
    expect([
      await runUndoNow(failing, db, run({ mode: 'dry' }), NOW),
      await runUndoNow(failing, db, run({ undo_deadline: NOW }), NOW),
      await runUndoNow(failing, db, run({ undo_deadline: null }), NOW),
    ]).toEqual([NEITHER, NEITHER, NEITHER])
  })
})
