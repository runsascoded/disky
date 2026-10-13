// /health (specs/health-page.md): the state of the deployment's append-only stores, from `/api/health` — each store's
// stack drawn as binary-counter shards over the scan timeline (`pyrmts-react`'s `CoverTimeline`, the timeline the
// pyrmts consumers' /health pages share), every scan's coverage and its gaps, and freshness; the last scan job comes
// from /scans' own fetch (`useScanRuns`).
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CoverTimeline } from 'pyrmts-react'
import 'pyrmts-react/styles.css'
import type { PyramidTierCoverStatus } from 'pyrmts'
import { SiteNav } from './SiteNav'
import { SiteKbd } from './SiteKbd'
import { Tooltip } from './Tooltip'
import {
  type Cell, type Column, COLUMN_LABELS, coverageTiers, type HealthDoc, liveColumns, type MergeState, mergeLine, stackTiers, type StoreHealth,
} from './healthModel'
import { phaseOrder } from './scanRunsModel'
import { scanTime } from './scanSlug'
import { colorer, fmtDur, getJson, PhaseBar, Status, useScanRuns, utc } from './scanRunsUi'
import './scans.scss'
import './health.scss'

const useHealth = () => useQuery<HealthDoc, Error>({ queryKey: ['health'], queryFn: () => getJson('/api/health'), refetchInterval: 60_000, staleTime: 30_000 })

const DAY = 86_400_000
const WINDOWS = [['7d', 7], ['30d', 30], ['90d', 90], ['all', 0]] as const
type Win = typeof WINDOWS[number][0]

const STORE_NAMES: Record<StoreHealth['kind'], string> = { interval: 'Interval store', static: 'Static name index' }
const COLUMN_TIPS: Record<Column, string> = {
  path: "The scan's path sorts are served: its per-scan path store (D1 index_schema) or the interval store.",
  interval: 'The interval store serves it (expected from its base generation\'s first scan on).',
  light: "The static name index's suffix shards and catalog serve it.",
  drill: 'The static drill tier serves it (its stack stops at the first run without one).',
  anchors: 'The static anchors tier serves it (its stack stops at the first run without one).',
  r2: 'Every store listing it has its run whole on R2 (a listed run missing files there is cut by the readers).',
}

/** `n` seconds as a short age ("5h", "3d"). */
const age = (s: number | null | undefined): string => (s == null ? '—' : s < 3600 ? `${Math.max(1, Math.round(s / 60))}m` : s < 2 * 86_400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86_400)}d`)

/** Day ticks under a `CoverTimeline` (its own gridlines are monthly): the same x mapping (a 42-unit label column, then
 *  `[genesis, now]` → 1000 units), at most ~8 labels. */
function DayAxis({ genesis, now }: { genesis: number; now: number }) {
  const span = Math.max(1, now - genesis)
  const step = [1, 2, 7, 14, 30, 61].find(d => span / (d * DAY) <= 8) ?? 91
  const first = Math.ceil(genesis / DAY) * DAY
  const ticks: number[] = []
  for (let t = first; t <= now; t += step * DAY) ticks.push(t)
  const x = (t: number) => 42 + ((t - genesis) / span) * 1000
  return (
    <svg className="hl-days" viewBox="0 0 1042 12" aria-hidden>
      {ticks.map(t => <text key={t} x={x(t)} y={9} textAnchor="middle">{new Date(t).toISOString().slice(5, 10)}</text>)}
    </svg>
  )
}

/** A `CoverTimeline` with its row labels as an HTML column pinned over its left edge: narrower than the timeline (a
 *  phone) the bars scroll under it — starting at their newest end, where the runs are — and the labels stay put. The
 *  column sits on the SVG's own (42-unit) label gutter, at its row pitch (17 of 1042 units per row, 14 drawn). */
function Timeline({ tiers, genesis, now }: { tiers: PyramidTierCoverStatus[]; genesis: number; now: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const inner = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    const el = inner.current
    if (!el) return
    setW(el.clientWidth)
    const ro = new ResizeObserver(() => setW(el.clientWidth))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  useLayoutEffect(() => { const el = ref.current; if (el) el.scrollLeft = el.scrollWidth }, [genesis])
  const u = w / 1042
  return (
    <div className="hl-tl">
      <div className="hl-scroll" ref={ref}>
        <div className="hl-timeline" ref={inner}>
          <CoverTimeline tiers={tiers} genesis={genesis} now={now} />
          <DayAxis genesis={genesis} now={now} />
        </div>
      </div>
      {w > 0 && (
        <div className="hl-labels" aria-hidden>
          {tiers.map((t, i) => <span key={t.tier} style={{ top: i * 17 * u, height: 14 * u, lineHeight: `${14 * u}px`, fontSize: Math.min(11, Math.max(8, 14 * u)) }}>{t.tier}</span>)}
        </div>
      )}
    </div>
  )
}

const cellMark: Record<Cell, string> = { present: '●', missing: '✕', pending: '◐', na: '·' }

const MERGE_CHIP: Record<MergeState['status'], string> = { merging: 'running', stuck: 'warn', failed: 'failed' }
const MERGE_TIP: Record<MergeState['status'], string> = {
  merging: 'A merge job holds the lease and is building these merged runs; each is published as a revision of the newest manifest when done.',
  stuck: "The merge's status record stopped refreshing (its job was likely killed or preempted mid-merge), or it has outlived its lease. The next merge job takes the lease over once it's stale, and resumes.",
  failed: "The merge job raised (its lease is released): the store is as it was, servable. The next append's merge job plans the carries again.",
}

function MergeLine({ m, now }: { m: MergeState; now: number }) {
  return (
    <p className="hl-merge">
      <Tooltip content={MERGE_TIP[m.status]}><span className={`sr-status ${MERGE_CHIP[m.status]}`}>{m.status}</span></Tooltip>{' '}
      <span>{mergeLine(m, now)}</span>
      {m.why && <span className={m.status === 'failed' ? 'hl-err' : 'dim'}> — {m.why}</span>}
      <span className="dim"> · <code>{m.holder}</code>{m.done?.length ? <> · {m.done.length} merged so far</> : null}</span>
    </p>
  )
}

function StoreSection({ s, doc, genesis, now }: { s: StoreHealth; doc: HealthDoc; genesis: number; now: number }) {
  const tiers = useMemo(() => stackTiers(s, doc.scans, now), [s, doc.scans, now])
  const c = s.compaction
  // a carry a live (or stuck) merge has planned shows on its line, not as due
  const due = useMemo(() => {
    const planned = new Set(s.merge && s.merge.status !== 'failed' ? s.merge.merges.map(x => x.output) : [])
    return s.carries.filter(x => !planned.has(x.output))
  }, [s])
  const pct = Math.min(100, (c.run_scans / c.capacity) * 100)
  return (
    <section className="hl-store">
      <h3>{STORE_NAMES[s.kind]} <code className="dim">{s.gen}</code></h3>
      {s.error ? <p className="hl-err">Couldn't read the generation: <code>{s.error}</code></p> : (<>
        <dl className="sr-facts">
          <div><dt>base</dt><dd>{s.base.length} scans{s.base.length ? <> · {s.base[0]} → {s.base[s.base.length - 1]}</> : null}</dd></div>
          <div><dt>runs</dt><dd>{s.runs.length} · {s.runs.reduce((n, r) => n + r.scans.length, 0)} scans</dd></div>
          <div><dt>newest manifest</dt><dd>{s.manifest ? <><code>{s.manifest}</code>{s.rev ? <span className="dim"> (revision {s.rev})</span> : null} · {age(s.manifest_ts == null ? null : doc.now - s.manifest_ts)} ago</> : <span className="dim">none (the base alone)</span>}</dd></div>
          <div><dt>newest scan</dt><dd>{s.newest ?? '—'}</dd></div>
        </dl>
        <div className="hl-gauge">
          <Tooltip content={<>The runs hold {c.run_scans} of the {c.capacity} scans (2<sup>{c.level}</sup>) a compaction folds into a new base; carries stop below level {c.level}.</>}>
            <span className="hl-gauge-bar" role="meter" aria-valuemin={0} aria-valuemax={c.capacity} aria-valuenow={c.run_scans} aria-label="Scans in runs, of the compaction's"><i style={{ width: `${pct}%` }} /></span>
          </Tooltip>
          <span>compaction: {c.run_scans}/{c.capacity} scans in runs · highest run level {c.max_level < 0 ? '—' : c.max_level} of {c.level - 1}</span>
          {c.due && <span className="sr-status failed">compaction due</span>}
        </div>
        {s.merge && <MergeLine m={s.merge} now={doc.now} />}
        {due.length > 0 && (
          <p className="hl-carries">
            <span className="sr-status running">{due.length} carr{due.length === 1 ? 'y' : 'ies'} due</span>{' '}
            {due.map(x => <span key={x.output}><code>{x.output}</code> (level {x.level}, {x.inputs.length} runs)</span>)}
            <span className="dim"> — the next append's merge job builds {due.length === 1 ? 'it' : 'them'}.</span>
          </p>
        )}
        {(['light', 'drill', 'anchors'] as const).map(t => s.cut[t] && (
          <p key={t} className="hl-cut">The {t === 'light' ? (s.kind === 'interval' ? 'stack' : 'suffix tier') : `${t} tier`} is cut at <code>{s.cut[t]}</code>: its scans and every later run's aren't served from it.</p>
        ))}
        <Timeline tiers={tiers} genesis={genesis} now={now} />
      </>)}
    </section>
  )
}

function LastRun() {
  const q = useScanRuns()
  const runs = q.data?.runs ?? []
  const order = useMemo(() => phaseOrder(runs.flatMap(r => r.spans.map((s, i) => ({ phase: s.phase, seq: i })))), [runs])
  if (q.isLoading) return <span className="dim">loading…</span>
  if (q.error) return <span className="hl-err">{q.error.message}</span>
  if (!q.data?.configured) return <span className="dim">not recorded on this deployment (no <code>scan_runs</code> table)</span>
  const last = runs[0]
  if (!last) return <span className="dim">no runs yet</span>
  return (
    <span className="hl-lastrun">
      <Link to={`/scans/${encodeURIComponent(last.run.run_id)}`}><code>{last.run.scan}</code></Link>
      <Status run={last.run} failedAt={last.failed_at} />
      <span className="dim">{last.run.started_ts == null ? 'start not recorded' : `${utc(last.run.started_ts)} UTC`}</span>
      <span>{last.secs == null ? '—' : fmtDur(last.secs)}</span>
      <PhaseBar spans={last.spans} secs={last.secs} color={colorer(order)} width={140} />
    </span>
  )
}

export function HealthPage() {
  const q = useHealth()
  const [win, setWin] = useState<Win>('30d')
  const [showAll, setShowAll] = useState(false)
  useEffect(() => { document.title = 'Health' }, [])
  const doc = q.data
  const now = (doc?.now ?? 0) * 1000
  const genesis = useMemo(() => {
    const days = WINDOWS.find(w => w[0] === win)![1]
    const first = doc?.scans.length ? scanTime(doc.scans[0]) : now - DAY
    const g = days ? Math.max(first, now - days * DAY) : first
    return g - Math.max(DAY / 4, (now - g) * 0.02)
  }, [doc, win, now])
  const cols = useMemo(() => (doc ? liveColumns(doc.coverage) : []), [doc])
  const covTiers = useMemo(() => (doc ? coverageTiers(doc.coverage, cols, now) : []), [doc, cols, now])
  const gapRows = useMemo(() => (doc ? [...doc.coverage].reverse().filter(c => showAll || c.gaps.length) : []), [doc, showAll])
  const totalGaps = doc ? doc.coverage.filter(c => c.gaps.length).length : 0
  return (
    <main className="staged-page scans-page health-page">
      <SiteNav />
      <h1>Health</h1>
      <p className="sub">What this deployment serves for each scan, and how fresh it is: the path index (the map, every scan) and the append-only stores scans are added to as runs — the static name index (the map's name filter and /names) and the interval store, where configured. Each scan job's run is on <Link to="/scans">Scan runs</Link>.</p>
      {q.isLoading ? <p>Loading…</p> : q.error ? <p className="err">{q.error.message}</p> : doc && (<>
        <section className="hl-fresh">
          <h3>Freshness</h3>
          <dl className="sr-facts">
            <div><dt>newest scan</dt><dd>{doc.freshness.newest_scan ? <><code>{doc.freshness.newest_scan}</code> · {age(doc.freshness.newest_scan_age)} ago</> : '—'}</dd></div>
            {doc.freshness.stores.map(f => (
              <div key={f.kind}><dt>{STORE_NAMES[f.kind].toLowerCase()}</dt><dd>newest scan {f.newest ? <code>{f.newest}</code> : '—'}{f.newest_age != null && <> ({age(f.newest_age)} ago)</>} · last append {age(f.manifest_age)} ago</dd></div>
            ))}
            <div><dt>last scan job</dt><dd><LastRun /></dd></div>
          </dl>
        </section>
        <div className="hl-win" role="group" aria-label="Timeline window">
          <span className="dim">window</span>
          {WINDOWS.map(([w]) => <button key={w} type="button" className={w === win ? 'on' : undefined} aria-pressed={w === win} onClick={() => setWin(w)}>{w}</button>)}
          <span className="hl-legend"><i className="hl-sw present" />present <i className="hl-sw pending" />pending <i className="hl-sw missing" />missing <i className="hl-sw now" />now</span>
        </div>
        {doc.stores.length === 0 && <p className="dim">No append-only store is configured here (<code>STATIC_GEN</code> or <code>INTERVAL_STORE_GEN</code>, with an <code>INDEX_R2</code> binding), so there are no store runs to show; the coverage below is the path index's.</p>}
        {doc.stores.map(s => <StoreSection key={s.kind} s={s} doc={doc} genesis={genesis} now={now} />)}
        <section className="hl-coverage">
          <h3>Coverage per scan</h3>
          <p className="hl-gaps">
            {totalGaps ? <span className="sr-status failed">{totalGaps} scan{totalGaps === 1 ? '' : 's'} with gaps</span> : <span className="sr-status succeeded">no gaps</span>}
            {cols.filter(c => doc.gaps[c]).map(c => <Tooltip key={c} content={COLUMN_TIPS[c]}><span className="hl-gapcount">{COLUMN_LABELS[c]}: {doc.gaps[c]}</span></Tooltip>)}
          </p>
          {doc.unserved.length > 0 && (
            <p className="dim">
              <Tooltip content={<>A scan job finished for these, but nothing serves them now (not in the path index or any store's runs), e.g. scans purged since. Not counted as gaps: {doc.unserved.join(', ')}</>}>
                <span>{doc.unserved.length} older scan{doc.unserved.length === 1 ? '' : 's'} no longer served</span>
              </Tooltip>
            </p>
          )}
          {covTiers.length > 0 && <Timeline tiers={covTiers} genesis={genesis} now={now} />}
          {(gapRows.length > 0 || totalGaps < doc.coverage.length) && (
            <div className="run-filters">
              <label><input type="checkbox" checked={showAll} onChange={e => setShowAll(e.target.checked)} /> every scan</label>
              <span className="dim">{gapRows.length} / {doc.coverage.length} scans</span>
            </div>
          )}
          {gapRows.length > 0 && (
            <div className="runs-wrap">
              <table className="runs hl-cov">
                <thead><tr><th>scan</th>{cols.map(c => <th key={c} className="hl-c"><Tooltip content={COLUMN_TIPS[c]}><span>{COLUMN_LABELS[c]}</span></Tooltip></th>)}</tr></thead>
                <tbody>
                  {gapRows.map(r => (
                    <tr key={r.scan}>
                      <td className="nb"><code>{r.scan}</code></td>
                      {cols.map(c => <td key={c} className={`hl-c hl-${r.cells[c]}`} aria-label={`${COLUMN_LABELS[c]}: ${r.cells[c]}`}>{cellMark[r.cells[c]]}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="sr-legend"><span><b className="hl-present">●</b> served</span><span><b className="hl-missing">✕</b> gap</span><span><b className="hl-pending">◐</b> pending (its job is running)</span><span><b className="hl-na">·</b> not expected</span></p>
        </section>
        <p className="dim hl-note">Computed {age(Math.max(0, Date.now() / 1000 - doc.now))} ago from <code>INDEX_R2</code> (each store's <code>scans.json</code>, newest manifest and run files) and D1 (<code>index_schema</code>, <code>scan_runs</code>). A merge in progress is its job's status record beside each store (<code>merging.json</code>, refreshed every 5 min); one not refreshed for 30 min, or held past its 4 h lease, is stuck.</p>
      </>)}
      <SiteKbd />
    </main>
  )
}
