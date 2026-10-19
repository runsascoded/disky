import { describe, expect, it } from 'vitest'
import { fmtRunShort, fmtRunTime, runDay, scanRunsApi, scansHref, scansStore } from './scanRunsUi'
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
