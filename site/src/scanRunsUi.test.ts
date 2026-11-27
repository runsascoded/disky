import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { ScanRun } from './scanRunsModel'
import { agoLong, fmtRunShort, IssueStanding, type JobLinks, OUTCOME_HEAD, fmtRunTime, ReasonText, runDay, scanRunsApi, scansHref, scansStore } from './scanRunsUi'
import { resolveStores } from './stores'
import { TEST_REGISTRY } from './testStores'

describe('per-store scan runs', () => {
  const [cw, meta] = resolveStores('cw', 'meta', TEST_REGISTRY)
  it('`?store=` picks a configured store; none or an unknown key is the primary', () => {
    expect(['', '?store=meta', '?store=cw', '?store=nope', '?x=1&store=meta'].map(q => scansStore(q, [cw, meta]).key)).toEqual([
      'cw', 'meta', 'cw', 'cw', 'meta',
    ])
  })
  it('page links live under the store\'s path (`/meta/scans`); API URLs carry `store=` for a secondary store only', () => {
    expect([
      scansHref(cw, undefined, cw), scansHref(cw, 'uid/1', cw), scansHref(meta, undefined, cw), scansHref(meta, 'uid/1', cw),
      scanRunsApi(cw, undefined, cw), scanRunsApi(meta, undefined, cw), scanRunsApi(meta, 'uid/1', cw),
    ]).toEqual([
      '/scans', '/scans/uid%2F1', '/meta/scans', '/meta/scans/uid%2F1',
      '/api/scan-runs', '/api/scan-runs?store=meta', '/api/scan-runs/uid%2F1?store=meta',
    ])
  })
})

describe('scan-run display times', () => {
  const at = Date.parse('2026-10-13T02:06:00Z') / 1000
  it('uses the viewer timezone for the timestamp and chart day across midnight', () => {
    expect([
      fmtRunTime(at, 'America/New_York'), runDay(at, 'America/New_York'),
      fmtRunTime(at, 'Asia/Tokyo'), runDay(at, 'Asia/Tokyo'),
      fmtRunTime(null),
    ]).toEqual([
      'Oct 12, 2026, 10:06 PM EDT', '2026-10-12',
      'Oct 13, 2026, 11:06 AM GMT+9', '2026-10-13',
      '—',
    ])
  })
  it('short form: weekday, month, day and time in the viewer timezone; the year only outside the current one', () => {
    const now = Date.parse('2026-10-16T12:00:00Z') / 1000
    expect([
      fmtRunShort(at, 'America/New_York', now), fmtRunShort(at, 'Asia/Tokyo', now),
      fmtRunShort(Date.parse('2025-12-31T20:00:00Z') / 1000, 'America/New_York', now),
    ]).toEqual([
      'Mon Oct 12 · 10:06 PM', 'Tue Oct 13 · 11:06 AM',
      'Wed Dec 31 2025 · 3:00 PM',
    ])
  })
})

describe('OUTCOME_HEAD', () => {
  it('heads a recovered run by pointing at its issues', () => {
    expect(OUTCOME_HEAD.recovered).toBe('Recovered: see below.')
  })
})

describe('IssueStanding', () => {
  const run = (kind: string): ScanRun => ({ run_id: 'r1', scan: '2026-11-01', kind, finished_ts: 1_700_000_000 } as ScanRun)
  const render = (recovery: ScanRun): string => renderToStaticMarkup(createElement(
    MemoryRouter, null, createElement(IssueStanding, { issue: { phase: 'static-names', outcome: 'failed', recovery }, runHref: (id: string) => `/scans/${id}` }),
  ))
  it('words a recovery inferred from a later run by what the records show', () => {
    expect(render(run('scan'))).toBe('Held since: a later run&#x27;s <code>static-names</code> step completed (<a href="/scans/r1">the <code>2026-11-01</code> run</a>).')
  })
  it('keeps "Done by" for a linked phase-recovery attempt', () => {
    expect(render(run('phase-recovery'))).toMatch(/^Done by <a href="\/scans\/r1">a later attempt<\/a> at .+\.$/)
  })
})

describe('ReasonText', () => {
  const text = 'compaction g.c003 → c004 (job sn-compact-x in us-east1, since 21:58Z) holds the static-names store'
  const render = (jobs?: JobLinks): string => renderToStaticMarkup(createElement(
    MemoryRouter, null, createElement(ReasonText, { text, jobs, runHref: (id: string) => `/scans/${id}` }),
  ))
  const compaction = { run_id: 'uid-c', scan: '2026-11-25T1800', kind: 'compact', job_name: 'sn-compact-x', started_ts: 1 } as ScanRun
  it('links the job a busy skip names: its recorded run, else its Batch job, else nothing', () => {
    expect([render({ runs: [compaction], project: 'p' }), render({ runs: [], project: 'p' }), render({ runs: [], project: null }), render()]).toEqual([
      'compaction g.c003 → c004 (job <a href="/scans/uid-c"><code>sn-compact-x</code></a> in us-east1, since 21:58Z) holds the static-names store',
      'compaction g.c003 → c004 (job <a href="https://console.cloud.google.com/batch/jobsDetail/regions/us-east1/jobs/sn-compact-x/details?project=p" '
      + 'target="_blank" rel="noreferrer"><code>sn-compact-x</code> ↗</a> in us-east1, since 21:58Z) holds the static-names store',
      text,
      text,
    ])
  })
})

describe('agoLong', () => {
  it('reads to two units: seconds, minutes, then hours + zero-padded minutes, then days + hours', () => {
    const now = 1_800_000_000
    expect([0, 42, 60, 17 * 60 + 59, 3600, 2 * 3600 + 31 * 60 + 5, 86400, 3 * 86400 + 4 * 3600 + 59, -5].map(s => agoLong(now - s, now))).toEqual([
      '0s', '42s', '1m', '17m', '1h 00m', '2h 31m', '1d 0h', '3d 4h', '0s',
    ])
  })
})
