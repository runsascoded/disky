import { describe, expect, it } from 'vitest'
import { DAY, decodeSel, encodeSel, type ScanSel } from './scan'

const HOUR = DAY / 24
// A fixed "now" so year-less spellings resolve deterministically.
const NOW = new Date(Date.UTC(2026, 8, 20))

describe('decodeSel — the ?d= grammar (canonical dashless slugs, and every legacy spelling)', () => {
  const cases: [string, ScanSel | undefined][] = [
    ['', undefined],
    // before = look-back span, end = latest (floating)
    ['-3d', { span: 3 * DAY }],
    ['-6d12h', { span: 6 * DAY + 12 * HOUR }],
    ['-12h', { span: 12 * HOUR }],
    // end pinned: a day, an hour, a minute (dashless, canonical)
    ['260904', { d: '2026-09-04' }],
    ['26090400', { d: '2026-09-04T00' }],
    ['2609040002', { d: '2026-09-04T0002' }],
    // end pinned + span
    ['2609040002-7d', { d: '2026-09-04T0002', span: 7 * DAY }],
    ['26090412-12h', { d: '2026-09-04T12', span: 12 * HOUR }],
    // start pinned (from), end floating
    ['-2609010002', { from: '2026-09-01T0002' }],
    ['-260901', { from: '2026-09-01' }],
    // both endpoints pinned, any granularity each
    ['2609040002-2609010002', { d: '2026-09-04T0002', from: '2026-09-01T0002' }],
    ['260904-260901', { d: '2026-09-04', from: '2026-09-01' }],
    ['26090412-260901', { d: '2026-09-04T12', from: '2026-09-01' }],
    // legacy: the `-HH` / `-HHMM` time after a 6-digit day is that day's time,
    // never a range (no canonical slug is 2 or 4 digits)
    ['260904-12', { d: '2026-09-04T12' }],
    ['260904-0002', { d: '2026-09-04T0002' }],
    ['260904-0002-7d', { d: '2026-09-04T0002', span: 7 * DAY }],
    ['-260901-0002', { from: '2026-09-01T0002' }],
    ['260904-0002-260901-0002', { d: '2026-09-04T0002', from: '2026-09-01T0002' }],
    ['260904-12-260901', { d: '2026-09-04T12', from: '2026-09-01' }],
    ['260904T0002', { d: '2026-09-04T0002' }],
    // legacy ISO, whole
    ['2026-09-04', { d: '2026-09-04' }],
    ['2026-09-04T12', { d: '2026-09-04T12' }],
    ['2026-09-04T0002', { d: '2026-09-04T0002' }],
    // legacy year-less (current year)
    ['9-4', { d: '2026-09-04' }],
    ['9-4-12', { d: '2026-09-04T12' }],
    // not a selection: kept verbatim, so it's a miss rather than "latest"
    ['junk', { invalid: 'junk' }],
    ['26090412-12', { invalid: '26090412-12' }],
    ['260904-260901-260830', { invalid: '260904-260901-260830' }],
    ['260904-260901-7d', { invalid: '260904-260901-7d' }],
    ['261399', { invalid: '261399' }],
    ['2026-13-01', { invalid: '2026-13-01' }],
    ['-', { invalid: '-' }],
  ]
  for (const [enc, sel] of cases) {
    it(`decodes ${JSON.stringify(enc)}`, () => {
      expect(decodeSel(enc, NOW)).toEqual(sel)
    })
  }
})

describe('encodeSel — canonical forms round-trip', () => {
  const canonical = [
    '-3d', '-6d12h', '260904', '26090400', '2609040002', '2609040002-7d', '26090412-12h',
    '-2609010002', '-260901', '2609040002-2609010002', '260904-260901', '26090412-260901', 'junk',
  ]
  for (const enc of canonical) {
    it(`round-trips ${enc}`, () => {
      expect(encodeSel(decodeSel(enc, NOW))).toBe(enc)
    })
  }
  it('legacy spellings re-encode dashless', () => {
    expect(['260904-12', '260904-0002', '260904-0002-260901-0002', '-260901-0002', '260904T0002', '2026-09-04T0002', '9-4-12'].map(e => encodeSel(decodeSel(e, NOW))))
      .toEqual(['26090412', '2609040002', '2609040002-2609010002', '-2609010002', '2609040002', '2609040002', '26090412'])
  })
  it('empty selection encodes to undefined', () => {
    expect(encodeSel(undefined)).toBe(undefined)
    expect(encodeSel({})).toBe(undefined)
  })
})
