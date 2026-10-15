// specs/done/scan-runs-ui.md: /api/scan-runs over the D1 schema (FKs on).
import { describe, expect, it, vi } from 'vitest'
import { onRequestGet as route } from '../api/scan-runs/[[path]]'
import { sqliteD1 } from './testD1'

describe('D1', () => {
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

  const get = (db: unknown, path: string, env: object = {}, query = '') =>
    route({ request: new Request(`http://localhost/api/scan-runs${path ? `/${path}` : ''}${query}`), env: { DB: db, GCP_PROJECT: 'proj', META_TREE_URL: 'https://h.test/meta', ...env }, params: path ? { path: path.split('/') } : {} } as never)

  describe('GET /api/scan-runs', () => {
    it('returns active overlapping spans to the same request clock on detail and list', async () => {
      const clock = vi.spyOn(Date, 'now').mockReturnValue(400_000)
      try {
        const { db, raw } = await sqliteD1()
        raw.exec(SEED)
        raw.exec(`
        UPDATE scan_runs SET status = 'running', finished_ts = NULL WHERE run_id = 'a';
        INSERT INTO scan_run_phases (run_id, phase, seq, started_ts, finished_ts, status)
          VALUES ('a', 'static-names', 2, 305, NULL, 'running');
      `)
        const detail = await (await get(db, 'a')).json() as import('./scanRuns').ScanRunDetail
        const list = await (await get(db, '')).json() as import('./scanRuns').ScanRunsList
        const active = [{ phase: 'static-names', start: 205, end: 300, status: 'running', note: null, lane: 1 }]
        expect(detail.spans.filter(p => p.status === 'running')).toEqual(active)
        expect(list.runs.find(r => r.run.run_id === 'a')?.spans.filter(p => p.status === 'running')).toEqual(active)
      } finally {
        clock.mockRestore()
      }
    })
    it('preserves a historical timeout and exposes its linked recovery separately', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec(SEED)
      raw.exec(`
      UPDATE scan_run_phases SET note = 'failed (exit 124)' WHERE run_id = 'a' AND phase = 'index';
      INSERT INTO scan_runs (run_id, scan, kind, parent, status, started_ts, finished_ts, source, updated_ts, store)
        VALUES ('retry', '2026-10-07', 'phase-recovery', 'a', 'succeeded', 500, 600, 'backfill', 600, 'primary');
      INSERT INTO scan_run_phases (run_id, phase, seq, started_ts, finished_ts, status, note)
        VALUES ('retry', 'index', 0, 500, 600, 'done', 'Reused earlier stages');
    `)
      const r = await get(db, 'a')
      const b = await r.json() as import('./scanRuns').ScanRunDetail
      expect({ secs: b.secs, spans: b.spans.map(p => [p.phase, p.start, p.end, p.status]),
        issues: b.phase_issues.map(i => [i.phase, i.outcome, i.recovery?.run_id]),
        recoveries: b.recoveries.map(a => [a.run.run_id, a.secs, a.phases.map(p => p.phase)]),
      }).toEqual({ secs: 300, spans: [['listing', 0, 200, 'done'], ['index', 200, 290, 'failed']],
        issues: [['index', 'timed out', 'retry']], recoveries: [['retry', 100, ['index']]],
      })
      const list = await (await get(db, '')).json() as import('./scanRuns').ScanRunsList
      expect(list.runs.map(s => [s.run.run_id, s.phase_issues.map(i => [i.phase, i.recovery?.run_id])])).toEqual([
        ['b', []], ['a', [['index', 'retry']]],
      ])
    })
    it('lists the store\'s orchestrating runs with their totals and links', async () => {
      const { db, raw } = await sqliteD1()
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
      const { db, raw } = await sqliteD1()
      raw.exec(SEED)
      const r = await get(db, 'a')
      const body = (await r.json()) as { run: { run_id: string }; phases: { phase: string }[]; outputs: { key: string; delta_bytes: number | null }[]; downstream: { run_id: string }[]; siblings: unknown[]; prev_scan: string | null; next_scan: string | null }
      expect([r.status, body.run.run_id, body.phases.map(p => p.phase), body.outputs.map(o => [o.key, o.delta_bytes]), body.downstream.map(d => d.run_id), body.siblings, body.prev_scan, body.next_scan]).toEqual([
        200, 'a', ['listing', 'index'], [['listing', null]], ['a-l'], [], null, '2026-10-08',
      ])
      const missing = await get(db, 'nope')
      expect([missing.status, await missing.json()]).toEqual([404, { error: 'no scan run nope' }])
    })
    it('`store=<key>` reads that secondary store\'s runs; an unknown store is a 404', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec(SEED)
      const env = { STORES_JSON: JSON.stringify({ meta: {} }) }
      const primary = await (await get(db, '', env)).json() as import('./scanRuns').ScanRunsList
      const meta = await (await get(db, '', env, '?store=meta')).json() as import('./scanRuns').ScanRunsList
      expect([primary.runs.map(s => s.run.run_id), meta.runs.map(s => s.run.run_id)]).toEqual([['b', 'a'], ['m']])
      const one = await get(db, 'm', env, '?store=meta')
      expect([one.status, ((await one.json()) as import('./scanRuns').ScanRunDetail).run.run_id]).toEqual([200, 'm'])
      const other = await get(db, 'a', env, '?store=meta')
      expect([other.status, await other.json()]).toEqual([404, { error: 'no scan run a' }])
      const unknown = await get(db, '', env, '?store=nope')
      expect([unknown.status, await unknown.json()]).toEqual([404, { error: 'unknown store \'nope\'' }])
    })
    it('a D1 without the tables answers configured: false', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec('DROP TABLE scan_run_outputs; DROP TABLE scan_run_phases; DROP TABLE scan_runs')
      const r = await get(db, '')
      expect([r.status, await r.json()]).toEqual([200, { configured: false, runs: [], project: 'proj', meta: 'https://h.test/meta' }])
    })
  })
})
