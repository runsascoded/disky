import { describe, expect, it } from 'vitest'
import { PLAN_SWEEP_RUN_INSERT, planSweepScript, reviewConfig } from './planDispatch'
import { RUN_DIR, sqliteD1 } from './testD1'

describe('plan-sweep dispatch script', () => {
  // `dt-cloud plan-sweep manifest` takes the plan as its positional PLAN_PATH (no `--plan` option: the job exited 2).
  it('passes the plan positionally, then executes (dry and for real)', () => {
    expect([false, true].map(real => planSweepScript('/gcs/d/sweep/cw/runs/j', real).split('\n'))).toEqual([false, true].map(real => [
      'set -euo pipefail',
      `trap 'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $SITE_TOKEN" "$SITE_URL/api/plan-sweep/jobs" || true' EXIT`,
      'RUN="/gcs/d/sweep/cw/runs/j"',
      `python -c 'from os import environ; from dt_cloud.scan_steps import r2_profile; env = dict(environ); r2_profile(env); environ.update(env); from dt_cloud.cli import main; main()' plan-sweep manifest "$RUN/plan.json" -d "$SWEEP_DATE" -o "$RUN"`,
      `dt-cloud plan-sweep execute ${real ? '--for-real ' : ''}"$RUN"`,
    ]))
  })
})

describe('plan-sweep run row', () => {
  // plan-sweep runs where the cw schema is (`deletion_runs.manifest`); gcs's lineage names that column `plan`.
  it.skipIf(RUN_DIR !== 'manifest')('lands in the cw schema, its run dir as `manifest`', async () => {
    const { db, raw } = await sqliteD1()
    raw.exec(`INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (7, 'Staged', 'open', 'ann@example.test', 1)`)
    await db.prepare(PLAN_SWEEP_RUN_INSERT).bind('j1', 7, 'gs://d/sweep/cw/runs/j1', '2026-10-14', 'ann@example.test', 'dry', 100, 'gs://d/sweep/cw/runs/j1', 'dg').run()
    expect(raw.prepare('SELECT run_id, plan_id, manifest, scan, head, exec_head, mode, plan_digest FROM deletion_runs').all().map(r => ({ ...r }))).toEqual([
      { run_id: 'j1', plan_id: 7, manifest: 'gs://d/sweep/cw/runs/j1', scan: '2026-10-14', head: 0, exec_head: 0, mode: 'dry', plan_digest: 'dg' },
    ])
  })
})


describe('retired sweep review inputs', () => {
  const cfg = { project: 'p' } as Parameters<typeof reviewConfig>[1]
  it('keeps local deployments free of review credentials', () => {
    expect(reviewConfig({}, cfg)).toEqual({ vars: {}, secrets: {} })
  })
  it('passes the active R2 store with separate credential references', () => {
    expect(reviewConfig({ SWEEP_REVIEW_STORE: 'r2://index', SWEEP_REVIEW_SECRETS: JSON.stringify({
      R2_ENDPOINT: 'endpoint', R2_ACCESS_KEY_ID: 'reader-key', R2_SECRET_ACCESS_KEY: 'reader-secret',
    }) }, cfg)).toEqual({
      vars: { SWEEP_INTERVAL_STORE: 'r2://index', R2_BUCKET: 'index' },
      secrets: {
        R2_ENDPOINT: 'projects/p/secrets/endpoint/versions/latest',
        R2_ACCESS_KEY_ID: 'projects/p/secrets/reader-key/versions/latest',
        R2_SECRET_ACCESS_KEY: 'projects/p/secrets/reader-secret/versions/latest',
      },
    })
  })
  it('refuses an R2 source without its own credential references', () => {
    expect(() => reviewConfig({ SWEEP_REVIEW_STORE: 'r2://index' }, cfg)).toThrow('R2 sweep review requires endpoint and credential secret bindings')
  })
})
