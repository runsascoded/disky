// specs/staged-runs.md: gcs's undo route, the jobs view, the run detail and
// the emptied-batch replay behind /staged.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { onRequest as plansRoute } from '../api/plans/[[path]]'
import { sweepJobView } from '../api/sweep/jobs'
import { onRequestPost as undoRoute } from '../api/sweep/undo'
import { emptiedBatches, stageItems, prefixShape } from './plans'
import type { SweepBatchJob } from './sweepReflect'
import { runBuckets, undoGate, type UndoRunRow } from './sweepUndo'
import { sqliteD1 } from './testD1'

const B1 = 'gcs-a'
const B2 = 'gcs-b'
const SHAPE = prefixShape({ STORE_SCHEME: 'gs://', STORE_BUCKETS: `${B1},${B2}` })!
const RUN = '2026-09-28-p1/20260928T120000Z'
const NOW = 1_000_000

const row = (o: Partial<UndoRunRow> = {}): UndoRunRow => ({
  run_id: RUN, mode: 'real', finished_ts: NOW - 100, undo_state: 'none', undo_deadline: NOW + 100, buckets: B1, ...o,
})

describe('undoGate — a finished real run, inside its window, not already undone', () => {
  it('refuses each other case with its reason, allows the rest', () => {
    const answer = (r: UndoRunRow | null) => { const g = undoGate(r, NOW); return g ? [g.status, g.error] : 'ok' }
    expect([
      answer(null),
      answer(row({ mode: 'dry' })),
      answer(row({ finished_ts: null })),
      answer(row({ undo_state: 'full' })),
      answer(row({ undo_deadline: null })),
      answer(row({ undo_deadline: NOW })),
      answer(row()),
      answer(row({ undo_state: 'partial' })),
    ]).toEqual([
      [404, 'no such run'],
      [400, 'only real runs can be undone (a dry run deleted nothing)'],
      [409, 'the run is still in progress; stop it first, undo once it has recorded its end'],
      [409, 'already undone'],
      [409, 'the run recorded no undo window'],
      [409, 'undo window closed (the soft-delete retention has passed)'],
      'ok',
      'ok',
    ])
  })
})

// A D1 (the cw lineage: the sweep schema is shared) with plan 1 and one finished real run of it.
async function gcsDb({ buckets = B1 as string | null, deadline = NOW + 100 as number | null, mode = 'real' } = {}) {
  const { db, raw } = await sqliteD1('cw')
  await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
  await db.prepare(`
    INSERT INTO deletion_runs (run_id, manifest, scan, head, exec_head, actor, mode, started_ts, finished_ts, deleted_bytes, deleted_objects,
      skipped_gone, skipped_overwritten, drift_dirs, undo_deadline, log_dir, buckets, plan_id)
    VALUES (?, 'gs://my-data/sweep/runs/gcs-sweep-real-20260928-120000z', '2026-09-28', 0, 0, 'ann', ?, 100, 200, 3000, 3, 1, 0, 0, ?,
      'gs://my-data/sweep/runs/gcs-sweep-real-20260928-120000z', ?, 1)
  `).bind(RUN, mode, deadline, buckets).run()
  await db.prepare(`INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects) VALUES
    (?, 'gs://${B2}/tmp/a/', 1000, 1, 0, 0, 0, 0), (?, 'gs://${B1}/ckpt/b/', 2000, 2, 1, 0, 0, 0)`).bind(RUN, RUN).run()
  return { db, raw }
}

describe('runBuckets — the run\'s recorded cut, else its bands\' buckets', () => {
  it('reads either', async () => {
    const { db } = await gcsDb({ buckets: null })
    expect(await runBuckets(db, { run_id: RUN, buckets: `${B2},${B1}` })).toEqual([B1, B2])
    expect(await runBuckets(db, { run_id: RUN, buckets: null })).toEqual([B1, B2])
    expect(await runBuckets(db, { run_id: 'other', buckets: null })).toEqual([])
  })
})

// A service-account key the test signs its own JWT with (`gcpToken` mints over
// WebCrypto; the token exchange itself is stubbed).
async function saKey(): Promise<string> {
  const { privateKey } = await crypto.subtle.generateKey(
    { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify'],
  ) as CryptoKeyPair
  const der = new Uint8Array(await crypto.subtle.exportKey('pkcs8', privateKey) as ArrayBuffer)
  let s = ''
  for (const b of der) s += String.fromCharCode(b)
  return JSON.stringify({ client_email: 'dispatch@my-project.iam.gserviceaccount.com', private_key: `-----BEGIN PRIVATE KEY-----\n${btoa(s)}\n-----END PRIVATE KEY-----\n` })
}

describe('POST /api/sweep/undo', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })
  const post = (body: unknown) => new Request('http://localhost/api/sweep/undo', { method: 'POST', body: JSON.stringify(body) })
  const env = async (db: unknown) => ({
    DB: db, GCP_SA_KEY: await saKey(), JOB_SA: 'job@my-project.iam.gserviceaccount.com', GCP_PROJECT: 'my-project',
    DATA_BUCKET: 'my-data', SWEEP_IMAGE: 'img:1', CF_ACCOUNT_ID: 'acct', D1_DB_ID: 'd1-id', D1_DB_NAME: 'my-db', BUCKET_REGIONS: JSON.stringify({ [B1]: 'us-east1', [B2]: 'europe-west4' }),
  })
  const answer = async (r: Response) => [r.status, await r.json()]

  it('refuses a bad id, an unknown run, a dry run and a closed window — before any GCP call', async () => {
    vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { db } = await gcsDb({ deadline: NOW - 1 })
    const { db: dry } = await gcsDb({ mode: 'dry' })
    expect([
      await undoRoute({ request: post({ run_id: 'gcs-sweep-real-20260928-120000z' }), env: await env(db) } as never).then(answer),
      await undoRoute({ request: post({ run_id: '2026-09-28-p9/20260928T120000Z' }), env: await env(db) } as never).then(answer),
      await undoRoute({ request: post({ run_id: RUN }), env: await env(dry) } as never).then(answer),
      await undoRoute({ request: post({ run_id: RUN }), env: await env(db) } as never).then(answer),
    ]).toEqual([
      [400, { error: 'bad run_id' }],
      [404, { error: 'no such run' }],
      [400, { error: 'only real runs can be undone (a dry run deleted nothing)' }],
      [409, { error: 'undo window closed (the soft-delete retention has passed)' }],
    ])
    expect(fetch).not.toHaveBeenCalled()
  })

  it('submits `gcs-undo-<stamp>z` on the dispatch\'s spec, in the run\'s bucket region, and marks the row partial', async () => {
    vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
    const calls: { url: string; body: unknown }[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url === 'https://oauth2.googleapis.com/token') return new Response(JSON.stringify({ access_token: 'tok' }))
      calls.push({ url, body: JSON.parse(String(init?.body)) })
      return new Response('{}')
    }))
    const { db } = await gcsDb()
    expect(await undoRoute({ request: post({ run_id: RUN }), env: await env(db) } as never).then(answer)).toEqual([200, {
      job_id: 'gcs-undo-19700112-134640z', target: RUN, region: 'us-east1', by: 'dev@example.test',
    }])
    expect(calls).toEqual([{
      url: 'https://batch.googleapis.com/v1/projects/my-project/locations/us-east1/jobs?job_id=gcs-undo-19700112-134640z',
      body: {
        taskGroups: [{
          taskCount: 1,
          taskSpec: {
            runnables: [{ container: { imageUri: 'img:1', entrypoint: '/bin/bash', commands: ['-c', [
              'set -euo pipefail',
              `trap 'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $SITE_TOKEN" "$SITE_URL/api/sweep/jobs" || true' EXIT`,
              'dt-cloud sweep undo "$TARGET_RUN"',
            ].join('\n')] } }],
            computeResource: { cpuMilli: 8000, memoryMib: 60000 },
            maxRetryCount: 0,
            maxRunDuration: '259200s',
            environment: {
              variables: {
                OP: 'undo', TARGET_RUN: RUN, USER: 'dev@example.test', CLOUDFLARE_ACCOUNT_ID: 'acct', DATA_BUCKET: 'my-data', D1_DB_ID: 'd1-id', D1_DB_NAME: 'my-db', SITE_URL: 'http://localhost',
              },
              secretVariables: {
                SITE_TOKEN: 'projects/my-project/secrets/gcs-sheet-sync-token/versions/latest',
                CLOUDFLARE_API_TOKEN: 'projects/my-project/secrets/cf-pages-token/versions/latest',
              },
            },
          },
        }],
        allocationPolicy: {
          instances: [{ policy: { machineType: 'n2-highmem-8', bootDisk: { type: 'pd-balanced', sizeGb: '100' } } }],
          serviceAccount: { email: 'job@my-project.iam.gserviceaccount.com' },
          location: { allowedLocations: ['regions/us-east1'] },
        },
        logsPolicy: { destination: 'CLOUD_LOGGING' },
      },
    }])
    expect(await db.prepare('SELECT run_id, undo_state FROM deletion_runs').all().then(r => r.results)).toEqual([{ run_id: RUN, undo_state: 'partial' }])
  })

  it('a failed submit leaves the row as it was and says why', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => url === 'https://oauth2.googleapis.com/token'
      ? new Response(JSON.stringify({ access_token: 'tok' }))
      : new Response('{"error":{"code":403}}', { status: 403 })))
    vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
    const { db } = await gcsDb()
    expect(await undoRoute({ request: post({ run_id: RUN }), env: await env(db) } as never).then(answer))
      .toEqual([500, { error: 'batch submit failed (403)', status: 403, detail: { error: { code: 403 } } }])
    expect(await db.prepare('SELECT undo_state FROM deletion_runs').all().then(r => r.results)).toEqual([{ undo_state: 'none' }])
  })
})

describe('sweepJobView — a listed Batch job as /staged reads it', () => {
  const cfg = { project: 'my-project', dataBucket: 'my-data', bucketRegions: { [B1]: 'us-east1' } }
  const job = (id: string, vars: Record<string, string>, script: string): SweepBatchJob => ({
    name: `projects/my-project/locations/us-east1/jobs/${id}`, uid: `uid-${id}`, createTime: '2026-09-28T12:00:00Z', region: 'us-east1',
    status: { state: 'RUNNING', runDuration: '61.5s', statusEvents: [{ description: 'scheduled' }, { description: 'running' }] },
    taskGroups: [{ taskSpec: { environment: { variables: vars }, runnables: [{ container: { commands: ['-c', script] } }] } }],
  })
  it('a run: its plan, cut and region; an undo: its target', () => {
    const logs = (uid: string) => `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(`labels.job_uid="${uid}"\nlog_id("batch_task_logs")\ntimestamp >= "2026-09-28T12:00:00Z"`)}?project=my-project`
    expect([
      sweepJobView(cfg, job('gcs-sweep-real-20260928-120000z', { USER: 'ann', SWEEP_DATE: '2026-09-27', PLAN_ID: '1' }, `dt-cloud sweep execute -b ${B1} --for-real x`)),
      sweepJobView(cfg, job('gcs-undo-20260929-120000z', { USER: 'bob', OP: 'undo', TARGET_RUN: RUN }, 'dt-cloud sweep undo "$TARGET_RUN"')),
    ]).toEqual([{
      job_id: 'gcs-sweep-real-20260928-120000z', op: 'sweep', mode: 'real', state: 'RUNNING', created: '2026-09-28T12:00:00Z', updated: null,
      run_secs: 61.5, by: 'ann', date: '2026-09-27', plan_id: 1, target: null, buckets: [B1], region: 'us-east1', bucket_region: 'us-east1',
      plan: 'gs://my-data/sweep/runs/gcs-sweep-real-20260928-120000z', last_event: 'running', logs: logs('uid-gcs-sweep-real-20260928-120000z'),
    }, {
      job_id: 'gcs-undo-20260929-120000z', op: 'undo', mode: 'real', state: 'RUNNING', created: '2026-09-28T12:00:00Z', updated: null,
      run_secs: 61.5, by: 'bob', date: null, plan_id: null, target: RUN, buckets: [], region: 'us-east1', bucket_region: null,
      plan: 'gs://my-data/sweep/runs/gcs-undo-20260929-120000z', last_event: 'running', logs: logs('uid-gcs-undo-20260929-120000z'),
    }])
  })
})

describe('GET /api/plans/run?id= — a run\'s record and bands, largest first', () => {
  it('answers the run, 404s an unknown one', async () => {
    const { db } = await gcsDb()
    const get = (q: string) => plansRoute({ request: new Request(`http://localhost/api/plans/run${q}`), env: { DB: db } } as never)
    const r = await get(`?id=${encodeURIComponent(RUN)}`)
    const body = (await r.json()) as { run: { run_id: string; deleted_objects: number }; bands: unknown[] }
    expect([r.status, body.run.run_id, body.run.deleted_objects, body.bands]).toEqual([200, RUN, 3, [
      { prefix: `gs://${B1}/ckpt/b/`, bytes: 2000, objects: 2, gone: 1, overwritten: 0, drift_new_objects: 0, undone_objects: 0 },
      { prefix: `gs://${B2}/tmp/a/`, bytes: 1000, objects: 1, gone: 0, overwritten: 0, drift_new_objects: 0, undone_objects: 0 },
    ]])
    expect([(await get('?id=nope')).status, (await get('')).status]).toEqual([404, 400])
  })
})

describe('emptied stage batches — replayed from the plan_items audit trail', () => {
  const batch = (id: number, ts: number) => ({ id, plan_id: 1, note: null, created_by: 'ann', created_ts: ts })
  const ins = (batch_id: number, staged: string[], absorbed: string[] = [], covered: string[] = []) =>
    ({ action: 'insert', old_json: absorbed.length ? JSON.stringify({ absorbed }) : null, new_json: JSON.stringify({ staged, covered, batch_id, note: null }) })
  const del = (prefixes: string[]) => ({ action: 'delete', old_json: JSON.stringify({ prefixes, own: false }), new_json: null })
  const item = (prefix: string, batch_id: number) => ({ prefix, note: null, added_by: 'ann', added_ts: 1, batch_id })

  it('absorbed into later batches, unstaged, covered from the start; batches with items left are not emptied', () => {
    const a = 'gs://b/x/a/'; const b = 'gs://b/x/b/'; const c = 'gs://b/y/c/'; const x = 'gs://b/x/'; const y = 'gs://b/y/'
    expect(emptiedBatches(
      [batch(1, 10), batch(2, 20), batch(3, 30), batch(4, 40), batch(5, 50)],
      [item(x, 3), item(y, 5)],
      [
        ins(1, [a, b]),
        ins(2, [c]),
        del([c]),
        ins(3, [x], [a, b]),
        ins(4, [], [], ['gs://b/x/z/']),
        ins(5, [y]),
      ],
    )).toEqual([
      { ...batch(1, 10), staged: 2, covered: 0, absorbed: [{ into: 3, n: 2 }], unstaged: 0 },
      { ...batch(2, 20), staged: 1, covered: 0, absorbed: [], unstaged: 1 },
      { ...batch(4, 40), staged: 0, covered: 1, absorbed: [], unstaged: 0 },
    ])
  })

  it('end to end: two gestures, the second stages the first\'s ancestor; /api/plans/staged carries it', async () => {
    const { db } = await sqliteD1('cw')
    const s1 = await stageItems(db, [`gs://${B1}/runs/a/`, `gs://${B1}/runs/b/`], 'ann', 'old runs', SHAPE)
    const s2 = await stageItems(db, [`gs://${B1}/runs/`], 'bob', 'all of runs', SHAPE)
    expect([s1, s2]).toEqual([
      { plan_id: 1, batch_id: 1, staged: [`gs://${B1}/runs/a/`, `gs://${B1}/runs/b/`], covered: [], absorbed: [] },
      { plan_id: 1, batch_id: 2, staged: [`gs://${B1}/runs/`], covered: [], absorbed: [`gs://${B1}/runs/a/`, `gs://${B1}/runs/b/`] },
    ])
    const r = await plansRoute({ request: new Request('http://localhost/api/plans/staged'), env: { DB: db, STAGING: '1' } } as never)
    const body = (await r.json()) as { items: { prefix: string; batch_id: number }[]; emptied: { id: number; created_by: string; note: string; staged: number; absorbed: unknown[] }[] }
    expect([body.items.map(i => [i.prefix, i.batch_id]), body.emptied.map(e => [e.id, e.created_by, e.note, e.staged, e.absorbed])]).toEqual([
      [[`gs://${B1}/runs/`, 2]],
      [[1, 'ann', 'old runs', 2, [{ into: 2, n: 2 }]]],
    ])
  })
})
