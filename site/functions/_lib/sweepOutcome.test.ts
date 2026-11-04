import { expect, it, vi } from 'vitest'
import { NotFoundError } from '@rdub/file-tree'
import { announceFinished } from './stagedSlack'
import { outcomeReaderOf, runOutcomeOf } from './sweepOutcome'
import { reflectSweepRuns, type SweepBatchJob } from './sweepReflect'
import { runDir } from './sweepDispatch'
import { RUN_DIR, sqliteD1 } from './testD1'

const JOB = 'gcs-sweep-real-20261104-120000z'
const KEY = `sweep/runs/${JOB}/plan-summary.json`
const GENERATIONS = { contract: 'manifest-delete-generations', backend: 'xml', buckets: {} }
const SKIPS = { bytes: 2048, objects: 3, whole_buckets: ['b2'] }
const file = (doc: unknown) => ({ bytes: new TextEncoder().encode(JSON.stringify(doc)) })
const reading = (doc: unknown) => outcomeReaderOf(async () => file(doc))

it('reads the run dir\'s `plan-summary.json`', async () => {
  const get = vi.fn(async (_key: string) => file(GENERATIONS))
  expect(await runOutcomeOf(JOB, get)).toEqual({ attempted: true, skipped: null, notSwept: [] })
  expect(get.mock.calls).toEqual([[KEY]])
})

it('a generation run: attempted, with its skips and not-swept buckets', async () => {
  expect(await reading({ ...GENERATIONS, skipped_no_generation: SKIPS })(JOB)).toEqual({ attempted: true, skipped: { bytes: 2048, objects: 3 }, notSwept: ['b2'] })
  expect(await reading({ ...GENERATIONS, contract: 'reviewed-delete-generations', skipped_no_generation: { ...SKIPS, whole_buckets: [] } })(JOB)).toEqual({ attempted: true, skipped: { bytes: 2048, objects: 3 }, notSwept: [] })
})

it('agrees with the row on what "attempted" is: a generation run over JSON saw each reply, and keeps "deleted"', async () => {
  expect(await reading({ ...GENERATIONS, backend: 'json' })(JOB)).toEqual(null)
})

it('an older run has nothing to add; zero skips are none', async () => {
  expect(await reading({ buckets: {} })(JOB)).toEqual(null)
  expect(await reading({ ...GENERATIONS, skipped_no_generation: { bytes: 0, objects: 0, whole_buckets: [] } })(JOB)).toEqual({ attempted: true, skipped: null, notSwept: [] })
})

it('never throws: a missing file, a failed read or a document it can\'t read is no outcome', async () => {
  const log = vi.spyOn(console, 'log').mockImplementation(() => {})
  const missing = async (key: string) => { throw new NotFoundError(key) }
  const down = async () => { throw new Error('store down') }
  const notJson = async () => ({ bytes: new TextEncoder().encode('{"contract": ') })
  expect(await Promise.all([
    outcomeReaderOf(missing)(JOB),
    outcomeReaderOf(down)(JOB),
    outcomeReaderOf(notJson)(JOB),
    reading(null)(JOB),
    reading({ ...GENERATIONS, skipped_no_generation: { objects: 'many', bytes: 1, whole_buckets: [] } })(JOB),
    reading({ ...GENERATIONS, skipped_no_generation: { objects: 1, bytes: 1, whole_buckets: 'b1' } })(JOB),
  ])).toEqual([null, null, null, null, null, null])
  expect(log.mock.calls.map(([line]) => (line as string).split(':')[0])).toEqual(Array(5).fill(`outcome of ${JOB}`))
  log.mockRestore()
})

it('end to end: a finished generation run\'s thread message, and a failed read\'s', async () => {
  const CFG = { dataBucket: 'my-data' }
  const finishedJob: SweepBatchJob = {
    name: `projects/p/locations/us-central1/jobs/${JOB}`, uid: JOB, createTime: '2026-09-28T12:00:00Z', region: 'us-central1', status: { state: 'SUCCEEDED' },
    taskGroups: [{ taskSpec: { environment: { variables: { SWEEP_DATE: '2026-09-28', PLAN_DIGEST: 'D' } } } }],
  }
  const replies = async (reader: ReturnType<typeof outcomeReaderOf>): Promise<string[]> => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    await db.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, deleted_bytes, deleted_objects, log_dir, plan_id)
      VALUES ('r', ?, '2026-09-28', 0, 0, 'ann', 'real', 100, 500, 1024, 2, ?, 1)`).bind(runDir(CFG, JOB), runDir(CFG, JOB)).run()
    const posts: { thread_ts?: string; text?: string }[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('chat.postMessage')) posts.push(JSON.parse(init!.body as string))
      return new Response(JSON.stringify({ ok: true, ts: '1.000001', messages: [] }))
    }))
    const finished = await reflectSweepRuns(CFG, db, [finishedJob], Date.parse('2026-09-28T13:00:00Z') / 1000, undefined, RUN_DIR, reader)
    await announceFinished({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1' } as Parameters<typeof announceFinished>[0], db, finished, 'https://site.test')
    vi.unstubAllGlobals()
    return posts.filter(p => p.thread_ts).map(p => p.text!)
  }
  const link = '<https://site.test/staged?run=r|r>'
  expect(await replies(reading({ ...GENERATIONS, skipped_no_generation: SKIPS }))).toEqual([
    `:warning: Real deletion ${link} finished: attempted *1.0 KiB* / 2 objects. Skipped, no generation: *2.0 KiB* / 3 objects left in place. *Not swept:* \`b2\` (no object in them had a generation; their prefixes stay staged).`,
  ])
  const log = vi.spyOn(console, 'log').mockImplementation(() => {})
  expect(await replies(outcomeReaderOf(async () => { throw new Error('store down') }))).toEqual([
    `:white_check_mark: Real deletion ${link} finished: deleted *1.0 KiB* / 2 objects.`,
  ])
  log.mockRestore()
})
