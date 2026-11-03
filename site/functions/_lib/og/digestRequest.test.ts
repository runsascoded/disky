/** `POST /api/digest-image`'s request validation: both kinds' parsed form, and each 400's message. */
import { describe, expect, it } from 'vitest'
import { TIER_COLORS } from './digestCard'
import { parseDigestRequest, todayAt, windowFrame } from './digestRequest'

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
      from: OCT - 16 * H, to: NOV - 8 * H, month: OCT,
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
      from: Date.UTC(2026, 8, 30, 8), to: NOV - 8 * H, month: OCT,
      points: [{ t: Date.UTC(2026, 8, 30, 8), bytes: 14e12 }, { t: Date.UTC(2026, 9, 1, 8), bytes: 15e12 }],
      quota: { bytes: 253e12, name: '253 TB public' },
      diff: { from: '2026-09-30T0800', to: '2026-10-01T0800', path: 'bucket', title: 'What changed' },
    })
    expect(parseDigestRequest({ ...usage, kind: 'usage' })).toEqual(parseDigestRequest(usage))
  })
  it('drops a malformed quota, and takes no diff', () => {
    expect(parseDigestRequest({ ...usage, quota: { bytes: '1' }, diff: null, units: undefined })).toEqual({
      kind: 'usage', title: 'Usage — October 2026', note: 'example.org · TB', units: 'TiB',
      from: Date.UTC(2026, 8, 30, 8), to: NOV - 8 * H, month: OCT,
      points: [{ t: Date.UTC(2026, 8, 30, 8), bytes: 14e12 }, { t: Date.UTC(2026, 9, 1, 8), bytes: 15e12 }],
      quota: null, diff: null,
    })
  })
  it("over a window (`from`): from its baseline scan, the latest scan at `todayAt` of the width, the month its own", () => {
    // 10/1 08:00, the 1st: the baseline 9/1 08:00 (before the window's first day, 9/2), the latest at 0.7
    const req = parseDigestRequest({ ...usage, from: '2026-09-02', points: [{ scan: '2026-09-01T0800', bytes: 13e12 }, ...usage.points] })
    if (typeof req === 'string') throw new Error(req)
    const t = Date.UTC(2026, 9, 1, 8)
    expect([req.from, req.month, +((t - req.from) / (req.to - req.from)).toFixed(9)]).toEqual([Date.UTC(2026, 8, 1, 8), OCT, 0.7])
  })
  it("places the latest scan at 0.7 of the width on the 1st, rising linearly to 1 on the month's last day", () => {
    expect([todayAt(1, 31), todayAt(16, 31), todayAt(31, 31), todayAt(1, 28), todayAt(28, 28)].map(v => +v.toFixed(9))).toEqual([0.7, 0.85, 1, 0.7, 1])
    expect(todayAt(15, 30)).toBeCloseTo(0.7 + 0.3 * 14 / 29, 12)
  })
  it('spans the window plus runway: [today − 29 d, today + 29 d · (1 − p) / p]', () => {
    const D = 86400_000
    const span = (y: number, mo: number, d: number) => {
      const today = Date.UTC(y, mo, d)
      const { from, to } = windowFrame(today - 29 * D, [today - 29 * D, today], y, mo)
      return [(from - today) / D, +((to - today) / D).toFixed(6)]
    }
    expect([
      span(2026, 10, 1),   // Nov 1: p 0.7 → 29 · 0.3 / 0.7 ≈ 12.43 days of runway
      span(2026, 10, 15),  // Nov 15: p = 0.7 + 0.3 · 14/29
      span(2026, 10, 30),  // Nov 30, the last day: p 1, no runway
      span(2027, 1, 1),    // Feb 1 (28 days): p 0.7
      span(2027, 1, 28),   // Feb 28: p 1
    ]).toEqual([
      [-29, +(29 * 0.3 / 0.7).toFixed(6)],
      [-29, +(29 * (1 - todayAt(15, 30)) / todayAt(15, 30)).toFixed(6)],
      [-29, 0],
      [-29, +(29 * 0.3 / 0.7).toFixed(6)],
      [-29, 0],
    ])
  })
  it("takes today's position range (`today_at`), and refuses a bad one", () => {
    const req = parseDigestRequest({ ...usage, from: '2026-09-02', today_at: [0.5, 0.9], points: [{ scan: '2026-09-01T0800', bytes: 13e12 }, ...usage.points] })
    if (typeof req === 'string') throw new Error(req)
    expect(+((Date.UTC(2026, 9, 1, 8) - req.from) / (req.to - req.from)).toFixed(9)).toBe(0.5)
    const msg = 'today_at: [p0, p1], 0 < p0 ≤ p1 ≤ 1'
    expect([[0.9, 0.5], [0, 1], [0.5], 'x'].map(today_at => parseDigestRequest({ ...usage, from: '2026-09-02', today_at }))).toEqual([msg, msg, msg, msg])
  })
  it('refuses a malformed `from`', () => {
    expect(['2026-9-15', 20260915, null].map(from => parseDigestRequest({ ...usage, from }))).toEqual(['from: YYYY-MM-DD', 'from: YYYY-MM-DD', 'from: YYYY-MM-DD'])
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
      'title and note required', 'title and note required', "kind: 'usage' | 'tiers' | 'pools'", 'month: YYYY-MM', "units: 'TiB' | 'TB'", 'points: an array',
    ])
  })
})

describe('parseDigestRequest: pools', () => {
  const pools = {
    kind: 'pools', title: 'CoreWeave usage — October 2026', note: 'cw.oa.dev · TiB', month: '2026-10', alert_pct: 85,
    pools: [
      { name: '02a', quota: 910 * T, points: [{ scan: '2026-10-02', bytes: 700 * T }, { scan: '2026-10-01T1236', bytes: 690 * T }] },
      { name: 'rno2a zone', quota: 100 * T, points: [{ scan: '2026-10-02', bytes: 101 * T }] },
    ],
  }
  it('parses each pool (points time-sorted) and the alert share, over the frame of every pool’s scans', () => {
    expect(parseDigestRequest(pools)).toEqual({
      kind: 'pools', title: 'CoreWeave usage — October 2026', note: 'cw.oa.dev · TiB', units: 'TiB',
      from: OCT - 16 * H, to: NOV - 8 * H, month: OCT, alertPct: 85,
      pools: [
        { name: '02a', quota: 910 * T, points: [{ t: Date.UTC(2026, 9, 1, 12, 36), bytes: 690 * T }, { t: Date.UTC(2026, 9, 2), bytes: 700 * T }] },
        { name: 'rno2a zone', quota: 100 * T, points: [{ t: Date.UTC(2026, 9, 2), bytes: 101 * T }] },
      ],
      diff: null,
    })
    expect((parseDigestRequest({ ...pools, alert_pct: undefined }) as { alertPct: number }).alertPct).toBe(90)
  })
  it('refuses no pools, a zero quota, a bad point, a bad alert share, or pools with no points at all', () => {
    const bad = 'pools: [{name, quota > 0, points: [{scan, bytes}]}]'
    expect([
      parseDigestRequest({ ...pools, pools: [] }),
      parseDigestRequest({ ...pools, pools: [{ ...pools.pools[0], quota: 0 }] }),
      parseDigestRequest({ ...pools, pools: [{ ...pools.pools[0], points: [{ scan: 'x', bytes: 1 }] }] }),
      parseDigestRequest({ ...pools, alert_pct: 0 }),
      parseDigestRequest({ ...pools, pools: pools.pools.map(pl => ({ ...pl, points: [] })) }),
    ]).toEqual(['pools: 1–8 [{name, quota, points: [{scan, bytes}]}]', bad, bad, 'alert_pct: (0, 100]', 'pools: at least one point'])
  })
})
