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

/** The commands of the job a dispatch of `mode` submits at this minimum (null: no row), for a request shown `shown`; the rest of the launch stubbed. */
async function dispatched(mode: 'dry' | 'real', minimum: number | null, drop = false, shown?: number) {
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
  const req = { planId: 1, mode, date: '2026-11-04', actor: 'ops', siteUrl: 'https://site.example.test', ...(shown === undefined ? {} : { shownMinSoftDeleteDays: shown }) } as DispatchReq
  const prepared = await sweep.prepare(ENV, db as never, req)
  if ('error' in prepared) return { refused: prepared, fetched: bodies.length }
  const launched = await prepared.launch('2026-11-04', 'digest')
  if ('error' in launched) throw new Error(launched.error)
  const spec = JSON.parse(bodies[1]) as { taskGroups: { taskSpec: { runnables: { container: { commands: string[] } }[] } }[] }
  const script = spec.taskGroups[0].taskSpec.runnables[0].container.commands[1].split('\n')
  return { executor: script[script.length - 1].split(' ').slice(0, 15).join(' '), extra: launched.extra.min_soft_delete_days }
}

describe('sweep.prepare + launch', () => {
  it.each<[string, number | null, boolean, number | undefined, number]>([
    ['no row: 7', null, false, undefined, 7],
    ['a D1 without the table: 7', null, true, undefined, 7],
    ['the row, as shown', 0, false, 0, 0],
    ['a stricter row, on a request naming none', 14, false, undefined, 14],
    ['raised since it was shown: a stronger promise is no refusal', 14, false, 7, 14],
  ])('%s', async (_, minimum, drop, shown, expected) => {
    expect(await dispatched('real', minimum, drop, shown)).toEqual({
      executor: `dt-cloud sweep execute-manifest -e xml -j 64 -B 1 -c guided -r 8000 -s ${expected}`,
      extra: expected,
    })
  })
  describe('a real dispatch refuses a minimum lower than what the person was shown', () => {
    const terms = (now: string, shown: string) => `not deleting: the soft-delete minimum is now ${now}, lower than the ${shown} these terms promised, so the undo they describe may not exist. Re-read the terms and confirm again.`
    it.each<[string, number, number | undefined, string, number]>([
      ['shown 7, now 0', 0, 7, terms('0 days', '7 days'), 7],
      ['shown 7, now 3', 3, 7, terms('3 days', '7 days'), 7],
      ['shown 14, now 7', 7, 14, terms('7 days', '14 days'), 14],
      ['shown 1, now 0', 0, 1, terms('0 days', '1 day'), 1],
      ['no shown minimum (an old card, an API client) is 7', 0, undefined, terms('0 days', '7 days'), 7],
    ])('%s', async (_, minimum, shown, error, named) => {
      expect(await dispatched('real', minimum, false, shown)).toEqual({
        refused: { ok: false, status: 409, error, extra: { min_soft_delete_days: minimum, shown_min_soft_delete_days: named } },
        fetched: 0,
      })
    })
    it('a dry run promises nothing, so it isn\'t held to it', async () => {
      const { executor } = await dispatched('dry', 0, false, 7)
      expect(executor).toMatch(/^dt-cloud sweep execute -s 0 -b gcs-a "gs:\/\/my-data\/sweep\/runs\/gcs-sweep-dry-[0-9]{8}-[0-9]{6}z"$/)
    })
  })
  it('a dry run gets it too, so its "a real run would be refused" warning is the real run\'s answer', async () => {
    const { executor } = await dispatched('dry', 0)
    expect(executor).toBe(`dt-cloud sweep execute -s 0 -b ${BUCKET} "gs://my-data/sweep/runs/${/gcs-sweep-dry-[0-9]{8}-[0-9]{6}z/.exec(executor!)![0]}"`)
  })
})
