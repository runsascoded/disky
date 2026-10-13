// Scan runs (specs/scan-runs-ui.md): the D1 rows `dt-cloud scan-run record |
// backfill` writes, and the pure derivations `/scans` shows — each output's
// delta against the previous scan, a run's summary row, phase lanes, links.
// DOM-free: the Functions (`_lib/scanRuns.ts`) and the pages share it.
import { exactSlug, isScanId, scanTime } from './scanSlug'

export type ScanRunStatus = 'running' | 'succeeded' | 'failed' | 'nop'

export interface ScanRun {
  run_id: string
  store: string
  scan: string
  kind: string
  parent: string | null
  status: ScanRunStatus
  failed_phase: string | null
  error: string | null
  started_ts: number | null
  finished_ts: number | null
  job_name: string | null
  region: string | null
  machine: string | null
  spot: number | null
  tasks: number | null
  image: string | null
  cost_usd: number | null
  gen: string | null
  source: 'job' | 'backfill'
  updated_ts: number
}

export interface ScanRunPhase {
  run_id: string
  phase: string
  seq: number
  started_ts: number | null
  finished_ts: number | null
  status: 'running' | 'done' | 'failed'
  note: string | null
}

export interface ScanRunOutput {
  run_id: string
  key: string
  uri: string
  bytes: number | null
  objects: number | null
  rows: number | null
  gen: string | null
}

/** An output beside the previous scan's same structure. */
export interface OutputDelta extends ScanRunOutput {
  prev_scan: string | null
  prev_bytes: number | null
  prev_rows: number | null
  delta_bytes: number | null
  delta_rows: number | null
}

/** A key's structure: the key with its own scan id abstracted, so a per-scan
 * key (`static/deltas/2026-10-08`) compares with the previous scan's. */
export const structureOf = (key: string, scan: string): string => key.split(scan).join('{scan}')

/** A key's top level (`listing/b1` → `listing`). */
export const topOf = (key: string): string => key.split('/')[0]

/** A run's elapsed seconds (to `now` while it runs); null without a start. */
/** When a run started (epoch s), for ordering runs: its `started_ts`, else its scan id's own time (a date-only id is
 *  00:00 UTC), else its `updated_ts`. `started_ts` can be NULL on rows the job recorded before it knew its start (the
 *  in-job runs before `6842ed63`), and those must not sort below every older run. */
export function runTime(r: Pick<ScanRun, 'started_ts' | 'scan' | 'updated_ts'>): number {
  if (r.started_ts != null) return r.started_ts
  const t = scanTime(r.scan)
  return Number.isNaN(t) ? r.updated_ts : Math.floor(t / 1000)
}

/** Oldest first by `runTime`, then by run id. */
export const byRunTime = (a: ScanRun, b: ScanRun): number => runTime(a) - runTime(b) || a.run_id.localeCompare(b.run_id)

export function runSecs(r: Pick<ScanRun, 'started_ts' | 'finished_ts' | 'status'>, now: number): number | null {
  if (r.started_ts == null) return null
  const end = r.finished_ts ?? (r.status === 'running' ? now : null)
  return end == null ? null : Math.max(0, end - r.started_ts)
}

/** Per scan, per structure: the output of the scan's latest-finished
 * succeeded (or still running) run that has it — what the scan's structure is. */
export function outputIndex(runs: readonly ScanRun[], outputs: readonly ScanRunOutput[]): Map<string, Map<string, ScanRunOutput>> {
  const byId = new Map(runs.map(r => [r.run_id, r]))
  const best = new Map<string, Map<string, { o: ScanRunOutput; t: number }>>()
  for (const o of outputs) {
    const r = byId.get(o.run_id)
    if (!r || (r.status !== 'succeeded' && r.status !== 'running')) continue
    const t = r.finished_ts ?? r.started_ts ?? 0
    const m = best.get(r.scan) ?? new Map()
    best.set(r.scan, m)
    const s = structureOf(o.key, r.scan)
    const cur = m.get(s)
    if (!cur || t > cur.t) m.set(s, { o, t })
  }
  return new Map([...best].map(([scan, m]) => [scan, new Map([...m].map(([s, v]) => [s, v.o]))]))
}

/** `run`'s outputs, each beside the newest earlier scan holding the same
 * structure (null deltas when none does, or a side has no number). */
export function withDeltas(run: ScanRun, outputs: readonly ScanRunOutput[], index: Map<string, Map<string, ScanRunOutput>>): OutputDelta[] {
  const earlier = [...index.keys()].filter(s => s < run.scan).sort().reverse()
  return outputs.filter(o => o.run_id === run.run_id).map(o => {
    const s = structureOf(o.key, run.scan)
    const prevScan = earlier.find(sc => index.get(sc)!.has(s)) ?? null
    const p = prevScan ? index.get(prevScan)!.get(s)! : null
    const d = (a: number | null, b: number | null | undefined) => (a != null && b != null ? a - b : null)
    return {
      ...o, prev_scan: prevScan, prev_bytes: p?.bytes ?? null, prev_rows: p?.rows ?? null,
      delta_bytes: d(o.bytes, p?.bytes), delta_rows: d(o.rows, p?.rows),
    }
  })
}

/** A phase on the run's clock: seconds from the run's start. */
export interface PhaseSpan { phase: string; start: number; end: number; status: ScanRunPhase['status']; note: string | null; lane: number }

/** A run's phases as spans, in order, each on a lane: a phase overlapping the
 * one before it (an overlapped phase, `access-ingest` beside the listing)
 * takes the next free lane. */
export function phaseSpans(run: Pick<ScanRun, 'started_ts'>, phases: readonly ScanRunPhase[]): PhaseSpan[] {
  const t0 = run.started_ts
  if (t0 == null) return []
  const laneEnds: number[] = []
  return [...phases].sort((a, b) => a.seq - b.seq).flatMap(p => {
    if (p.started_ts == null || p.finished_ts == null) return []
    const start = p.started_ts - t0, end = p.finished_ts - t0
    let lane = laneEnds.findIndex(e => e <= start)
    if (lane < 0) { lane = laneEnds.length; laneEnds.push(end) } else laneEnds[lane] = end
    return [{ phase: p.phase, start, end, status: p.status, note: p.note, lane }]
  })
}

/** Where a failed run failed: its recorded phase, else after its last phase. */
export function failedAt(run: ScanRun, phases: readonly ScanRunPhase[]): string | null {
  if (run.status !== 'failed') return null
  if (run.failed_phase) return run.failed_phase
  const last = [...phases].filter(p => p.run_id === run.run_id).sort((a, b) => b.seq - a.seq)[0]
  return last ? `after ${last.phase}` : null
}

/** One `/scans` row: an orchestrating run with its downstream jobs folded in. */
export interface RunSummary {
  run: ScanRun
  secs: number | null
  spans: PhaseSpan[]
  /** The run's top-level outputs' bytes, and their sum of deltas vs the previous scan. */
  out_bytes: number | null
  delta_bytes: number | null
  /** The downstream jobs (listing fan-out, …) and their cost. */
  downstream: number
  cost_usd: number | null
  failed_at: string | null
}

export function summarize(runs: readonly ScanRun[], phases: readonly ScanRunPhase[], outputs: readonly ScanRunOutput[], now: number): RunSummary[] {
  const index = outputIndex(runs, outputs)
  const kids = new Map<string, ScanRun[]>()
  for (const r of runs) if (r.parent) kids.set(r.parent, [...(kids.get(r.parent) ?? []), r])
  const byRun = new Map<string, ScanRunPhase[]>()
  for (const p of phases) byRun.set(p.run_id, [...(byRun.get(p.run_id) ?? []), p])
  return runs.filter(r => !r.parent || !runs.some(p => p.run_id === r.parent))
    .sort((a, b) => byRunTime(b, a))
    .map(run => {
      const tops = withDeltas(run, outputs, index).filter(o => !o.key.includes('/'))
      const sum = (xs: (number | null)[]) => (xs.some(x => x != null) ? xs.reduce<number>((a, x) => a + (x ?? 0), 0) : null)
      const ks = kids.get(run.run_id) ?? []
      const costs = [run.cost_usd, ...ks.map(k => k.cost_usd)]
      return {
        run, secs: runSecs(run, now), spans: phaseSpans(run, byRun.get(run.run_id) ?? []),
        out_bytes: sum(tops.map(o => o.bytes)), delta_bytes: sum(tops.map(o => o.delta_bytes)),
        downstream: ks.length, cost_usd: sum(costs), failed_at: failedAt(run, byRun.get(run.run_id) ?? []),
      }
    })
}

/** Phase names in their usual order (by mean seq), for a stable color per phase. */
export function phaseOrder(phases: readonly Pick<ScanRunPhase, 'phase' | 'seq'>[]): string[] {
  const acc = new Map<string, { n: number; s: number }>()
  for (const p of phases) { const a = acc.get(p.phase) ?? { n: 0, s: 0 }; a.n++; a.s += p.seq; acc.set(p.phase, a) }
  return [...acc].sort((a, b) => a[1].s / a[1].n - b[1].s / b[1].n || a[0].localeCompare(b[0])).map(([k]) => k)
}

/** The `/meta` tree's page for an output (`META_TREE_URL` = `<origin>/meta`):
 * a prefix opens its directory, an object (or a split part, `…/path-index`)
 * its parent directory. Null without a base or for a non-store URI. */
export function metaHref(base: string | null | undefined, uri: string): string | null {
  const m = base ? /^[a-z0-9]+:\/\/([^/]+)\/(.*)$/.exec(uri) : null
  if (!m) return null
  const key = m[2]
  const dir = key.endsWith('/') ? key.slice(0, -1) : key.split('/').slice(0, -1).join('/')
  return `${base!.replace(/\/$/, '')}/${m[1]}${dir ? `/${dir}` : ''}`
}

/** The map at a scan (`/?d=<slug>`): `slug` its canonical slug (`useMinSlug`),
 * else its minute (`exactSlug`; never the day slug, the day's latest scan). */
export const mapHref = (scan: string, storePath = '/', slug: (id: string) => string = exactSlug): string | null =>
  isScanId(scan) ? `${storePath.replace(/\/$/, '')}/?d=${slug(scan)}` : null

export const batchHref = (project: string, region: string, job: string, tab: 'details' | 'logs' = 'details'): string =>
  `https://console.cloud.google.com/batch/jobsDetail/regions/${region}/jobs/${job}/${tab}?project=${encodeURIComponent(project)}`

/** Logs Explorer's `;query=` is parsed as URL params: `(`/`)` must be escaped
 * too (`encodeURIComponent` leaves them), or the query is cut at the first `(`. */
export const logsQueryParam = (query: string): string => encodeURIComponent(query).replace(/\(/g, '%28').replace(/\)/g, '%29')

/** The run's logs: its Batch job's Logs tab (task + agent logs, no query to
 * get wrong), else Logs Explorer over its task logs (`labels.job_uid` is the
 * run id). */
export function logsHref(project: string, run: Pick<ScanRun, 'run_id' | 'started_ts' | 'job_name' | 'region'>): string {
  if (run.job_name && run.region) return batchHref(project, run.region, run.job_name, 'logs')
  const since = run.started_ts != null ? new Date((run.started_ts - 600) * 1000).toISOString() : null
  const query = `labels.job_uid="${run.run_id}"\nlog_id("batch_task_logs")${since ? `\ntimestamp >= "${since}"` : ''}`
  return `https://console.cloud.google.com/logs/query;query=${logsQueryParam(query)}?project=${encodeURIComponent(project)}`
}
