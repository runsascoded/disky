import { describe, expect, it } from 'vitest'
import { planBuckets } from './plans'
import { bucketCut, jobStampOf, planJsonObject, planJsonPath, RUN_ID_RE, runDir, sweepJobSpec, sweepScript, undoScript } from './sweepDispatch'

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
    expect(sweepScript({ cfg: CFG, mode: 'dry', jobId, buckets: [E1], plan: planJsonPath(CFG, jobId) })).toBe([
      'set -euo pipefail',
      trap,
      `dt-cloud sweep manifest -d "$SWEEP_DATE" --plan "gs://my-data/sweep/runs/${jobId}/plan.json" -b ${E1} -o "gs://my-data/sweep/runs/${jobId}"`,
      `dt-cloud sweep execute -b ${E1} "gs://my-data/sweep/runs/${jobId}"`,
    ].join('\n'))
  })
  it('real, the cut is the plan\'s buckets', () => {
    const reviewed = 'gs://my-data/sweep/runs/gcs-sweep-dry-20261004-020914z'
    expect(sweepScript({ cfg: CFG, mode: 'real', jobId, buckets: [W4, E1], plan: planJsonPath(CFG, jobId), reviewed })).toBe([
      'set -euo pipefail',
      trap,
      `dt-cloud sweep execute-reviewed -e xml -j 64 -B 2 -c guided -r 8000 -b ${W4} -b ${E1} -p "gs://my-data/sweep/runs/${jobId}/plan.json" -o "gs://my-data/sweep/runs/${jobId}" -w /work/reviewed --for-real "${reviewed}"`,
    ].join('\n'))
  })
  it('uses one controller per selected bucket and supports an explicit adaptive deployment', () => {
    const reviewed = 'gs://my-data/sweep/runs/gcs-sweep-dry-20261004-020914z'
    expect(sweepScript({ cfg: CFG, mode: 'real', jobId, buckets: [E1], plan: 'gs://my-data/plan.json', reviewed, pacing: 'adaptive' }).split('\n')).toEqual([
      'set -euo pipefail', trap,
      `dt-cloud sweep execute-reviewed -e xml -j 64 -B 1 -c adaptive -r 8000 -b ${E1} -p "gs://my-data/plan.json" -o "gs://my-data/sweep/runs/${jobId}" -w /work/reviewed --for-real "${reviewed}"`,
    ])
  })
  it('refuses real without a reviewed manifest, rather than silently regenerating it', () => {
    expect(() => sweepScript({ cfg: CFG, mode: 'real', jobId, buckets: [E1], plan: planJsonPath(CFG, jobId) }))
      .toThrow('real dispatch requires its reviewed DR manifest')
  })
  it('undo: `sweep undo` of the job\'s TARGET_RUN, behind the same exit trap', () => {
    expect(undoScript()).toBe([
      'set -euo pipefail',
      trap,
      'dt-cloud sweep undo "$TARGET_RUN"',
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
