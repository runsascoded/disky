import { Tooltip } from './Tooltip'
import { Explain } from './Help'
import { TimeSeries } from '@disk-tree/react'
import type { Annotation, Series as TsSeries } from '@disk-tree/react'
import { DEFAULT_PALETTE } from '@rdub/treemap'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { boolParam, useUrlState } from 'use-prms'
import { shortName } from './UserChip'
import { useStore, useStoreFetch } from './store'
import { useUnits } from './units'
import { Skeleton } from './Busy'
import { bandCallouts, pickAnnotations, relativeSeries, stackSeries, unitTicks, youngestGenesis } from './series'
import { DAY, fmtScan } from './scan'
import type { Band } from './series'
import { stringParam } from 'use-prms'
import { perf, usePerfCommit } from './perf'

// Stored bytes over the historical scans, scoped exactly like the map: the
// drilled prefix, a user, or an owner pool (`/api/series` — one row read per
// scan in that scan's index tiers; specs/view-serving.md §1). Nothing is
// precomputed per prefix and nothing is floored.

interface Pt { x: number; y: number; y0?: number }
interface Series { path: string; points: { date: string; b: number; o: number }[]; roots?: { path: string; points: { date: string; b: number; o: number }[] }[] }

// Default prominence-suppression radius for the local-extrema callouts, in days
// (≈6 scans at the 12-hourly cadence): peaks/dips within this of a more
// prominent one collapse to it.
const DEFAULT_RADIUS_DAYS = 3
// Default prominence floor for those callouts, as a percentage of the trace's
// range: a wobble smaller than this isn't a peak or dip worth a label.
const DEFAULT_FLOOR_PCT = 10

type YFrom = 'data' | 'zero'
type Layout = 'stacked' | 'lines'
type XRange = '1w' | '1m' | 'all'
// What the y-axis measures: bytes, bytes gained since each trace's first shown
// scan, or that as a percentage of the start.
type Values = 'abs' | 'delta' | 'pct'
const X_RANGES: [XRange, number | null][] = [['1w', 7], ['1m', 30], ['all', null]]

// The x-range (`?xr=1w|1m`; all = default): the last N days before the
// latest scan. A phone's chart is too narrow to read a month of 12-hourly
// scans at the right edge.
function XRangeToggle({ v, set }: { v: XRange; set: (r: XRange) => void }) {
  return (
    <span className="gran" role="radiogroup" aria-label="X-axis range">
      {X_RANGES.map(([r, days]) => (
        <Explain key={r} text={days ? `Only the last ${days === 7 ? 'week' : '30 days'} of scans` : 'Every scan'}>
          <button role="radio" aria-checked={v === r} className={v === r ? 'on' : ''} onClick={() => set(r)}>{r}</button>
        </Explain>
      ))}
    </span>
  )
}

// Per-root layout at the store root (`?ln` — lines on): stacked bands (the
// composition; a root's band starts at its genesis) or overlaid lines (each
// root's own movement).
function LayoutToggle({ v, set }: { v: Layout; set: (l: Layout) => void }) {
  return (
    <span className="gran" role="radiogroup" aria-label="Roots layout">
      <span className="lbl">roots</span>
      {(['stacked', 'lines'] as Layout[]).map(l => (
        <Explain key={l} text={l === 'stacked' ? 'One band per bucket, stacked to the total; a bucket’s band starts at the first scan that covered it' : 'One line per bucket (the total is in the tooltip — drawn, it would dwarf them)'}>
          <button role="radio" aria-checked={v === l} className={v === l ? 'on' : ''} onClick={() => set(l)}>{l}</button>
        </Explain>
      ))}
    </span>
  )
}

// y-axis origin toggle (`?fit` — fit on): anchor at zero (default — honest
// proportions; the Δ / % values are the way to see a small movement in a
// large total) or fit the y-range to the data. Stacked, "fit" zooms to the
// stack's top edge (the total's own movement), clipping the bands below.
function YFromToggle({ v, set, stacked }: { v: YFrom; set: (y: YFrom) => void; stacked: boolean }) {
  return (
    <span className="gran" role="radiogroup" aria-label="Y-axis range">
      <span className="lbl">y-axis</span>
      {(['zero', 'data'] as YFrom[]).map(y => (
        <Explain key={y} text={y === 'data' ? (stacked ? 'Zoom the y-range to the top of the stack — the total’s own movement, and which band it came from' : 'Fit the y-range to the data (a small movement in a large total stays visible)') : 'Start the y-axis at zero (honest proportions)'}>
          <button role="radio" aria-checked={v === y} className={v === y ? 'on' : ''} onClick={() => set(y)}>{y === 'data' ? 'fit' : 'from 0'}</button>
        </Explain>
      ))}
    </span>
  )
}

// What the y-axis measures (`?ym=d|p`; abs = default). Δ and % put buckets
// of very different sizes on one comparable axis: each trace starts at 0 and
// shows what it gained or lost since — the question "what moved", which the
// absolute stack answers only for the biggest bucket.
function ValuesToggle({ v, set }: { v: Values; set: (m: Values) => void }) {
  const opts: [Values, string, string][] = [
    ['abs', 'abs', 'Stored bytes'],
    ['delta', 'Δ', 'Bytes gained (or lost) since each trace’s first shown scan — every trace starts at 0, so buckets of any size compare'],
    ['pct', '%', 'Growth since each trace’s first shown scan, as a percentage of its size then'],
  ]
  return (
    <span className="gran" role="radiogroup" aria-label="Y-axis values">
      <span className="lbl">values</span>
      {opts.map(([m, label, text]) => (
        <Explain key={m} text={text}>
          <button role="radio" aria-checked={v === m} className={v === m ? 'on' : ''} onClick={() => set(m)}>{label}</button>
        </Explain>
      ))}
    </span>
  )
}

// Points sit at UTC midnight of each scan's calendar date, so labels format
// in UTC too — a local-time render shows the 8/23 scan as “Aug 22” in the US.
// x → the scan id it came from: date-only ids sit at midnight; a sub-daily
// id keeps its `THHMM` (`2026-09-17T1201`), so a pick or brush names the
// exact scan.
export const dateOfX = (x: number) => {
  const iso = new Date(x).toISOString()
  return iso.slice(11, 16) === '00:00' ? iso.slice(0, 10) : `${iso.slice(0, 10)}T${iso.slice(11, 13)}${iso.slice(14, 16)}`
}
const fmtX = (x: number) => new Date(x).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
// The tooltip's x: the scan's canonical label (`fmtScan`) — a sub-daily scan
// shows its local 12-hour time with a bare a/p (`9/23 8:01a`), exactly like the
// scan dropdown/header; a date-only scan stays a bare date. Distinguishes the
// two scans of a day, which `fmtX`'s date-only axis label can't.
const fmtXTip = (x: number) => fmtScan(dateOfX(x))
// A scan's instant: `YYYY-MM-DD` = UTC midnight, `YYYY-MM-DDTHHMM` = that
// UTC time. Two scans a day must not share an x (the bands key by x, and a
// shared x drew the total as a vertical step against the band).
export const xOfScan = (d: string) => {
  const m = /^(\d{4}-\d{2}-\d{2})(?:T(\d{2})(\d{2}))?$/.exec(d)
  return m ? new Date(`${m[1]}T${m[2] ?? '00'}:${m[3] ?? '00'}:00Z`).getTime() : new Date(d.slice(0, 10)).getTime()
}
// Signed formats for the relative modes: `+1.2 Ti` / `−340 Gi` / `0`, `+3.1%`.
const signed = (y: number, mag: string) => (y < 0 ? `−${mag}` : y > 0 ? `+${mag}` : mag)
const fmtPct = (y: number) => {
  const a = Math.abs(y * 100)
  return a === 0 ? '0%' : signed(y, `${a >= 10 ? a.toFixed(0) : a.toFixed(1)}%`)
}

export function SizeOverTime({ scans, prefix, user, pool, onPickDate, onBrush, window: win, scopeLabel = 'all buckets', paths, filterLabel }: {
  /** The store's root scope word for the unscoped subtitle (`all buckets`, `the whole bucket`). */
  scopeLabel?: string
  /** The page filter's match roots: the series is their sum per scan. */
  paths?: string[]
  /** The filter text, for the subtitle. */
  filterLabel?: string
  scans: string[]
  prefix: string
  /** The owner axis's user: their bytes under `prefix`, per scan. */
  user?: string | null
  /** The owner axis's pool — `unowned` = bytes no person owns, `owned`
   * = bytes some person owns — under `prefix`, per scan. `user` wins. */
  pool?: 'unowned' | 'owned' | null
  /** Click a point → view the page as of that scan (pins `?d=`). */
  onPickDate?: (date: string) => void
  /** Drag across the chart → make [from, to] the page's diff window. */
  onBrush?: (from: string, to: string) => void
  /** The page's current diff window (scan ids), shaded on the chart. */
  window?: [string, string]
}) {
  const { fmtBytes, units } = useUnits()
  const [fitP, setFitP] = useUrlState('fit', boolParam)
  const yFrom: YFrom = fitP ? 'data' : 'zero'
  const setYFrom = (y: YFrom) => setFitP(y === 'data')
  const [linesP, setLinesP] = useUrlState('ln', boolParam)
  const layout: Layout = linesP ? 'lines' : 'stacked'
  const setLayout = (l: Layout) => setLinesP(l === 'lines')
  const [xrP, setXrP] = useUrlState('xr', stringParam())
  const xRange: XRange = xrP === '1w' || xrP === '1m' ? xrP : 'all'
  const setXRange = (r: XRange) => setXrP(r === 'all' ? undefined : r)
  const [ymP, setYmP] = useUrlState('ym', stringParam())
  const values: Values = ymP === 'd' ? 'delta' : ymP === 'p' ? 'pct' : 'abs'
  const setValues = (m: Values) => setYmP(m === 'delta' ? 'd' : m === 'pct' ? 'p' : undefined)
  const relative = values !== 'abs'
  // Local-peak/dip callouts (specs/obs-axis-indexing.md): on by default, radius
  // is the prominence-suppression distance in days (`?ex=0` off; `?exr=<days>`),
  // floor the least prominence that earns a label, as % of the trace's range
  // (`?exp=<pct>`).
  const [exP, setExP] = useUrlState('ex', stringParam())
  const extrema = exP !== '0'
  const setExtrema = (on: boolean) => setExP(on ? undefined : '0')
  const [exrP, setExrP] = useUrlState('exr', stringParam())
  const radiusDays = exrP && !Number.isNaN(+exrP) ? +exrP : DEFAULT_RADIUS_DAYS
  const setRadiusDays = (d: number) => setExrP(d === DEFAULT_RADIUS_DAYS ? undefined : String(d))
  const [expP, setExpP] = useUrlState('exp', stringParam())
  const floorPct = expP && !Number.isNaN(+expP) ? +expP : DEFAULT_FLOOR_PCT
  const setFloorPct = (p: number) => setExpP(p === DEFAULT_FLOOR_PCT ? undefined : String(p))
  const [gearOpen, setGearOpen] = useState(false)

  // The store root, unscoped: one trace per root (specs/done/root-geneses.md §2).
  const split = !prefix && !user && !pool && !paths?.length && !filterLabel
  const scope = (user ? `&lens=user:${encodeURIComponent(user)}` : pool ? `&o=${pool}` : '') + (paths?.length ? `&paths=${encodeURIComponent(paths.join(','))}` : '') + (split ? '&split=roots' : '')
  // The subtree's store: its key in the query key (two mounted stores may
  // share a prefix spelling), its `store=` on the request.
  const store = useStore()
  const sfetch = useStoreFetch()
  const seriesQ = useQuery<Series>({
    queryKey: ['series', store.key, prefix, scope, scans.length],
    // Under a filter, wait for its match roots: the whole-store series is not
    // what the page asked for.
    enabled: scans.length > 1 && !(filterLabel && !paths?.length),
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const pf = perf.start('series', `${prefix || '/'}${scope}|n${scans.length}`)
      const r = await pf.track(sfetch(`/api/series?path=${encodeURIComponent(prefix)}${scope}`, { credentials: 'include' }))
      if (!r.ok) { pf.fail(); throw new Error(`series: ${r.status}`) }
      const j = await r.json() as Series
      pf.decoded()
      return j
    },
  })
  usePerfCommit('series')

  const label = user ? shortName(user) : pool ?? (prefix || 'total')
  // The x-range cut: points on or after (latest − N days). Applied to every
  // trace, so the stack, the total and the callouts agree.
  const xFrom = useMemo(() => {
    const days = X_RANGES.find(([r]) => r === xRange)![1]
    const last = seriesQ.data?.points.reduce((m, p) => Math.max(m, xOfScan(p.date)), 0) ?? 0
    return days ? last - days * 86_400_000 : -Infinity
  }, [seriesQ.data, xRange])
  const toPts = (points: { date: string; b: number }[]): Pt[] => points.map(p => ({ x: xOfScan(p.date), y: p.b })).filter(p => p.x >= xFrom).sort((a, b) => a.x - b.x)
  const total = useMemo(() => toPts(seriesQ.data?.points ?? []), [seriesQ.data, xFrom])
  // The roots' traces (split mode), keyed by bucket, coloured by slot (largest
  // at the latest scan = slot 0, as the map colours the root's children).
  const roots = useMemo(() => {
    const rs = seriesQ.data?.roots ?? []
    if (rs.length < 2) return []
    const latest = (r: { points: { date: string; b: number }[] }) => r.points[r.points.length - 1]?.b ?? 0
    const bySize = [...rs].sort((a, b) => latest(b) - latest(a))
    const slot = new Map(bySize.map((r, i) => [r.path, i]))
    return rs.map(r => ({ key: r.path, color: DEFAULT_PALETTE[slot.get(r.path)! % DEFAULT_PALETTE.length], points: toPts(r.points) }))
  }, [seriesQ.data, xFrom])
  // The x before which the total lacks a root — the total is dashed there.
  const genesis = useMemo(() => youngestGenesis(roots), [roots])
  // The relative modes' traces: each root, and the total, against its own
  // first shown point.
  const relTotal = useMemo(() => (relative ? relativeSeries([{ key: 'total', points: total }], values)[0].points : total), [relative, values, total])
  const relRoots = useMemo(() => (relative ? relativeSeries(roots, values).map((t, i) => ({ ...roots[i], points: t.points })) : roots), [relative, values, roots])
  const stacked = roots.length > 0 && layout === 'stacked' && !relative
  const totalLine = { key: 'total', label: 'total', color: 'var(--ink-3)', area: false, dots: false, strokeWidth: 1 } as const
  const series = useMemo((): TsSeries<Pt>[] => {
    if (total.length < 2) return []
    if (!roots.length) return [{ key: 'scoped', label, color: 'var(--s1)', points: relTotal }]
    if (stacked) {
      // Bands stack up to the total; the total rides along the stack's top
      // edge as a thin grey line (dashed before the youngest genesis) so the
      // tooltip lists it and the pre-genesis stretch reads as incomplete.
      // `y0: 0` keeps its tooltip value the whole total, not a band height;
      // `fit` makes the y-axis "fit" zoom to it.
      const bands: TsSeries<Pt>[] = stackSeries(roots).map((s, i) => ({ key: s.key, label: s.key, color: roots[i].color, points: s.points as Band[] }))
      return [...bands, { ...totalLine, fit: true, points: total.map(p => ({ ...p, y0: 0 })), dashBeforeX: genesis ?? undefined }]
    }
    // Lines: the total is drawn only on a relative axis, where it's the same
    // scale as its parts; in bytes it would dwarf them, so it's tooltip-only.
    return [...relRoots.map(r => ({ key: r.key, label: r.key, color: r.color, area: false, points: r.points })), { ...totalLine, plot: relative, points: relTotal, dashBeforeX: genesis ?? undefined }]
  }, [total, roots, stacked, relative, relTotal, relRoots, label, genesis])
  // A scope that owns nothing here in any scan is a flat zero line — say so
  // instead of drawing an empty axis.
  const allZero = series.length === 1 && total.every(p => p.y === 0)

  // Callouts at the points a reader looks for first: the ends of the series,
  // its extremes, and — when a range is brushed — that range's own endpoints
  // (the size at the start/end of the selection). Coinciding roles (first is
  // also max; window-end is also last) share one label. With roots they
  // annotate the total (stacked: each band's own height too, inside the band;
  // lines in bytes: nothing — the total isn't drawn and six lines' worth of
  // labels would be noise). Picking is a pure helper (`pickAnnotations`, tested).
  const fmtY = values === 'pct' ? fmtPct : relative ? (y: number) => signed(y, fmtBytes(Math.abs(y))) : fmtBytes
  const annotations = useMemo((): Annotation[] => {
    const winX = win ? [xOfScan(win[0]), xOfScan(win[1])] as [number, number] : undefined
    const radius = extrema ? radiusDays * DAY : undefined
    const floor = floorPct / 100
    if (roots.length && !stacked && !relative) return []
    const pts = roots.length ? relTotal : series.length === 1 ? series[0].points : []
    const out: Annotation[] = pickAnnotations(pts, winX, radius, floor)
      // A relative trace's first point is 0 by construction — no callout.
      .filter(c => !relative || c.x !== pts[0].x)
      // Above a stack there's nothing; below its top edge are the bands (and
      // their own callouts), so the total's labels always go above.
      .map(c => ({ x: c.x, y: c.y, label: fmtY(c.y), below: stacked ? false : c.below }))
    if (stacked) {
      for (const s of series) {
        if (s.key === 'total') continue
        for (const c of bandCallouts(s.points as Band[], radius, floor)) out.push({ x: c.x, y: c.y, y0: c.y0, label: fmtBytes(c.h) })
      }
    }
    return out
  }, [series, relTotal, roots, stacked, relative, win, extrema, radiusDays, floorPct, fmtY, fmtBytes])

  const yTickValues = useMemo(() => {
    if (values === 'pct') return undefined // the chart's own nice ticks, formatted as %
    const ys = series.filter(s => s.plot !== false).flatMap(s => s.points.map(p => p.y))
    if (!ys.length) return undefined
    const max = Math.max(0, ...ys)
    const min = relative ? Math.min(0, ...ys) : yFrom === 'data' ? Math.min(...ys) : 0
    // Fit mode pads 5% each side (TimeSeries), so tick that slightly wider range.
    const pad = yFrom === 'data' ? (max - min) * 0.05 : 0
    return unitTicks(relative ? min - pad : Math.max(0, min - pad), max + pad, units === 'iec' ? 1024 : 1000)
  }, [series, units, yFrom, values, relative])

  const firstX = total[0]?.x
  if (scans.length < 2) return null
  return (
    <section id="over-time">
      <h2>
        Size over time
        <Tooltip content={<>
          {user
            ? <><b>{shortName(user)}</b>’s bytes{prefix ? <> under <code>{prefix}</code></> : ''} per scan.</>
            : pool === 'unowned'
              ? <>Bytes no person owns{prefix ? <> under <code>{prefix}</code></> : ''}, per scan.</>
              : pool === 'owned'
                ? <>Bytes owned by a person{prefix ? <> under <code>{prefix}</code></> : ''}, per scan.</>
                : paths?.length
                  ? <>Stored bytes under “{filterLabel}” ({paths.length} {paths.length === 1 ? 'prefix' : 'prefixes'}) per scan.</>
                : prefix
                  ? <>Stored bytes under <code>{prefix}</code> per scan.</>
                  : roots.length
                    ? <>Stored bytes per scan, one {stacked ? 'band' : 'line'} per bucket{genesis != null && <> — the total is dashed before every bucket was in the scan</>}.</>
                    : <>Total stored bytes per scan ({scopeLabel}).</>}
          {relative && <> Shown as {values === 'pct' ? 'growth' : 'bytes gained'} since each trace’s first shown scan.</>}
          {' '}Each point is that scan’s own index row — exact, at any depth. Click a point to view that scan; drag to set the diff window.
        </>}>
          <span className="info" aria-label="about this chart" tabIndex={0}>ⓘ</span>
        </Tooltip>
        <XRangeToggle v={xRange} set={setXRange} />
        <ValuesToggle v={values} set={setValues} />
        {!relative && <YFromToggle v={yFrom} set={setYFrom} stacked={stacked} />}
        {roots.length > 0 && !relative && <LayoutToggle v={layout} set={setLayout} />}
        <Explain text="Annotation options">
          <button
            type="button"
            className={`gear${gearOpen ? ' on' : ''}`}
            aria-label="annotation options"
            aria-expanded={gearOpen}
            onClick={() => setGearOpen(o => !o)}
            style={{ background: 'none', border: 'none', cursor: 'pointer', font: 'inherit', color: 'inherit', opacity: gearOpen ? 1 : 0.55, padding: '0 2px' }}
          >⚙</button>
        </Explain>
      </h2>
      {gearOpen && (
        <div className="over-time-config" style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap', margin: '0 0 6px', fontSize: '0.85em', opacity: 0.92 }}>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
            <input type="checkbox" checked={extrema} onChange={e => setExtrema(e.target.checked)} />
            mark local peaks &amp; dips
          </label>
          <label
            title="Prominence-suppression radius, in days: nearby peaks/dips collapse to the more prominent one"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 8, opacity: extrema ? 1 : 0.4 }}
          >
            radius
            <input type="range" min={0.5} max={30} step={0.5} value={radiusDays} disabled={!extrema} onChange={e => setRadiusDays(+e.target.value)} />
            <b style={{ minWidth: 30, textAlign: 'right' }}>{radiusDays}</b>
          </label>
          <label
            title="Prominence floor, as a percentage of the trace's range: a peak or dip that stands out by less gets no label"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 8, opacity: extrema ? 1 : 0.4 }}
          >
            floor
            <input type="range" min={0} max={50} step={1} value={floorPct} disabled={!extrema} onChange={e => setFloorPct(+e.target.value)} />
            <b style={{ minWidth: 30, textAlign: 'right' }}>{floorPct}%</b>
          </label>
        </div>
      )}
      {seriesQ.isError && <p className="sub"><i>series unavailable</i></p>}
      {allZero ? (
        <p className="loading">
          {user ? <><b>{shortName(user)}</b> owns nothing{prefix ? <> under <code>{prefix}</code></> : ''} in any scan</>
            : pool === 'unowned' ? <>nothing{prefix ? <> under <code>{prefix}</code></> : ''} is unowned in any scan</>
              : <>nothing{prefix ? <> under <code>{prefix}</code></> : ''} in any scan</>}
        </p>
      ) : series.length > 0 ? (
        <TimeSeries<Pt>
          series={series}
          getX={p => p.x}
          getY={p => p.y}
          getY0={stacked ? p => p.y0 ?? 0 : undefined}
          formatY={fmtY}
          formatX={fmtX}
          formatTipX={fmtXTip}
          yTickValues={yTickValues}
          yFrom={relative ? 'zero' : yFrom}
          yLabel={values === 'pct' ? 'growth' : relative ? 'bytes since start' : 'stored bytes'}
          height={220}
          annotations={annotations}
          onPickX={onPickDate && (x => onPickDate(dateOfX(x)))}
          onBrush={onBrush && ((x0, x1) => onBrush(dateOfX(x0), dateOfX(x1)))}
          window={win && [xOfScan(win[0]), xOfScan(win[1])]}
        />
      ) : (
        seriesQ.isLoading ? <Skeleton height={220} label="loading series…" /> : <p className="loading">fewer than two scans hold this path</p>
      )}
      {roots.length > 0 && (
        <div className="legend roots-legend">
          {roots.map(r => (
            <span className="li" key={r.key}>
              <span className="sw" style={{ background: r.color }} />
              {r.key}
              {r.points[0] && firstX != null && r.points[0].x > firstX && <span className="since"> · since {fmtX(r.points[0].x)}</span>}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}
