// What the plan-sweep executor's three submitters post to Batch: each job in the deployment's region (`BATCH_REGION`),
// carrying `DISKY_LABELS` + its `component` on the job's `labels` and on `allocationPolicy.labels` (the level its VM and
// disks bill under). Unset `DISKY_LABELS`: neither level carries any.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { onRequestPost as purgeRoute } from '../api/plan-sweep/purge'
import { onRequestPost as undoRoute } from '../api/plan-sweep/undo'
import { PLAN_SWEEP_RUN_INSERT, planSweep } from './planDispatch'
import { RUN_DIR, sqliteD1 } from './testD1'

type Labels = Record<string, string>
interface Posted { url: string; labels?: Labels; allocation?: Labels; location: unknown }

const B1 = 'my-bucket'
const RUN = 'cw-sweep-real-20261104-010203z'
const DEPLOY = { app: 'disky', deployment: 'cw' }
const NOW = Date.parse('2026-11-05T00:00:00Z')

const saKey = async (): Promise<string> => {
  const { privateKey } = await crypto.subtle.generateKey(
    { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify'],
  ) as CryptoKeyPair
  const der = new Uint8Array(await crypto.subtle.exportKey('pkcs8', privateKey) as ArrayBuffer)
  let s = ''
  for (const b of der) s += String.fromCharCode(b)
  return JSON.stringify({ client_email: 'dispatch@my-project.iam.gserviceaccount.com', private_key: `-----BEGIN PRIVATE KEY-----\n${btoa(s)}\n-----END PRIVATE KEY-----\n` })
}

// plan-sweep runs where the cw schema is (`deletion_runs.manifest`); gcs's lineage names that column `plan`.
describe.skipIf(RUN_DIR !== 'manifest')('plan-sweep jobs, as posted to Batch', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

  /** A D1 with one staged plan and one finished real run; an env naming every key the routes need (+ `extra`);
   *  and every job posted to Batch while `act` runs, its job id replaced by `<job>`. */
  const posted = async (extra: Record<string, string>, act: (env: never, db: D1Database) => Promise<unknown>): Promise<Posted[]> => {
    vi.useFakeTimers({ now: NOW, toFake: ['Date'] })
    const { db, raw } = await sqliteD1()
    raw.exec(`INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann@example.test', 1)`)
    raw.exec(`INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, 's3://${B1}/a/', 'ann@example.test', 1)`)
    await db.prepare(PLAN_SWEEP_RUN_INSERT).bind(RUN, 1, `gs://my-data/sweep/cw/runs/${RUN}`, '2026-11-04', 'ann@example.test', 'real', 100, `gs://my-data/sweep/cw/runs/${RUN}`, 'dg').run()
    const out: Posted[] = []
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (url.includes('oauth2')) return new Response(JSON.stringify({ access_token: 't', expires_in: 3600 }))
      if (url.includes('batch.googleapis.com')) {
        const spec = JSON.parse(String(init?.body)) as { labels?: Labels; allocationPolicy: { labels?: Labels; location: unknown } }
        out.push({ url: url.replace(/job_id=.*$/, 'job_id=<job>'), labels: spec.labels, allocation: spec.allocationPolicy.labels, location: spec.allocationPolicy.location })
      }
      return new Response('{}')
    })
    const env = {
      DB: db, GCP_SA_KEY: await saKey(), JOB_SA: 'job@my-project.iam.gserviceaccount.com', GCP_PROJECT: 'my-project', BATCH_REGION: 'us-east1',
      DATA_BUCKET: 'my-data', SWEEP_IMAGE: 'img:1', SWEEP_S3_ENDPOINT: 'https://s3.example.com', STORE_SCHEME: 's3://', STORE_BUCKETS: B1, ...extra,
    }
    await act(env as never, db)
    return out
  }
  const post = (path: string) => new Request(`http://localhost${path}`, { method: 'POST', body: JSON.stringify({ run_id: RUN }) })

  const submitters: Record<string, (env: never, db: D1Database) => Promise<unknown>> = {
    'sweep': async (env, db) => {
      const prepared = await planSweep.prepare(env, db, { planId: 1, mode: 'dry', actor: 'ann@example.test', siteUrl: 'https://site.test' })
      if (!('launch' in prepared)) throw new Error(JSON.stringify(prepared))
      const launched = await prepared.launch('2026-11-04', 'digest', undefined)
      if (!('job_id' in launched)) throw new Error(JSON.stringify(launched))
    },
    'sweep-undo': async (env, db) => {
      await db.prepare('UPDATE deletion_runs SET undo_deadline = ? WHERE run_id = ?').bind(NOW / 1000 + 3600, RUN).run()
      const r = await undoRoute({ request: post('/api/plan-sweep/undo'), env } as never)
      if (r.status !== 200) throw new Error(await r.text())
    },
    'sweep-purge': async (env, db) => {
      await db.prepare(`UPDATE deletion_runs SET undo_deadline = ?, purge_state = 'pending' WHERE run_id = ?`).bind(NOW / 1000 - 3600, RUN).run()
      const r = await purgeRoute({ request: post('/api/plan-sweep/purge'), env } as never)
      if (r.status !== 200) throw new Error(await r.text())
    },
  }
  const at = { url: 'https://batch.googleapis.com/v1/projects/my-project/locations/us-east1/jobs?job_id=<job>', location: { allowedLocations: ['regions/us-east1'] } }

  it.each(Object.keys(submitters))('%s: `DISKY_LABELS` + its component, on the job and on its allocation policy', async component => {
    const labels = { ...DEPLOY, component }
    expect(await posted({ DISKY_LABELS: 'app=disky,deployment=cw' }, submitters[component])).toEqual([{ ...at, labels, allocation: labels }])
  })
  it.each(Object.keys(submitters))('%s: no `DISKY_LABELS`, no labels on either', async component => {
    expect(await posted({}, submitters[component])).toEqual([{ ...at, labels: undefined, allocation: undefined }])
  })
})
