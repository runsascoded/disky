import { describe, expect, it } from 'vitest'
import { planSweepScript } from './planDispatch'

describe('plan-sweep dispatch script', () => {
  // `dt-cloud plan-sweep manifest` takes the plan as its positional PLAN_PATH (no `--plan` option: the job exited 2).
  it('passes the plan positionally, then executes (dry and for real)', () => {
    expect([false, true].map(real => planSweepScript('/gcs/d/sweep/cw/runs/j', real).split('\n'))).toEqual([false, true].map(real => [
      'set -euo pipefail',
      `trap 'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $SITE_TOKEN" "$SITE_URL/api/plan-sweep/jobs" || true' EXIT`,
      'RUN="/gcs/d/sweep/cw/runs/j"',
      'dt-cloud plan-sweep manifest "$RUN/plan.json" -d "$SWEEP_DATE" -o "$RUN"',
      `dt-cloud plan-sweep execute ${real ? '--for-real ' : ''}"$RUN"`,
    ]))
  })
})
