import { describe, expect, it } from 'vitest'
import { planBuckets } from './plans'
import { bucketCut, planJsonObject, planJsonPath, runDir, sweepScript } from './sweepDispatch'

const GCS = ['marin-us-central2', 'marin-us-east1', 'marin-eu-west4']
const E1 = 'marin-us-east1'
const W4 = 'marin-eu-west4'

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
    expect(planBuckets(['gs://other/x/'], GCS)).toEqual({ 'marin-us-central2': ['other/x/'] })
  })
})

describe('bucketCut — the run\'s -b cut from a plan\'s buckets', () => {
  it('no request: every plan bucket', () => {
    expect(bucketCut([W4, E1], [])).toEqual([W4, E1])
  })
  it('a request: the intersection, in plan order', () => {
    expect(bucketCut([W4, E1], [E1, 'marin-us-west4'])).toEqual([E1])
  })
  it('disjoint: empty (the route 400s)', () => {
    expect(bucketCut([W4, E1], ['marin-us-west4'])).toEqual([])
  })
})

describe('sweepScript — the Batch container\'s bash', () => {
  const jobId = 'gcs-sweep-dry-20260928-1200z'
  // on exit, ping the site so the finished run is reflected (and posted to Slack) at once
  const trap = `trap 'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $GCS_USAGE_TOKEN" "$SITE_URL/api/sweep/jobs" || true' EXIT`
  it('dry, one bucket: `manifest --plan <run>/plan.json` then execute', () => {
    expect(sweepScript({ mode: 'dry', jobId, buckets: [E1], plan: planJsonPath(jobId) })).toBe([
      'set -euo pipefail',
      trap,
      `dt-cloud sweep manifest -d "$SWEEP_DATE" --plan "gs://oa-gcs-usage-dvx/sweep/runs/${jobId}/plan.json" -b ${E1} -o "gs://oa-gcs-usage-dvx/sweep/runs/${jobId}"`,
      `dt-cloud sweep execute -b ${E1} "gs://oa-gcs-usage-dvx/sweep/runs/${jobId}"`,
    ].join('\n'))
  })
  it('real, the cut is the plan\'s buckets', () => {
    expect(sweepScript({ mode: 'real', jobId, buckets: [W4, E1], plan: planJsonPath(jobId) })).toBe([
      'set -euo pipefail',
      trap,
      `dt-cloud sweep manifest -d "$SWEEP_DATE" --plan "gs://oa-gcs-usage-dvx/sweep/runs/${jobId}/plan.json" -b ${W4} -b ${E1} -o "gs://oa-gcs-usage-dvx/sweep/runs/${jobId}"`,
      `dt-cloud sweep execute -b ${W4} -b ${E1} --for-real "gs://oa-gcs-usage-dvx/sweep/runs/${jobId}"`,
    ].join('\n'))
  })
  it('run dir + plan.json paths agree (gs:// for the executor, the object name for the upload)', () => {
    expect(runDir(jobId)).toBe(`gs://oa-gcs-usage-dvx/sweep/runs/${jobId}`)
    expect(planJsonPath(jobId)).toBe(`gs://oa-gcs-usage-dvx/sweep/runs/${jobId}/plan.json`)
    expect(planJsonObject(jobId)).toBe(`sweep/runs/${jobId}/plan.json`)
  })
})
