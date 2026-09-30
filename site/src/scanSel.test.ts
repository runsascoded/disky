import { describe, expect, it } from 'vitest'
import { DAY, decodeSel, encodeSel, type ScanSel } from './scan'

const HOUR = DAY / 24
// A fixed "now" so year-less spellings resolve deterministically (unused by the
// canonical YYMMDD forms below, but decodeSel takes it).
const NOW = new Date(Date.UTC(2026, 8, 20))

describe('decodeSel — the ?d= grammar', () => {
  const cases: [string, ScanSel | undefined][] = [
    ['', undefined],
    // before = look-back span, end = latest (floating)
    ['-3d', { span: 3 * DAY }],
    ['-6d12h', { span: 6 * DAY + 12 * HOUR }],
    ['-12h', { span: 12 * HOUR }],
    // end pinned, default look-back
    ['260904-0002', { d: '2026-09-04T0002' }],
    ['260904', { d: '2026-09-04' }],
    // end pinned + span
    ['260904-0002-7d', { d: '2026-09-04T0002', span: 7 * DAY }],
    // start pinned (from), end floating
    ['-260901-0002', { from: '2026-09-01T0002' }],
    ['-260901', { from: '2026-09-01' }],
    // both endpoints pinned (a frozen window)
    ['260904-0002-260901-0002', { d: '2026-09-04T0002', from: '2026-09-01T0002' }],
    ['260904-260901', { d: '2026-09-04', from: '2026-09-01' }],
    // the end scan's OWN -HHMM time (4 digits) is never read as a pinned start
    ['260819-1008', { d: '2026-08-19T1008' }],
  ]
  for (const [enc, sel] of cases) {
    it(`decodes ${JSON.stringify(enc)}`, () => {
      expect(decodeSel(enc, NOW)).toEqual(sel)
    })
  }
})

describe('encodeSel — canonical forms round-trip', () => {
  const canonical = [
    '-3d', '-6d12h', '260904-0002', '260904', '260904-0002-7d',
    '-260901-0002', '-260901', '260904-0002-260901-0002', '260904-260901', '260819-1008',
  ]
  for (const enc of canonical) {
    it(`round-trips ${enc}`, () => {
      expect(encodeSel(decodeSel(enc, NOW))).toBe(enc)
    })
  }
  it('empty selection encodes to undefined', () => {
    expect(encodeSel(undefined)).toBe(undefined)
    expect(encodeSel({})).toBe(undefined)
  })
})
