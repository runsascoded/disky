// specs/scan-runs-ui.md: /api/scan-runs over the cw lineage's migration (FKs on).
import { describe, expect, it } from 'vitest'
import { onRequestGet as route } from '../api/scan-runs/[[path]]'
import { sqliteD1 } from './testD1'

const SEED = `
INSERT INTO scan_runs (run_id, scan, kind, parent, status, started_ts, finished_ts, job_name, region, cost_usd, source, updated_ts, store) VALUES
  ('a', '2026-10-07', 'scan', NULL, 'succeeded', 100, 400, 'job-a', 'r1', 1.5, 'backfill', 0, 'primary'),
  ('a-l', '2026-10-07', 'listing', 'a', 'succeeded', 110, 300, 'job-al', 'r1', 0.5, 'backfill', 0, 'primary'),
  ('b', '2026-10-08', 'scan', NULL, 'failed', 1000, 1300, 'job-b', 'r1', NULL, 'job', 0, 'primary'),
  ('m', '2026-10-08', 'scan', NULL, 'succeeded', 1000, 1300, 'job-m', 'r1', NULL, 'job', 0, 'meta');
INSERT INTO scan_run_phases (run_id, phase, seq, started_ts, finished_ts) VALUES
  ('a', 'listing', 0, 100, 300), ('a', 'index', 1, 300, 390), ('b', 'listing', 0, 1000, 1200);
INSERT INTO scan_run_outputs (run_id, key, uri, bytes, objects, rows) VALUES
  ('a', 'listing', 'gs://d/listing/2026-10-07/', 100, 3, 9), ('b', 'listing', 'gs://d/listing/2026-10-08/', 160, 3, 12);
`

const get = (db: unknown, path: string, env: object = {}) =>
  route({ request: new Request(`http://localhost/api/scan-runs${path ? `/${path}` : ''}`), env: { DB: db, GCP_PROJECT: 'proj', META_TREE_URL: 'https://h.test/meta', ...env }, params: path ? { path: path.split('/') } : {} } as never)

describe('GET /api/scan-runs', () => {
  it('lists the store\'s orchestrating runs with their totals and links', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(SEED)
    const r = await get(db, '')
    const body = (await r.json()) as { configured: boolean; project: string; meta: string; runs: { run: { run_id: string }; out_bytes: number; delta_bytes: number | null; downstream: number; cost_usd: number; failed_at: string | null }[] }
    expect([r.status, body.configured, body.project, body.meta]).toEqual([200, true, 'proj', 'https://h.test/meta'])
    expect(body.runs.map(s => [s.run.run_id, s.out_bytes, s.delta_bytes, s.downstream, s.cost_usd, s.failed_at])).toEqual([
      ['b', 160, 60, 0, null, 'after listing'],
      ['a', 100, null, 1, 2, null],
    ])
  })
  it('one run: phases, outputs with deltas, downstream jobs; 404 for an unknown id', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(SEED)
    const r = await get(db, 'a')
    const body = (await r.json()) as { run: { run_id: string }; phases: { phase: string }[]; outputs: { key: string; delta_bytes: number | null }[]; downstream: { run_id: string }[]; siblings: unknown[]; prev_scan: string | null; next_scan: string | null }
    expect([r.status, body.run.run_id, body.phases.map(p => p.phase), body.outputs.map(o => [o.key, o.delta_bytes]), body.downstream.map(d => d.run_id), body.siblings, body.prev_scan, body.next_scan]).toEqual([
      200, 'a', ['listing', 'index'], [['listing', null]], ['a-l'], [], null, '2026-10-08',
    ])
    const missing = await get(db, 'nope')
    expect([missing.status, await missing.json()]).toEqual([404, { error: 'no scan run nope' }])
  })
  it('a D1 without the tables answers configured: false', async () => {
    const { db } = await sqliteD1('cw', { before: '0016_scan_runs.sql' })
    const r = await get(db, '')
    expect([r.status, await r.json()]).toEqual([200, { configured: false, runs: [], project: 'proj', meta: 'https://h.test/meta' }])
  })
})
