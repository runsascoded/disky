/** `POST /api/digest-image`'s request validation: both kinds' parsed form, and each 400's message. */
import { describe, expect, it } from 'vitest'
import { TIER_COLORS } from './digestCard'
import { parseDigestRequest } from './digestRequest'

const T = 2 ** 40
const H = 3600_000
const OCT = Date.UTC(2026, 9, 1)
const NOV = Date.UTC(2026, 10, 1)

const tiers = {
  kind: 'tiers', title: 'marin GCS — October 2026', note: 'gcs.oa.dev · TiB', month: '2026-10',
  tiers: ['Archive', 'Coldline', 'Nearline', 'Standard'],
  points: [
    { scan: '2026-10-02', bytes: [0, 2900 * T, 1 * T, 30 * T] },
    { scan: '2026-10-01T1236', bytes: [0, 2910 * T, 1 * T, 30 * T] },
  ],
}

describe('parseDigestRequest: tiers', () => {
  it('parses the tiers (default colours, bottom → top) and the points (time-sorted) over the month frame', () => {
    expect(parseDigestRequest(tiers)).toEqual({
      kind: 'tiers', title: 'marin GCS — October 2026', note: 'gcs.oa.dev · TiB', units: 'TiB',
      from: OCT - 16 * H, to: NOV - 8 * H,
      tiers: [
        { name: 'Archive', color: TIER_COLORS[0] }, { name: 'Coldline', color: TIER_COLORS[1] },
        { name: 'Nearline', color: TIER_COLORS[2] }, { name: 'Standard', color: TIER_COLORS[3] },
      ],
      points: [
        { t: Date.UTC(2026, 9, 1, 12, 36), bytes: [0, 2910 * T, 1 * T, 30 * T] },
        { t: Date.UTC(2026, 9, 2), bytes: [0, 2900 * T, 1 * T, 30 * T] },
      ],
      diff: null,
    })
  })
  it('takes a diff as the usage card does (and refuses a bad one)', () => {
    const diff = { from: '2026-09-30T0701', to: '2026-10-17T0701', path: '', title: 'What changed — 10/1 → 10/17' }
    const ok = parseDigestRequest({ ...tiers, diff })
    expect([typeof ok === 'string' ? ok : ok.diff, parseDigestRequest({ ...tiers, diff: { ...diff, path: '../x' } })]).toEqual([diff, 'diff: bad path'])
  })
  it('refuses tiers that are missing, empty, too many or not names', () => {
    const msg = `tiers: 1–${TIER_COLORS.length} names, bottom → top`
    expect([undefined, [], Array(TIER_COLORS.length + 1).fill('x'), ['a', 1]].map(t => parseDigestRequest({ ...tiers, tiers: t }))).toEqual([msg, msg, msg, msg])
  })
  it('refuses a point whose bytes are not one non-negative number per tier', () => {
    const msg = 'points: [{scan, bytes: [one per tier]}]'
    expect([
      [{ scan: '2026-10-02', bytes: [1, 2, 3] }],
      [{ scan: '2026-10-02', bytes: [1, 2, 3, -4] }],
      [{ scan: '2026-10-02', bytes: [1, 2, 3, '4'] }],
      [{ scan: '2026-10-02', bytes: 10 }],
      [{ scan: 'oct-2', bytes: [1, 2, 3, 4] }],
      [null],
    ].map(points => parseDigestRequest({ ...tiers, points }))).toEqual([msg, msg, msg, msg, msg, msg])
  })
})

describe('parseDigestRequest: usage', () => {
  const usage = {
    title: 'Usage — October 2026', note: 'example.org · TB', month: '2026-10', units: 'TB',
    points: [{ scan: '2026-10-01T0800', bytes: 15e12 }, { scan: '2026-09-30T0800', bytes: 14e12 }],
    quota: { bytes: 253e12, name: '253 TB public' },
    diff: { from: '2026-09-30T0800', to: '2026-10-01T0800', path: 'bucket', title: 'What changed' },
  }
  it('is the kind when none is given (the cw job sends none); its frame widens to the lead-in scan', () => {
    expect(parseDigestRequest(usage)).toEqual({
      kind: 'usage', title: 'Usage — October 2026', note: 'example.org · TB', units: 'TB',
      from: Date.UTC(2026, 8, 30, 8), to: NOV - 8 * H,
      points: [{ t: Date.UTC(2026, 8, 30, 8), bytes: 14e12 }, { t: Date.UTC(2026, 9, 1, 8), bytes: 15e12 }],
      quota: { bytes: 253e12, name: '253 TB public' },
      diff: { from: '2026-09-30T0800', to: '2026-10-01T0800', path: 'bucket', title: 'What changed' },
    })
    expect(parseDigestRequest({ ...usage, kind: 'usage' })).toEqual(parseDigestRequest(usage))
  })
  it('drops a malformed quota, and takes no diff', () => {
    expect(parseDigestRequest({ ...usage, quota: { bytes: '1' }, diff: null, units: undefined })).toEqual({
      kind: 'usage', title: 'Usage — October 2026', note: 'example.org · TB', units: 'TiB',
      from: Date.UTC(2026, 8, 30, 8), to: NOV - 8 * H,
      points: [{ t: Date.UTC(2026, 8, 30, 8), bytes: 14e12 }, { t: Date.UTC(2026, 9, 1, 8), bytes: 15e12 }],
      quota: null, diff: null,
    })
  })
  it('refuses a bad diff', () => {
    expect([
      { ...usage.diff, from: '2026-10-01T0800' },
      { ...usage.diff, path: '../x' },
      { ...usage.diff, path: '/x' },
      { ...usage.diff, title: 1 },
    ].map(diff => parseDigestRequest({ ...usage, diff }))).toEqual(['diff: from < to, scan ids', 'diff: bad path', 'diff: bad path', 'diff: title required'])
  })
})

describe('parseDigestRequest: common', () => {
  it('refuses a missing title / note, an unknown kind, a bad month or units, non-array points', () => {
    expect([
      null,
      { ...tiers, title: undefined },
      { ...tiers, kind: 'pie' },
      { ...tiers, month: '2026-1' },
      { ...tiers, units: 'PiB' },
      { ...tiers, points: {} },
    ].map(parseDigestRequest)).toEqual([
      'title and note required', 'title and note required', "kind: 'usage' | 'tiers'", 'month: YYYY-MM', "units: 'TiB' | 'TB'", 'points: an array',
    ])
  })
})
