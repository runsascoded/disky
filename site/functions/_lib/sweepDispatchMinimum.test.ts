// A dispatch reads the soft-delete minimum from `sweep_settings` and submits it on the job's command line, the only way the
// value reaches a run: nothing the job reads later can move it.
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DispatchReq, ExecEnv } from './dispatch'
import { sweep } from './sweepDispatch'
import { sqliteD1 } from './testD1'

vi.mock('./gcp.js', async original => ({ ...await original<typeof import('./gcp.js')>(), gcpToken: async () => 'token' }))

afterEach(() => vi.unstubAllGlobals())

const BUCKET = 'gcs-a'
const ENV = {
  GCP_SA_KEY: 'key', JOB_SA: 'job@example.test', GCP_PROJECT: 'project', DATA_BUCKET: 'my-data', SWEEP_IMAGE: 'image@sha256:abc', CF_ACCOUNT_ID: 'account', D1_DB_ID: 'db',
  STORE_SCHEME: 'gs://', STORE_BUCKETS: BUCKET, BUCKET_REGIONS: JSON.stringify({ [BUCKET]: 'us-east5' }),
} as unknown as ExecEnv

/** The commands of the job a dispatch of `mode` submits at this minimum (null: no row); the rest of the launch stubbed. */
async function dispatched(mode: 'dry' | 'real', minimum: number | null, drop = false) {
  const { db, raw } = await sqliteD1()
  raw.exec("INSERT INTO plans (id, name, created_by, created_ts) VALUES (1, 'review', 'actor', 1)")
  raw.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'actor', 1)").run(`gs://${BUCKET}/a/`)
  if (minimum !== null) raw.prepare("INSERT INTO sweep_settings (id, min_soft_delete_days, updated_by, updated_at) VALUES (1, ?, 'ops', 1)").run(minimum)
  if (drop) raw.exec('DROP TABLE sweep_settings')
  const bodies: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (_: string, init: RequestInit) => {
    bodies.push(String(init.body))
    return new Response('{}', { status: 200 })
  }))
  const req = { planId: 1, mode, date: '2026-11-04', actor: 'ops', siteUrl: 'https://site.example.test' } as DispatchReq
  const prepared = await sweep.prepare(ENV, db as never, req)
  if ('error' in prepared) throw new Error(prepared.error)
  const launched = await prepared.launch('2026-11-04', 'digest')
  if ('error' in launched) throw new Error(launched.error)
  const spec = JSON.parse(bodies[1]) as { taskGroups: { taskSpec: { runnables: { container: { commands: string[] } }[] } }[] }
  const script = spec.taskGroups[0].taskSpec.runnables[0].container.commands[1].split('\n')
  return { executor: script[script.length - 1].split(' ').slice(0, 15).join(' '), extra: launched.extra.min_soft_delete_days }
}

describe('sweep.prepare + launch', () => {
  it.each<[string, number | null, boolean, number]>([
    ['no row: 7', null, false, 7],
    ['a D1 without the table: 7', null, true, 7],
    ['the row', 0, false, 0],
    ['a stricter row', 14, false, 14],
  ])('%s', async (_, minimum, drop, expected) => {
    expect(await dispatched('real', minimum, drop)).toEqual({
      executor: `dt-cloud sweep execute-manifest -e xml -j 64 -B 1 -c guided -r 8000 -s ${expected}`,
      extra: expected,
    })
  })
  it('a dry run gets it too, so its "a real run would be refused" warning is the real run\'s answer', async () => {
    const { executor } = await dispatched('dry', 0)
    expect(executor).toBe(`dt-cloud sweep execute -s 0 -b ${BUCKET} "gs://my-data/sweep/runs/${/gcs-sweep-dry-[0-9]{8}-[0-9]{6}z/.exec(executor)![0]}"`)
  })
})
