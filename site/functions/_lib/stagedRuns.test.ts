// specs/staged-runs.md: gcs's undo route, the jobs view, the run detail and
// the emptied-batch replay behind /staged.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { onRequest as plansRoute } from '../api/plans/[[path]]'
import { sweepJobView } from '../api/sweep/jobs'
import { onRequestPost as undoRoute } from '../api/sweep/undo'
import { emptiedBatches, stageItems, prefixShape, snapshotPlanBuckets, unstageDeleted } from './plans'
import { reflectSweepRuns, type SweepBatchJob } from './sweepReflect'
import { runBuckets, undoGate, type UndoRunRow } from './sweepUndo'
import { RUN_DIR, sqliteD1 } from './testD1'
import { fakeR2, softDeleteDoc } from './testR2'

const B1 = 'gcs-a'
const B2 = 'gcs-b'
const SHAPE = prefixShape({ STORE_SCHEME: 'gs://', STORE_BUCKETS: `${B1},${B2}` })!
const RUN = '2026-09-28-p1/20260928T120000Z'
const NOW = 1_000_000

const row = (o: Partial<UndoRunRow> = {}): UndoRunRow => ({
  run_id: RUN, mode: 'real', finished_ts: NOW - 100, undo_state: 'none', undo_deadline: NOW + 100, buckets: B1, ...o,
})

describe('undoGate — a finished real run, inside its window, not already undone', () => {
  it('a run that required nothing and measured nothing says why there is no undo', () => {
    expect(undoGate(row({ undo_deadline: null, soft_delete_days: null, min_soft_delete_days: 0 }), NOW)).toEqual({ ok: false, status: 409, error: 'no undo promised: this run required no soft delete and recorded no retention, so treat its deletes as permanent' })
  })
  it('a run that recorded soft delete off says why there is no undo', () => {
    expect(undoGate(row({ undo_deadline: null, soft_delete_days: 0 }), NOW)).toEqual({ ok: false, status: 409, error: 'no undo: a bucket in this run had soft delete off, so what it deleted there is gone for good' })
  })
  it('accepts a real run closed from what it wrote, once its window is set', () => {
    expect(undoGate(row({ undo_deadline: null }), NOW)).toEqual({ ok: false, status: 409, error: 'the run recorded no undo window' })
    expect(undoGate(row({ finished_ts: NOW - 100, undo_deadline: NOW - 100 + 7 * 86400 }), NOW)).toBeNull()
  })
  it('refuses an undo GCS can no longer do, naming the buckets and the snapshot; says where a fresher answer is', () => {
    expect(undoGate(row(), NOW, { buckets: [B1, B2], asOf: '2026-11-05' })).toEqual({
      ok: false, status: 409,
      error: `undo unavailable: soft delete is off on ${B1}, ${B2} (as of 2026-11-05); if that has changed since, \`dt-cloud sweep undo\` asks GCS itself`,
    })
    expect(undoGate(row(), NOW, null)).toBeNull()
    // The run's own record comes first: a closed window stays a closed window.
    expect(undoGate(row({ undo_deadline: NOW }), NOW, { buckets: [B1], asOf: '2026-11-05' })).toEqual({ ok: false, status: 409, error: 'undo window closed (the soft-delete retention has passed)' })
  })
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

describe('D1', () => {
  // A D1 (the sweep schema is shared, but for `RUN_DIR`) with plan 1 and one finished real run of it.
  async function gcsDb({ buckets = B1 as string | null, deadline = NOW + 100 as number | null, mode = 'real' } = {}) {
    const { db, raw } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    await db.prepare(`
    INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, deleted_bytes, deleted_objects,
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

    describe('against the job\'s newest reading of the buckets\' soft delete', () => {
      const snapshot = (off: Record<string, boolean>) => ({ SERVED_R2: '1', INDEX_R2: fakeR2({ 'snapshots/soft-delete/2026-11-05.json': softDeleteDoc('2026-11-05', off) }) })
      const state = async (db: Awaited<ReturnType<typeof gcsDb>>['db']) => (await db.prepare('SELECT undo_state, undo_deadline FROM deletion_runs WHERE run_id = ?').bind(RUN).first())
      it('refuses a run on a bucket it shows off, before any GCP call, leaving the row as recorded', async () => {
        vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
        const fetch = vi.fn()
        vi.stubGlobal('fetch', fetch)
        const { db } = await gcsDb()
        expect(await undoRoute({ request: post({ run_id: RUN }), env: { ...await env(db), ...snapshot({ [B1]: true, [B2]: false }) } } as never).then(answer)).toEqual([409, {
          error: `undo unavailable: soft delete is off on ${B1} (as of 2026-11-05 07:12Z); if that has changed since, \`dt-cloud sweep undo\` asks GCS itself`,
        }])
        expect(fetch).not.toHaveBeenCalled()
        expect(await state(db)).toEqual({ undo_state: 'none', undo_deadline: NOW + 100 })
      })
      it('a run with no recorded cut is held to the buckets its bands name: one off refuses it', async () => {
        vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
        const fetch = vi.fn()
        vi.stubGlobal('fetch', fetch)
        const { db } = await gcsDb({ buckets: null })
        expect(await undoRoute({ request: post({ run_id: RUN }), env: { ...await env(db), ...snapshot({ [B1]: false, [B2]: true }) } } as never).then(answer)).toEqual([409, {
          error: `undo unavailable: soft delete is off on ${B2} (as of 2026-11-05 07:12Z); if that has changed since, \`dt-cloud sweep undo\` asks GCS itself`,
        }])
        expect(fetch).not.toHaveBeenCalled()
      })
      it.each<[string, Record<string, boolean>]>([
        ['it shows the run\'s buckets on (another bucket off doesn\'t matter)', { [B1]: false, [B2]: true }],
        ['it says nothing of the run\'s bucket: unknown isn\'t unavailable, and the job asks GCS itself', { [B2]: true }],
      ])('dispatches where %s', async (_, off) => {
        vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
        vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(url === 'https://oauth2.googleapis.com/token' ? JSON.stringify({ access_token: 'tok' }) : '{}')))
        const { db } = await gcsDb()
        expect(await undoRoute({ request: post({ run_id: RUN }), env: { ...await env(db), ...snapshot(off) } } as never).then(answer)).toEqual([200, {
          bulk: false, job_id: 'gcs-undo-19700112-134640z', target: RUN, region: 'us-east1', by: 'dev@example.test',
        }])
        expect(await state(db)).toEqual({ undo_state: 'none', undo_deadline: NOW + 100 })
      })
    })

    describe('the row says nothing of an undo until its job has restored something', () => {
      const submitted = async (before: string) => {
        vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
        vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(url === 'https://oauth2.googleapis.com/token' ? JSON.stringify({ access_token: 'tok' }) : '{}')))
        const { db } = await gcsDb()
        await db.prepare('UPDATE deletion_runs SET undo_state = ? WHERE run_id = ?').bind(before, RUN).run()
        const submit = await undoRoute({ request: post({ run_id: RUN }), env: await env(db) } as never)
        const read = () => db.prepare(`SELECT r.undo_state, (SELECT SUM(undone_objects) FROM deletion_bands b WHERE b.run_id = r.run_id) AS undone, (SELECT COUNT(*) FROM admin_edits WHERE tbl = 'deletion_runs' AND pk = r.run_id) AS audited
          FROM deletion_runs r WHERE r.run_id = ?`).bind(RUN).first()
        return { db, status: submit.status, read }
      }
      // What Batch lists of an undo job that ended without recording: refused (soft delete off now, asked of GCS), killed, or an image that never ran.
      const undoJob = (state: string): SweepBatchJob => ({
        name: 'projects/my-project/locations/us-east1/jobs/gcs-undo-19700112-134640z', uid: 'uid-undo', createTime: '1970-01-12T13:46:40Z', region: 'us-east1',
        status: { state }, taskGroups: [{ taskSpec: { environment: { variables: { OP: 'undo', TARGET_RUN: RUN } } } }],
      })
      it.each<[string, string, string]>([
        ['a run not undone, its job refused or killed before restoring: still not undone', 'none', 'FAILED'],
        ['…or cancelled', 'none', 'CANCELLED'],
        ['…or still running', 'none', 'RUNNING'],
        ['a run an earlier undo left partly undone: still that', 'partial', 'FAILED'],
      ])('%s', async (_, before, jobState) => {
        const { db, status, read } = await submitted(before)
        expect([status, await read()]).toEqual([200, { undo_state: before, undone: 0, audited: 1 }])
        expect(await reflectSweepRuns({ dataBucket: 'my-data' }, db, [undoJob(jobState)], NOW + 60, undefined, RUN_DIR)).toEqual([])
        expect(await read()).toEqual({ undo_state: before, undone: 0, audited: 1 })
      })
    })

    it('submits `gcs-undo-<stamp>z` on the dispatch\'s spec, in the run\'s bucket region, and leaves the row\'s undo state to the job', async () => {
      vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
      const calls: { url: string; body: unknown }[] = []
      vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
        if (url === 'https://oauth2.googleapis.com/token') return new Response(JSON.stringify({ access_token: 'tok' }))
        calls.push({ url, body: JSON.parse(String(init?.body)) })
        return new Response('{}')
      }))
      const { db } = await gcsDb()
      expect(await undoRoute({ request: post({ run_id: RUN }), env: await env(db) } as never).then(answer)).toEqual([200, {
        bulk: false, job_id: 'gcs-undo-19700112-134640z', target: RUN, region: 'us-east1', by: 'dev@example.test',
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
      expect(await db.prepare('SELECT run_id, undo_state FROM deletion_runs').all().then(r => r.results)).toEqual([{ run_id: RUN, undo_state: 'none' }])
    })

    it('a run whose buckets `BUCKET_REGIONS` does not place runs in `BATCH_REGION`; neither set, it is refused before any GCP call', async () => {
      vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
      const urls: string[] = []
      vi.stubGlobal('fetch', vi.fn(async (url: string) => {
        if (url !== 'https://oauth2.googleapis.com/token') urls.push(url)
        return new Response(url === 'https://oauth2.googleapis.com/token' ? JSON.stringify({ access_token: 'tok' }) : '{}')
      }))
      const unplaced = async (extra: Record<string, string>) => {
        const { db } = await gcsDb()
        return undoRoute({ request: post({ run_id: RUN }), env: { ...await env(db), BUCKET_REGIONS: '{}', ...extra } } as never).then(answer)
      }
      expect(await unplaced({})).toEqual([503, { error: 'undo not configured (BATCH_REGION unset)' }])
      expect(urls).toEqual([])
      expect(await unplaced({ BATCH_REGION: 'us-west4' })).toEqual([200, { bulk: false, job_id: 'gcs-undo-19700112-134640z', target: RUN, region: 'us-west4', by: 'dev@example.test' }])
      expect(urls).toEqual(['https://batch.googleapis.com/v1/projects/my-project/locations/us-west4/jobs?job_id=gcs-undo-19700112-134640z'])
    })

    it('labels the job and its allocation policy `DISKY_LABELS` + `component: sweep-undo` (the spec above, with none set, carries neither)', async () => {
      vi.useFakeTimers({ now: NOW * 1000, toFake: ['Date'] })
      const labels: unknown[] = []
      vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
        if (url === 'https://oauth2.googleapis.com/token') return new Response(JSON.stringify({ access_token: 'tok' }))
        const spec = JSON.parse(String(init?.body)) as { labels?: unknown; allocationPolicy: { labels?: unknown } }
        labels.push(spec.labels, spec.allocationPolicy.labels)
        return new Response('{}')
      }))
      const { db } = await gcsDb()
      const r = await undoRoute({ request: post({ run_id: RUN }), env: { ...await env(db), DISKY_LABELS: 'app=disky,deployment=gcs' } } as never)
      const want = { app: 'disky', deployment: 'gcs', component: 'sweep-undo' }
      expect([r.status, labels]).toEqual([200, [want, want]])
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
    it('a run dispatched with a selection total carries it, display only', () => {
      const v = sweepJobView(cfg, job('gcs-sweep-real-20260928-120000z', { USER: 'ann', EXPECTED_BYTES: '137000', EXPECTED_OBJECTS: '754' }, 'x'))
      expect(v.expected).toEqual({ bytes: 137_000, objects: 754 })
    })
    it('a run: its plan, cut and region; an undo: its target', () => {
      const logs = (uid: string) => `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(`labels.job_uid="${uid}"\nlog_id("batch_task_logs")\ntimestamp >= "2026-09-28T12:00:00Z"`).replace('(', '%28').replace(')', '%29')}?project=my-project`
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
    const item = (prefix: string, batch_id: number) => ({ prefix, kind: 'prefix' as const, note: null, added_by: 'ann', added_ts: 1, batch_id, as_of: null })

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
        { ...batch(1, 10), prefixes: [a, b], staged: 2, covered: 0, absorbed: [{ into: 3, n: 2 }], unstaged: 0, deleted: 0 },
        { ...batch(2, 20), prefixes: [c], staged: 1, covered: 0, absorbed: [], unstaged: 1, deleted: 0 },
        { ...batch(4, 40), prefixes: [], staged: 0, covered: 1, absorbed: [], unstaged: 0, deleted: 0 },
      ])
    })

    it('end to end: two gestures, the second stages the first\'s ancestor; /api/plans/staged carries it', async () => {
      const { db } = await sqliteD1()
      const s1 = await stageItems(db, [`gs://${B1}/runs/a/`, `gs://${B1}/runs/b/`], 'ann', 'old runs', SHAPE, '2026-09-27')
      const s2 = await stageItems(db, [`gs://${B1}/runs/`], 'bob', 'all of runs', SHAPE, '2026-09-28')
      expect([s1, s2]).toEqual([
        { plan_id: 1, batch_id: 1, staged: [`gs://${B1}/runs/a/`, `gs://${B1}/runs/b/`], covered: [], absorbed: [], as_of: '2026-09-27' },
        { plan_id: 1, batch_id: 2, staged: [`gs://${B1}/runs/`], covered: [], absorbed: [`gs://${B1}/runs/a/`, `gs://${B1}/runs/b/`], as_of: '2026-09-28' },
      ])
      const r = await plansRoute({ request: new Request('http://localhost/api/plans/staged'), env: { DB: db, STAGING: '1' } } as never)
      const body = (await r.json()) as { items: { prefix: string; batch_id: number; as_of: string }[]; emptied: { id: number; created_by: string; note: string; staged: number; absorbed: unknown[] }[] }
      expect([body.items.map(i => [i.prefix, i.batch_id, i.as_of]), body.emptied.map(e => [e.id, e.created_by, e.note, e.staged, e.absorbed])]).toEqual([
        [[`gs://${B1}/runs/`, 2, '2026-09-28']],
        [[1, 'ann', 'old runs', 2, [{ into: 2, n: 2 }]]],
      ])
    })

    it('POST /api/plans/stage: `as_of` defaults to the latest scan, must name a scan, and a re-stage keeps the first; the snapshot carries it', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec(`INSERT INTO index_schema (date, variant, version, schema_json) VALUES
      ('2026-10-06', 'path', 2, '[]'), ('2026-10-07', 'path', 2, '[]'), ('2026-10-07', 'bysize', 2, '[]')`)
      const env = { DB: db, STAGING: '1', STORE_SCHEME: 'gs://', STORE_BUCKETS: `${B1},${B2}`, DEV_EMAIL: 'ann@example.test' }
      const stage = async (body: object) => {
        const r = await plansRoute({ request: new Request('http://localhost/api/plans/stage', { method: 'POST', body: JSON.stringify(body) }), env } as never)
        return [r.status, await r.json()]
      }
      const a = `gs://${B1}/a/`, b = `gs://${B1}/b/`, c = `gs://${B2}/c/`
      expect([
        await stage({ prefixes: [a] }),
        await stage({ prefixes: [a, b], as_of: '2026-10-06' }),
        await stage({ prefixes: [c], as_of: '2026-10-05' }),
        await stage({ prefixes: [c], as_of: 20261007 }),
      ]).toEqual([
        [201, { plan_id: 1, batch_id: 1, staged: [a], covered: [], absorbed: [], as_of: '2026-10-07' }],
        [201, { plan_id: 1, batch_id: 2, staged: [b], covered: [], absorbed: [], as_of: '2026-10-06' }],
        [400, { error: 'as_of must be a scan date (latest 2026-10-07)', as_of: '2026-10-05' }],
        [400, { error: 'as_of must be a scan date (latest 2026-10-07)', as_of: 20261007 }],
      ])
      const r = await plansRoute({ request: new Request('http://localhost/api/plans/staged'), env } as never)
      const body = (await r.json()) as { items: { prefix: string; batch_id: number; as_of: string }[] }
      expect([
        body.items.map(i => [i.prefix, i.batch_id, i.as_of]).sort(),
        await snapshotPlanBuckets(db, 1, prefixShape(env)!),
        raw.prepare("SELECT new_json FROM admin_edits WHERE tbl = 'plan_items' ORDER BY id").all().map(e => JSON.parse((e as { new_json: string }).new_json)),
      ]).toEqual([
        [[a, 1, '2026-10-07'], [b, 2, '2026-10-06']],
        { plan_id: 1, name: 'Staged', sweep: [a, b], buckets: [B1], as_of: { [a]: '2026-10-07', [b]: '2026-10-06' } },
        [
          { staged: [a], covered: [], batch_id: 1, note: null, as_of: '2026-10-07' },
          { staged: [b], covered: [], batch_id: 2, note: null, as_of: '2026-10-06' },
        ],
      ])
    })
  })

  describe('unstageDeleted — a finished real run takes what it deleted out of the queue', () => {
    it('completes a verified empty review once, recording zero processing rather than deleting newer versions', async () => {
      const { db, raw } = await gcsDb()
      const prefix = `gs://${B1}/newer-only/`
      raw.prepare('INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, ?, 50)').run(prefix, 'ann')
      raw.prepare('INSERT INTO deletion_bands (run_id, prefix, bytes, objects) VALUES (?, ?, 0, 0)').run(RUN, prefix)
      expect(await unstageDeleted(db, 1, [RUN])).toEqual([prefix])
      expect(await unstageDeleted(db, 1, [RUN])).toEqual([])
      expect(raw.prepare('SELECT prefix FROM plan_items').all()).toEqual([])
      expect(raw.prepare('SELECT bytes, objects FROM deletion_bands WHERE run_id = ? AND prefix = ?').all(RUN, prefix)).toEqual([{ bytes: 0, objects: 0 }])
      expect(raw.prepare("SELECT action, who, old_json, new_json FROM admin_edits WHERE tbl = 'plan_items'").all()).toEqual([
        { action: 'delete', who: `run:${RUN}`, old_json: JSON.stringify({ prefixes: [prefix], deleted_by: RUN }), new_json: null },
      ])
    })
    it('a prefix a run left no band for (it had skips) stays staged beside a clean one; a later clean run takes it out', async () => {
      const { db, raw } = await gcsDb()
      const partly = `gs://${B1}/partly/`, clean = `gs://${B1}/clean/`
      raw.exec(`INSERT INTO deletion_bands (run_id, prefix, bytes, objects) VALUES ('${RUN}', '${clean}', 5, 1)`)
      for (const p of [partly, clean]) raw.exec(`INSERT INTO plan_items (plan_id, prefix, batch_id, added_by, added_ts) VALUES (1, '${p}', NULL, 'ann', 50)`)
      const removed = (await unstageDeleted(db, 1, [RUN])).filter(p => p === partly || p === clean)
      const items = () => raw.prepare(`SELECT prefix FROM plan_items WHERE plan_id = 1 AND prefix IN ('${partly}', '${clean}')`).all().map(r => (r as { prefix: string }).prefix)
      expect([removed, items()]).toEqual([[clean], [partly]])
      raw.exec(`INSERT INTO deletion_bands (run_id, prefix, bytes, objects) VALUES ('${RUN}', '${partly}', 8, 4)`)
      expect([await unstageDeleted(db, 1, [RUN]), items()]).toEqual([[partly], []])
    })
    it('consumes successful reviewed staging despite new/changed objects; keeps unrun and later-staged items; audits idempotently', async () => {
      const { db, raw } = await gcsDb()
      const done = `gs://${B1}/ckpt/b/`, drift = `gs://${B2}/tmp/a/`, ovw = `gs://${B1}/ow/`, unrun = `gs://${B1}/other/`, late = `gs://${B1}/late/`
      raw.exec(`INSERT INTO deletion_bands (run_id, prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects) VALUES ('${RUN}', '${ovw}', 5, 1, 0, 1, 0, 0), ('${RUN}', '${late}', 5, 1, 0, 0, 0, 0)`)
      raw.exec(`UPDATE deletion_bands SET drift_new_objects = 2 WHERE prefix = '${drift}'`)
      for (const [p, ts] of [[done, 50], [drift, 50], [ovw, 50], [unrun, 50], [late, 150]] as const) {
        raw.exec(`INSERT INTO plan_items (plan_id, prefix, batch_id, added_by, added_ts) VALUES (1, '${p}', NULL, 'ann', ${ts})`)
      }
      expect(await unstageDeleted(db, 1, [])).toEqual([])
      const first = await unstageDeleted(db, 1, [RUN])
      const second = await unstageDeleted(db, 1, [RUN])
      const items = raw.prepare('SELECT prefix FROM plan_items WHERE plan_id = 1 ORDER BY prefix').all().map(r => (r as { prefix: string }).prefix)
      const edits = raw.prepare("SELECT action, who, old_json, new_json FROM admin_edits WHERE tbl = 'plan_items' ORDER BY id").all()
      expect([first, second, items, edits]).toEqual([
        [done, ovw, drift],
        [],
        [late, unrun].sort(),
        [{ action: 'delete', who: `run:${RUN}`, old_json: JSON.stringify({ prefixes: [done, ovw, drift], deleted_by: RUN }), new_json: null }],
      ])
    })

    it('any number of succeeded runs (an executor poll passes every one; D1 binds ≤ 100 parameters a statement)', async () => {
      const { db, raw } = await gcsDb()
      const done = `gs://${B1}/ckpt/b/`
      raw.exec(`INSERT INTO plan_items (plan_id, prefix, batch_id, added_by, added_ts) VALUES (1, '${done}', NULL, 'ann', 50)`)
      const runs = [...Array.from({ length: 149 }, (_, i) => `r-${String(i).padStart(3, '0')}`), RUN]
      expect(await unstageDeleted(db, 1, runs)).toEqual([done])
    })

    it('does not consume dry or failed-summary staging', async () => {
      const { db, raw } = await gcsDb()
      const prefix = `gs://${B1}/ckpt/b/`
      raw.exec(`INSERT INTO plan_items (plan_id,prefix,added_by,added_ts) VALUES (1,'${prefix}','actor',1)`)
      raw.exec(`UPDATE deletion_runs SET mode='dry' WHERE run_id='${RUN}'`)
      expect(await unstageDeleted(db, 1, [RUN])).toEqual([])
      raw.exec(`UPDATE deletion_runs SET mode='real', plan_digest='' WHERE run_id='${RUN}'`)
      expect(await unstageDeleted(db, 1, [RUN])).toEqual([])
      expect(raw.prepare('SELECT prefix FROM plan_items').all()).toEqual([{ prefix }])
    })

    it('a deleted item counts as `deleted` (not `unstaged`) in its emptied batch', () => {
      const b = { id: 1, plan_id: 1, note: null, created_by: 'ann', created_ts: 10 }
      expect(emptiedBatches([b], [], [
        { action: 'insert', old_json: null, new_json: JSON.stringify({ staged: ['gs://b/a/', 'gs://b/c/'], covered: [], batch_id: 1, note: null }) },
        { action: 'delete', old_json: JSON.stringify({ prefixes: ['gs://b/a/'], deleted_by: 'r1' }), new_json: null },
        { action: 'delete', old_json: JSON.stringify({ prefixes: ['gs://b/c/'], own: true }), new_json: null },
      ])).toEqual([{ ...b, prefixes: ['gs://b/a/', 'gs://b/c/'], staged: 2, covered: 0, absorbed: [], unstaged: 1, deleted: 1 }])
    })
  })
})
