import { useMemo, useState } from 'react'
import { dateColor, dateGradientCss, epochDaysToDate, userColor } from './colors'
import type { UserIndexEntry } from './colors'
import type { AgeRow, ColorMode, Granularity } from './types'
import { MODE_LABELS } from './types'
import { useUnits } from './units'
import { usePerfCommit } from './perf'

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']

/** Color axes the chart can stratify by — every mode with a per-row value. */
export const AGE_MODES: ColorMode[] = ['date', 'read', 'user', 'tree']

// Read-mode key for "no read observed in the logging window"; sorts first so
// the never-read slab is the base of every stack (the sweep-interesting part).
const NEVER = -1

const dateBarColor = (i: number, n: number): string => dateColor(n > 1 ? i / (n - 1) : 1)

const dayToDate = (d: number): Date => new Date(d * 86400_000)
const iso = (d: number): string => dayToDate(d).toISOString().slice(0, 10)
const isoMonth = (d: number): string => iso(d).slice(0, 7)

// bucket start (epoch days) for a row's day under the given granularity
const bucketOf = (d: number, gran: Granularity): number => {
  if (gran === 'day') return d
  if (gran === 'week') return d - ((d + 3) % 7) // epoch day 0 = Thu; Monday start
  const dt = dayToDate(d)
  return Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth(), 1) / 86400_000
}

const bucketLabel = (b: number, gran: Granularity): string =>
  gran === 'month' ? isoMonth(b) : iso(b)

/** Stacked bars of bytes by created date (month/week/day), split per color mode.
 *  `readRange` null means the read axis is unavailable for this scan (no access
 *  window, or age strata published before they carried `a`). */
export function AgeChart({ rows, baseRows, diffLabels, catOrder, mode, onMode, modes = AGE_MODES, userIdx, readRange }: {
  rows: AgeRow[]
  /** The diff window's "before" scan, same path — enables the diff toggle. When
   *  present the chart can show per-vintage growth/shrink (created-date bucket
   *  gained bytes = new writes; lost bytes = deletions), which tells natural TTL
   *  expiry (old vintages shrink) from manual deletes (recent vintages shrink). */
  baseRows?: AgeRow[]
  diffLabels?: { from: string; to: string }
  catOrder: string[]
  /** Axes to offer (caller drops the ones this scan can't color by — never a dead button). */
  modes?: ColorMode[]
  mode: ColorMode
  onMode?: (m: ColorMode) => void
  userIdx: Map<string, UserIndexEntry>
  readRange?: { min: number; max: number } | null
}) {
  usePerfCommit('age')
  const { fmtBytes } = useUnits()
  // Default to the finest granularity that still fits on screen: the most
  // bars we'll draw is MAX (≈8px each across the 900-unit viewBox), and finer
  // beats coarser — CoreWeave's ~8-week history gets /day, GCS's years get
  // /week (its /day would be ~740 bars; /month was needlessly chunky).
  const [gran, setGran] = useState<Granularity>(() => {
    const days = rows.map(r => r.d).filter(Number.isFinite)
    if (!days.length) return 'month'
    const count = (g: Granularity) => new Set(days.map(d => bucketOf(d, g))).size
    const MAX = 120
    return (['day', 'week', 'month'] as Granularity[]).find(g => count(g) <= MAX) ?? 'month'
  })
  const [hover, setHover] = useState<{ b: number; x: number; y: number } | null>(null)
  const [diffOn, setDiffOn] = useState(false)

  // Per-vintage delta between the diff window's two scans, at the current path:
  // a created-date bucket's bytes can only grow (new objects written near that
  // date) or shrink (objects of that vintage deleted), so `to − from` reads as
  // net writes (green, recent buckets) vs. net deletions (red, any vintage).
  const diff = useMemo(() => {
    if (!baseRows?.length) return null
    const acc = (rs: AgeRow[]) => {
      const m = new Map<number, number>()
      for (const r of rs) {
        if (!Number.isFinite(r.d)) continue
        const bk = bucketOf(r.d, gran)
        m.set(bk, (m.get(bk) ?? 0) + r.b)
      }
      return m
    }
    const from = acc(baseRows), to = acc(rows)
    const keys = [...new Set([...from.keys(), ...to.keys()])].sort((a, b) => a - b)
    const rowsD = keys.map(bk => {
      const f = from.get(bk) ?? 0, t = to.get(bk) ?? 0
      return { bk, from: f, to: t, delta: t - f }
    })
    let up = 0, down = 0
    for (const d of rowsD) { if (d.delta > 0) up = Math.max(up, d.delta); else down = Math.max(down, -d.delta) }
    return { rowsD, up, down }
  }, [baseRows, rows, gran])
  const showDiff = diffOn && !!diff

  const { buckets, byBucket, colorOf, labelOf, segOrder, legend } = useMemo(() => {
    const slotMap = new Map(catOrder.slice(0, 8).map((k, i) => [k, SLOTS[i]]))
    const userMode = mode === 'user'
    const keyOf = (r: AgeRow) =>
      mode === 'read' ? String(r.a ?? NEVER)
      : userMode ? (r.u ?? 'unattributed')
      : r.d1 && slotMap.has(r.d1) ? r.d1 : '(other)'
    const byBucket = new Map<number, Map<string, number>>()
    for (const r of rows) {
      if (!Number.isFinite(r.d)) continue // pre-day-granularity snapshot rows
      const bk = bucketOf(r.d, gran)
      const k = keyOf(r)
      const m = byBucket.get(bk) ?? new Map<string, number>()
      m.set(k, (m.get(k) ?? 0) + r.b)
      byBucket.set(bk, m)
    }
    const buckets = [...byBucket.keys()].sort((a, b) => a - b)
    const rr = readRange && readRange.max > readRange.min ? readRange : null
    const colorOf = (k: string): string =>
      mode === 'read'
        ? (k === String(NEVER) || !rr ? 'var(--never-read)' : dateColor((Number(k) - rr.min) / (rr.max - rr.min)))
      : userMode ? (k === 'unattributed' ? 'var(--t-unattr)' : userColor(k, userIdx))
      : `var(${slotMap.get(k) ?? '--other'})`
    const labelOf = (k: string): string =>
      mode === 'read' ? (k === String(NEVER) ? 'never read' : `read ${epochDaysToDate(Number(k))}`)
      : k === 'unattributed' ? 'unowned' // internal key; display name is standardized
      : k
    // Stack order: categorical modes put the biggest slice at the base; the
    // read axis stacks by time instead (never-read base, then older → newer
    // reads), so the un-touched share of each vintage is one contiguous slab.
    const segOrder = (a: [string, number], b: [string, number]): number =>
      mode === 'read' ? Number(a[0]) - Number(b[0]) : b[1] - a[1]
    const legend: [string, string][] =
      userMode
        ? [
            ...[...userIdx.keys()].slice(0, 10).map((u): [string, string] => [u, userColor(u, userIdx)]),
            ['unowned', 'var(--t-unattr)'],
          ]
        : [
            ...catOrder.slice(0, 8).map((k, i): [string, string] => [k, `var(${SLOTS[i]})`]),
            ['(other)', 'var(--other)'],
          ]
    return { buckets, byBucket, colorOf, labelOf, segOrder, legend }
  }, [rows, catOrder, mode, userIdx, gran, readRange])

  const maxB = useMemo(
    () => Math.max(...buckets.map(b => [...byBucket.get(b)!.values()].reduce((a, v) => a + v, 0))),
    [buckets, byBucket],
  )

  if (buckets.length === 0) return null

  const W = 900
  const H = 220
  // Bars: the created-date buckets (snapshot) or the union of both scans' (diff).
  const bars: number[] = showDiff ? diff!.rowsD.map(d => d.bk) : buckets
  const bw = W / Math.max(bars.length, 1)
  const gap = bw > 4 ? 1 : bw > 1.5 ? 0.4 : 0
  const tickEvery = Math.ceil(bars.length / 12)
  // Diff geometry: a zero line placed so both the biggest gain and biggest loss
  // fit; grows up, shrinks down (all-negative → line near the top, and vice versa).
  const dspan = diff ? Math.max(diff.up + diff.down, 1) : 1
  const zeroY = diff ? (diff.up / dspan) * H : H
  const diffByBk = showDiff ? new Map(diff!.rowsD.map(d => [d.bk, d])) : null

  return (
    <div className="agechart">
      <div className="legend">
        {showDiff ? (
          <>
            <span className="li"><span className="sw" style={{ background: 'var(--grew)' }} />grew</span>
            <span className="li"><span className="sw" style={{ background: 'var(--shrank)' }} />shrank</span>
            {diffLabels && <span className="li lbl">{diffLabels.from} → {diffLabels.to}</span>}
          </>
        ) : mode === 'date' ? (
          <span className="li gradli">
            older
            <span className="gradbar" style={{ background: dateGradientCss() }} />
            newer
          </span>
        ) : mode === 'read' ? (
          <>
            <span className="li"><span className="sw" style={{ background: 'var(--never-read)' }} />never read*</span>
            {readRange && (
              <span className="li gradli">
                {epochDaysToDate(readRange.min)}
                <span className="gradbar" style={{ background: dateGradientCss() }} />
                {epochDaysToDate(readRange.max)}
              </span>
            )}
          </>
        ) : (
          legend.map(([k, v]) => (
            <span className="li" key={k}>
              <span className="sw" style={{ background: v }} />
              {k}
            </span>
          ))
        )}
        <span className="ctl">
          {diff && (
            <span className="gran" role="radiogroup" aria-label="Snapshot or diff">
              <button role="radio" aria-checked={!showDiff} className={!showDiff ? 'on' : ''} onClick={() => setDiffOn(false)}>snapshot</button>
              <button role="radio" aria-checked={showDiff} className={showDiff ? 'on' : ''} onClick={() => setDiffOn(true)}>diff</button>
            </span>
          )}
          {onMode && !showDiff && (
            <span className="gran" role="radiogroup" aria-label="Color by">
              <span className="lbl">color by</span>
              {modes.map(m => (
                <button key={m} role="radio" aria-checked={mode === m} className={mode === m ? 'on' : ''} onClick={() => onMode(m)}>
                  {MODE_LABELS[m]}
                </button>
              ))}
            </span>
          )}
          <span className="gran" role="radiogroup" aria-label="Time granularity">
            {(['month', 'week', 'day'] as Granularity[]).map(g => (
              <button key={g} role="radio" aria-checked={gran === g} className={gran === g ? 'on' : ''} onClick={() => setGran(g)}>
                {g}
              </button>
            ))}
          </span>
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H + 24}`} preserveAspectRatio="none" role="img" aria-label={showDiff ? `Bytes changed by created ${gran}` : `Bytes by created ${gran}`}>
        {showDiff && <line x1={0} y1={zeroY} x2={W} y2={zeroY} className="zeroline" />}
        {bars.map((bk, i) => {
          let body
          if (showDiff) {
            const d = diffByBk!.get(bk)!
            const mag = (Math.abs(d.delta) / dspan) * H
            const y = d.delta >= 0 ? zeroY - mag : zeroY
            body = <rect x={i * bw + gap} y={y} width={Math.max(bw - 2 * gap, 0.8)} height={Math.max(mag, 0)} fill={d.delta >= 0 ? 'var(--grew)' : 'var(--shrank)'} rx={bw > 4 ? 1.5 : 0} />
          } else {
            const parts = byBucket.get(bk)!
            let y = H
            body = [...parts.entries()].sort(segOrder).map(([k, b]) => {
              const h = (b / maxB) * H
              y -= h
              return <rect key={k} x={i * bw + gap} y={y} width={Math.max(bw - 2 * gap, 0.8)} height={Math.max(h - gap, 0)} fill={mode === 'date' ? dateBarColor(i, bars.length) : colorOf(k)} rx={bw > 4 ? 1.5 : 0} />
            })
          }
          return (
            <g
              key={bk}
              onMouseMove={e => setHover({ b: bk, x: e.clientX, y: e.clientY })}
              onMouseLeave={() => setHover(null)}
            >
              <rect x={i * bw} y={0} width={bw} height={H} fill="transparent" />
              {body}
              {(i % tickEvery === 0) && (
                <text x={i * bw + bw / 2} y={H + 16} textAnchor="middle" className="tick">{bucketLabel(bk, gran)}</text>
              )}
              {hover?.b === bk && <rect x={i * bw} y={0} width={bw} height={H} className="hoverband" />}
            </g>
          )
        })}
      </svg>
      {hover && (
        <div className="tip" style={{ left: Math.min(hover.x + 14, window.innerWidth - 300), top: hover.y + 14 }}>
          <div className="path">
            {gran === 'week' ? `wk of ${bucketLabel(hover.b, gran)}` : bucketLabel(hover.b, gran)}
          </div>
          {showDiff ? (() => {
            const d = diffByBk!.get(hover.b)!
            return (
              <div className="nums">
                {`was ${fmtBytes(d.from)} · now ${fmtBytes(d.to)} · `}
                <span className={d.delta >= 0 ? 'grew' : 'shrank'}>{`${d.delta >= 0 ? '+' : '−'}${fmtBytes(Math.abs(d.delta))}`}</span>
              </div>
            )
          })() : (
            <div className="nums">
              {[...(byBucket.get(hover.b) ?? new Map<string, number>())]
                .sort(mode === 'read' ? (a, b) => b[1] - a[1] : segOrder)
                .slice(0, 5)
                .map(([k, b]) => `${labelOf(k)} ${fmtBytes(b)}`)
                .join(' · ')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
