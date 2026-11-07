import { afterEach, describe, expect, it, vi } from 'vitest'
import { planBuckets } from './plans'
import type { ExecEnv } from './dispatch'
import { bucketCut, jobStampOf, planJsonObject, planJsonPath, RUN_ID_RE, runDir, sweep, sweepJobSpec, sweepScript, undoScript } from './sweepDispatch'
import { sqliteD1 } from './testD1'

const GCS = ['gcs-a', 'gcs-b', 'gcs-c']
const E1 = 'gcs-b'
const W4 = 'gcs-c'
const CFG = { dataBucket: 'my-data' }

describe('planBuckets — a plan\'s items grouped by bucket (no PlanSpansBuckets)', () => {
  it('two buckets: each with its items relative, buckets and items sorted, dupes folded', () => {
    expect(planBuckets([`gs://${E1}/ckpt/old/`, `gs://${W4}/tmp/x/`, `gs://${E1}/ckpt/a/`, `gs://${E1}/ckpt/old/`], GCS)).toEqual({
      [W4]: ['tmp/x/'],
      [E1]: ['ckpt/a/', 'ckpt/old/'],
    })
  })
  it('no items: no buckets', () => {
    expect(planBuckets([], GCS)).toEqual({})
  })
  it('an unknown bucket groups under the primary, as canonicalPrefix stored it', () => {
    expect(planBuckets(['gs://other/x/'], GCS)).toEqual({ 'gcs-a': ['other/x/'] })
  })
})

describe('bucketCut — the run\'s -b cut from a plan\'s buckets', () => {
  it('no request: every plan bucket', () => {
    expect(bucketCut([W4, E1], [])).toEqual([W4, E1])
  })
  it('a request: the intersection, in plan order', () => {
    expect(bucketCut([W4, E1], [E1, 'gcs-d'])).toEqual([E1])
  })
  it('disjoint: empty (the route 400s)', () => {
    expect(bucketCut([W4, E1], ['gcs-d'])).toEqual([])
  })
})

describe('sweepScript — the Batch container\'s bash', () => {
  const jobId = 'gcs-sweep-dry-20260928-1200z'
  // on exit, ping the site so the finished run is reflected (and posted to Slack) at once
  const trap = `trap 'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $SITE_TOKEN" "$SITE_URL/api/sweep/jobs" || true' EXIT`
  it('dry, one bucket: `manifest --plan <run>/plan.json` then execute', () => {
    expect(sweepScript({ cfg: CFG, mode: 'dry', jobId, buckets: [E1], plan: planJsonPath(CFG, jobId), store: 'gs://my-data', minSoftDeleteDays: 7 })).toBe([
      'set -euo pipefail',
      trap,
      `dt-cloud sweep manifest -d "$SWEEP_DATE" --plan "gs://my-data/sweep/runs/${jobId}/plan.json" -b ${E1} -i "gs://my-data" -o "gs://my-data/sweep/runs/${jobId}"`,
      `dt-cloud sweep execute -s 7 -b ${E1} "gs://my-data/sweep/runs/${jobId}"`,
    ].join('\n'))
  })
  it('real, the cut is the plan\'s buckets', () => {
    const reviewed = 'gs://my-data/sweep/runs/gcs-sweep-dry-20261004-020914z'
    expect(sweepScript({ cfg: CFG, mode: 'real', jobId, buckets: [W4, E1], plan: planJsonPath(CFG, jobId), store: 'gs://my-data', reviewed, minSoftDeleteDays: 7 })).toBe([
      'set -euo pipefail',
      trap,
      `dt-cloud sweep execute-reviewed -e xml -j 64 -B 2 -c guided -r 8000 -s 7 -b ${W4} -b ${E1} -p "gs://my-data/sweep/runs/${jobId}/plan.json" -o "gs://my-data/sweep/runs/${jobId}" -w /work/reviewed --for-real "${reviewed}"`,
    ].join('\n'))
  })
  it('uses one controller per selected bucket and supports an explicit adaptive deployment', () => {
    const reviewed = 'gs://my-data/sweep/runs/gcs-sweep-dry-20261004-020914z'
    expect(sweepScript({ cfg: CFG, mode: 'real', jobId, buckets: [E1], plan: 'gs://my-data/plan.json', store: 'gs://my-data', reviewed, pacing: 'adaptive', minSoftDeleteDays: 0 }).split('\n')).toEqual([
      'set -euo pipefail', trap,
      `dt-cloud sweep execute-reviewed -e xml -j 64 -B 1 -c adaptive -r 8000 -s 0 -b ${E1} -p "gs://my-data/plan.json" -o "gs://my-data/sweep/runs/${jobId}" -w /work/reviewed --for-real "${reviewed}"`,
    ])
  })
  it('real without a dry run of exactly its items: its own manifest, then the reviewed run\'s executor over that manifest\'s generations, with no relist', () => {
    const run = `gs://my-data/sweep/runs/${jobId}`
    expect(sweepScript({ cfg: CFG, mode: 'real', jobId, buckets: [W4, E1], plan: planJsonPath(CFG, jobId), store: 'gs://my-data', minSoftDeleteDays: 30 })).toBe([
      'set -euo pipefail',
      trap,
      `dt-cloud sweep manifest -d "$SWEEP_DATE" --plan "${run}/plan.json" -b ${W4} -b ${E1} -i "gs://my-data" -o "${run}/source"`,
      `dt-cloud sweep execute-manifest -e xml -j 64 -B 2 -c guided -r 8000 -s 30 -b ${W4} -b ${E1} -p "${run}/plan.json" -o "${run}" -w /work/manifest --for-real "${run}/source"`,
    ].join('\n'))
  })
  it('real without a dry run, explicit adaptive pacing, an R2 hold store: only the manifest reads through `r2_profile`', () => {
    const run = `gs://my-data/sweep/runs/${jobId}`
    const R2_MAIN = `python -c 'from os import environ; from dt_cloud.scan_steps import r2_profile; env = dict(environ); r2_profile(env); environ.update(env); from dt_cloud.cli import main; main()'`
    expect(sweepScript({ cfg: CFG, mode: 'real', jobId, buckets: [E1], plan: planJsonPath(CFG, jobId), store: 'r2://my-index', pacing: 'adaptive', minSoftDeleteDays: 7 }).split('\n')).toEqual([
      'set -euo pipefail',
      trap,
      `${R2_MAIN} sweep manifest -d "$SWEEP_DATE" --plan "${run}/plan.json" -b ${E1} -i "r2://my-index" -o "${run}/source"`,
      `dt-cloud sweep execute-manifest -e xml -j 64 -B 1 -c adaptive -r 8000 -s 7 -b ${E1} -p "${run}/plan.json" -o "${run}" -w /work/manifest --for-real "${run}/source"`,
    ])
  })
  it('undo: `sweep undo` of the job\'s TARGET_RUN, behind the same exit trap', () => {
    expect(undoScript()).toBe([
      'set -euo pipefail',
      trap,
      'dt-cloud sweep undo "$TARGET_RUN"',
    ].join('\n'))
    expect(undoScript({ bulk: true })).toBe([
      'set -euo pipefail',
      trap,
      'dt-cloud sweep undo -B "$TARGET_RUN"',
    ].join('\n'))
  })
  it('run dir + plan.json paths agree (gs:// for the executor, the object name for the upload)', () => {
    expect(runDir(CFG, jobId)).toBe(`gs://my-data/sweep/runs/${jobId}`)
    expect(planJsonPath(CFG, jobId)).toBe(`gs://my-data/sweep/runs/${jobId}/plan.json`)
    expect(planJsonObject(jobId)).toBe(`sweep/runs/${jobId}/plan.json`)
  })
})

describe('sweepJobSpec — the Batch spec every gcs executor job shares (a run, an undo)', () => {
  it('the image runs the script as the job account, beside its buckets, per-job env first', () => {
    const cfg = { project: 'my-project', image: 'img:1', cfAccountId: 'acct', dataBucket: 'my-data', d1DbId: 'd1-id', d1DbName: 'my-db' }
    expect(sweepJobSpec({
      cfg, jobSa: 'job@my-project.iam.gserviceaccount.com', region: 'us-east1', script: 'echo hi',
      actor: 'ann', siteUrl: 'https://site.example', env: { OP: 'undo', TARGET_RUN: '2026-09-28-p1/20260928T120000Z' },
    })).toEqual({
      taskGroups: [{
        taskCount: 1,
        taskSpec: {
          runnables: [{ container: { imageUri: 'img:1', entrypoint: '/bin/bash', commands: ['-c', 'echo hi'] } }],
          computeResource: { cpuMilli: 8000, memoryMib: 60000 },
          maxRetryCount: 0,
          maxRunDuration: '259200s',
          environment: {
            variables: {
              OP: 'undo', TARGET_RUN: '2026-09-28-p1/20260928T120000Z',
              USER: 'ann', CLOUDFLARE_ACCOUNT_ID: 'acct', DATA_BUCKET: 'my-data', D1_DB_ID: 'd1-id', D1_DB_NAME: 'my-db', SITE_URL: 'https://site.example',
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
    })
  })
  it('job stamps and run ids', () => {
    expect(jobStampOf(new Date('2026-10-03T20:44:48.123Z'))).toBe('20261003-204448')
    expect(['2026-09-28-p1/20260928T120000Z', '2026-09-28-p12/20260928T120000Z', 'gcs-sweep-real-20260928-120000z', '2026-09-28-p1', '2026-09-28-p1/x'].map(r => RUN_ID_RE.test(r)))
      .toEqual([true, true, false, false, false])
  })
})

describe('a dispatched run\'s hold store (`sweep manifest -i`)', () => {
  const B1 = 'my-bucket'
  const R2_MAIN = `python -c 'from os import environ; from dt_cloud.scan_steps import r2_profile; env = dict(environ); r2_profile(env); environ.update(env); from dt_cloud.cli import main; main()'`
  const SHARED_VARS = {
    USER: 'ann', CLOUDFLARE_ACCOUNT_ID: 'acct', DATA_BUCKET: 'my-data', D1_DB_ID: 'd1-id', D1_DB_NAME: 'my-db', SITE_URL: 'https://site.test',
  }
  const SHARED_SECRETS = {
    SITE_TOKEN: 'projects/my-project/secrets/gcs-sheet-sync-token/versions/latest',
    CLOUDFLARE_API_TOKEN: 'projects/my-project/secrets/cf-pages-token/versions/latest',
  }
  type Labels = Record<string, string>
  interface Submitted {
    url: string
    spec: { labels?: Labels; allocationPolicy: { labels?: Labels; location: { allowedLocations: string[] } } }
    commands: string[]
    environment: { variables: Record<string, string>; secretVariables: Record<string, string> }
  }
  const saKey = async (): Promise<string> => {
    const { privateKey } = await crypto.subtle.generateKey(
      { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify'],
    ) as CryptoKeyPair
    const der = new Uint8Array(await crypto.subtle.exportKey('pkcs8', privateKey) as ArrayBuffer)
    let s = ''
    for (const b of der) s += String.fromCharCode(b)
    return JSON.stringify({ client_email: 'dispatch@my-project.iam.gserviceaccount.com', private_key: `-----BEGIN PRIVATE KEY-----\n${btoa(s)}\n-----END PRIVATE KEY-----\n` })
  }
  afterEach(() => { vi.unstubAllGlobals() })

  /** `sweep.prepare` under `extra`, a one-item plan on `B1` staged, and every Batch submit it makes. */
  const prepared = async (extra: Partial<ExecEnv>) => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'a', 1)").run()
    await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'a', 1)").bind(`gs://${B1}/a/`).run()
    const submitted: Submitted[] = []
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (url.includes('oauth2')) return new Response(JSON.stringify({ access_token: 't', expires_in: 3600 }))
      if (url.includes('batch.googleapis.com')) {
        const spec = JSON.parse(String(init?.body))
        const { taskSpec } = spec.taskGroups[0]
        submitted.push({ url, spec, commands: taskSpec.runnables[0].container.commands, environment: taskSpec.environment })
      }
      return new Response('{}')
    })
    const env = {
      DB: db, GCP_SA_KEY: await saKey(), JOB_SA: 'job@my-project.iam.gserviceaccount.com', GCP_PROJECT: 'my-project', BATCH_REGION: 'my-region', DATA_BUCKET: 'my-data', SWEEP_IMAGE: 'img:1',
      CF_ACCOUNT_ID: 'acct', D1_DB_ID: 'd1-id', D1_DB_NAME: 'my-db', STORE_SCHEME: 'gs', STORE_BUCKETS: B1, ...extra,
    } as ExecEnv
    return { prepared: await sweep.prepare(env, db, { planId: 1, mode: 'dry', actor: 'ann', siteUrl: 'https://site.test' }), submitted }
  }

  /** The one job `sweep.prepare` + `launch` submit under `extra`. */
  const launched = async (extra: Partial<ExecEnv>) => {
    const { prepared: p, submitted } = await prepared(extra)
    if (!('launch' in p)) throw new Error(JSON.stringify(p))
    const job = await p.launch('2026-11-03T1622', 'digest', undefined)
    if (!('job_id' in job)) throw new Error(JSON.stringify(job))
    expect(submitted.length).toBe(1)
    return { job, ...submitted[0] }
  }

  /** That job's script's manifest line, and its env. */
  const dispatched = async (extra: Partial<ExecEnv>) => {
    const { job, commands, environment } = await launched(extra)
    const run = `gs://my-data/sweep/runs/${job.job_id}`
    return {
      manifest: commands[1].split('\n').filter(l => l.includes('sweep manifest')).map(l => l.replaceAll(run, '<run>')),
      variables: environment.variables,
      secretVariables: environment.secretVariables,
    }
  }

  describe('the region it is submitted to', () => {
    const where = async (extra: Partial<ExecEnv>) => {
      const { job, url, spec } = await launched(extra)
      return [url.replace(job.job_id, '<job>'), spec.allocationPolicy.location, job.extra.region]
    }
    const at = (region: string) => [
      `https://batch.googleapis.com/v1/projects/my-project/locations/${region}/jobs?job_id=<job>`, { allowedLocations: [`regions/${region}`] }, region,
    ]
    it('beside its bucket (`BUCKET_REGIONS`), with or without a deployment region', async () => {
      const regions = JSON.stringify({ [B1]: 'us-east5' })
      expect(await where({ BUCKET_REGIONS: regions })).toEqual(at('us-east5'))
      expect(await where({ BUCKET_REGIONS: regions, BATCH_REGION: undefined })).toEqual(at('us-east5'))
    })
    it('a bucket `BUCKET_REGIONS` does not place: the deployment\'s (`BATCH_REGION`)', async () => {
      expect(await where({})).toEqual(at('my-region'))
      expect(await where({ BUCKET_REGIONS: JSON.stringify({ other: 'us-east5' }) })).toEqual(at('my-region'))
    })
    it('neither names one: refused before anything is submitted (there is no default region)', async () => {
      const { prepared: p, submitted } = await prepared({ BATCH_REGION: undefined })
      expect([p, submitted]).toEqual([{ ok: false, status: 503, error: 'dispatch not configured (BATCH_REGION unset)', extra: { buckets: [B1] } }, []])
    })
  })

  describe('the cost labels it is submitted with', () => {
    const labels = async (extra: Partial<ExecEnv>) => {
      const { spec } = await launched(extra)
      return [spec.labels, spec.allocationPolicy.labels]
    }
    it('`DISKY_LABELS` + `component: sweep`, on the job and on its allocation policy (what its VM and disks bill under)', async () => {
      const want = { app: 'disky', deployment: 'gcs', component: 'sweep' }
      expect(await labels({ DISKY_LABELS: 'app=disky, deployment=gcs' })).toEqual([want, want])
    })
    it('no `DISKY_LABELS`: none on either', async () => {
      expect(await labels({})).toEqual([undefined, undefined])
    })
  })

  it('no review store: the data bucket, no credentials', async () => {
    expect(await dispatched({})).toEqual({
      manifest: [`dt-cloud sweep manifest -d "$SWEEP_DATE" --plan "<run>/plan.json" -b ${B1} -i "gs://my-data" -o "<run>"`],
      variables: { SWEEP_DATE: '2026-11-03T1622', PLAN_ID: '1', PLAN_DIGEST: 'digest', ...SHARED_VARS },
      secretVariables: SHARED_SECRETS,
    })
  })

  it('an R2 review store: read through `r2_profile`, with its bucket and credential references', async () => {
    expect(await dispatched({
      SWEEP_REVIEW_STORE: 'r2://my-index',
      SWEEP_REVIEW_SECRETS: JSON.stringify({ R2_ENDPOINT: 'endpoint', R2_ACCESS_KEY_ID: 'reader-key', R2_SECRET_ACCESS_KEY: 'reader-secret' }),
    })).toEqual({
      manifest: [`${R2_MAIN} sweep manifest -d "$SWEEP_DATE" --plan "<run>/plan.json" -b ${B1} -i "r2://my-index" -o "<run>"`],
      variables: { SWEEP_DATE: '2026-11-03T1622', PLAN_ID: '1', PLAN_DIGEST: 'digest', SWEEP_INTERVAL_STORE: 'r2://my-index', R2_BUCKET: 'my-index', ...SHARED_VARS },
      secretVariables: {
        R2_ENDPOINT: 'projects/my-project/secrets/endpoint/versions/latest',
        R2_ACCESS_KEY_ID: 'projects/my-project/secrets/reader-key/versions/latest',
        R2_SECRET_ACCESS_KEY: 'projects/my-project/secrets/reader-secret/versions/latest',
        ...SHARED_SECRETS,
      },
    })
  })

  it('an R2 review store without its credential references is refused', async () => {
    await expect(dispatched({ SWEEP_REVIEW_STORE: 'r2://my-index' })).rejects.toThrow('R2 sweep review requires endpoint and credential secret bindings')
  })
})
