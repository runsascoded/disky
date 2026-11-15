import { expect, it } from 'vitest'
import { sqliteD1 } from './testD1'
import { loadScanRuns, scanRunDetail, scanRunsList } from './scanRuns'
import { isScanProducer, manualScans, scanRunOf } from '../../src/scanRunsModel'

it('lists poller failures and real recoveries without treating them as snapshots or mixing their navigation', async () => {
  const { db, raw } = await sqliteD1()
  raw.exec(`INSERT INTO scan_runs (run_id,scan,kind,status,started_ts,finished_ts,source,updated_ts) VALUES
    ('old','2026-11-08','scan','succeeded',10,20,'job',20),
    ('scan','2026-11-09T0701','scan','succeeded',100,200,'job',200),
    ('failed','2026-11-09T0820','access-ingest','failed',300,400,'job',400),
    ('held','2026-11-09T0920','access-ingest','nop',500,600,'job',600),
    ('recovered','2026-11-09T1020','access-ingest','succeeded',700,800,'job',800);
    INSERT INTO scan_run_phases (run_id,phase,seq,started_ts,finished_ts,status,note) VALUES
    ('failed','access-ingest',0,300,400,'failed','failed (exit 1)'),
    ('held','access-ingest',0,500,600,'done','skipped (busy): ingest lease held'),
    ('recovered','access-ingest',0,700,800,'done','ingest lease taken');`)
  raw.exec("UPDATE scan_runs SET trigger = 'manual' WHERE run_id IN ('scan', 'recovered')")
  const rows = (await loadScanRuns(db, 'primary'))!
  const list = scanRunsList(rows, 900)
  expect(list.runs.map(s => [s.run.run_id, s.run.kind, s.run.status])).toEqual([
    ['recovered', 'access-ingest', 'succeeded'], ['held', 'access-ingest', 'nop'], ['failed', 'access-ingest', 'failed'],
    ['scan', 'scan', 'succeeded'], ['old', 'scan', 'succeeded'],
  ])
  expect(scanRunDetail(rows, 'scan', 900)?.prev_scan).toBe('2026-11-08')
  expect(scanRunDetail(rows, 'scan', 900)?.next_scan).toBeNull()
  const failed = scanRunDetail(rows, 'failed', 900)!
  expect([failed.prev_scan, failed.next_scan, failed.siblings, failed.outputs]).toEqual([null, '2026-11-09T0920', [], []])
  expect(failed.phase_issues.map(i => [i.phase, i.outcome, i.recovery?.run_id])).toEqual([['access-ingest', 'failed', 'recovered']])
  expect([isScanProducer('access-ingest'), scanRunOf(list.runs, '2026-11-09T1020'), scanRunOf(list.runs, '2026-11-09T0701')?.run_id]).toEqual([false, undefined, 'scan'])
  expect([...manualScans(list.runs)]).toEqual(['2026-11-09T0701'])
})
