import { useEffect, useState } from 'react'
import { PHASES, WIDGETS, perf, type PerfEntry, type Widget } from '../perf'

// `?perf=1`: the time-to-render panel (specs/render-bench.md §2.1). One row
// per widget — its most recently active load — as a stacked bar of the four
// spans (wait | decode | paint | settle) on a shared scale, the total in ms,
// the server's own phases (from `Server-Timing`) and the cache tier beneath.
// Redraws on every stamp. Reads the same ledger the console (`__perf.table()`)
// and the bench runner do.

const LABEL: Record<Widget, string> = {
  treemap: 'treemap', table: 'table', dtm: 'diff map', dtable: 'diff table', series: 'over time', age: 'age',
}

/** When this load last did anything: its request plus every span known so far. */
const lastActivity = (e: PerfEntry): number => e.start + (e.wait ?? 0) + (e.decode ?? 0) + (e.paint ?? 0) + (e.settle ?? 0)

const fmtMs = (ms: number | null): string => (ms == null ? '–' : ms >= 100 ? `${Math.round(ms).toLocaleString()}` : ms.toFixed(1))

export function PerfOverlay() {
  const [, bump] = useState(0)
  const [open, setOpen] = useState(true)
  useEffect(() => perf.subscribe(() => bump(n => n + 1)), [])
  if (!open) return null
  const entries = perf.entries()
  const rows = WIDGETS.flatMap(w => {
    const ls = entries.filter(e => e.widget === w)
    if (!ls.length) return []
    return [{ w, n: ls.length, e: ls.reduce((a, b) => (lastActivity(b) >= lastActivity(a) ? b : a)) }]
  })
  const now = performance.now()
  // A load still in flight fills its bar up to now, so the scale holds while
  // it lands instead of jumping when it does.
  const extent = (e: PerfEntry) => (e.done || e.failed ? e.total ?? lastActivity(e) - e.start : now - e.start)
  const scale = Math.max(1, ...rows.map(r => extent(r.e)))
  return (
    <div className="perf-ov" role="region" aria-label="time to render">
      <div className="head">
        <span>perf</span>
        <span className="legend">{PHASES.map(p => <span key={p} className={`sw ${p}`}>{p}</span>)}</span>
        <button type="button" className="x" title="clear" aria-label="clear" onClick={() => perf.reset()}>↺</button>
        <button type="button" className="x" title="close" aria-label="close" onClick={() => setOpen(false)}>×</button>
      </div>
      {rows.length === 0 && <div className="empty">no widget loads yet</div>}
      {rows.map(({ w, n, e }) => {
        const state = e.failed ? 'failed' : e.empty ? 'empty' : e.done ? '' : 'loading…'
        const server = Object.entries(e.server)
        return (
          <div key={w} className={'row' + (e.failed ? ' failed' : '')}>
            <div className="hd">
              <b>{LABEL[w]}</b>
              {n > 1 && <span className="n">×{n}</span>}
              <span className="key" title={e.key}>{e.key}</span>
              <span className="tot">{e.done ? `${fmtMs(e.total)} ms${e.empty ? ' · empty' : ''}` : state}</span>
              {e.cache && <span className={`cache ${e.cache}`}>{e.cache}</span>}
            </div>
            <div className="bar" title={PHASES.map(p => `${p} ${fmtMs(e[p])}`).join(' · ')}>
              {PHASES.map(p => (e[p] != null && e[p]! > 0
                ? <span key={p} className={`seg ${p}`} style={{ width: `${(100 * e[p]!) / scale}%` }} />
                : null))}
              {!e.done && !e.failed && <span className="seg open" style={{ width: `${(100 * (now - lastActivity(e))) / scale}%` }} />}
            </div>
            <div className="sub">
              {PHASES.map(p => <span key={p}>{p} <b>{fmtMs(e[p])}</b></span>)}
              {server.length > 0 && <span className="srv">server: {server.map(([k, v]) => `${k} ${v}`).join(' · ')}</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}
