/**
 * The fleet document (specs/disky-oa-dev.md): each deployment's state, read from its own site with its read token —
 * `/api/health` (freshness, coverage gaps), `/api/scan-runs` (what each scan job did) and `/api/series?store=meta` (its
 * `/meta` store's bytes per scan) — and folded into one JSON. A deployment that can't be read, wholly or in part, says
 * which part and why (`errors`); it never fails the document.
 *
 * The wire types below are the fields read from each route's answer (`site/functions/api/health.ts`,
 * `api/scan-runs/[[path]].ts`, `api/series.ts`), nothing more.
 */
import type { Env } from './env.js'
import type { CostDay, FleetDeployment, FleetDoc, FleetMeta, FleetRun, MetaPoint, ObjPoint } from './fleetTypes.js'

export type { CostDay, FleetDeployment, FleetDoc, FleetHealth, FleetMeta, FleetRun, MetaPoint } from './fleetTypes.js'

export interface Deployment {
  name: string
  /** The deployment's site origin, e.g. `https://gcs.oa.dev`. */
  url: string
  /** The secret (an env name) holding its read token. */
  token: string
  color: string
  /** Its admin channel (a Slack id, e.g. `#<deployment>-admin`'s): the post links it. */
  channel?: string
}

/** `FLEET_JSON`'s deployments. Throws on anything but a list of `{name, url, token, color[, channel]}` with distinct names. */
export function parseFleet(raw: string | undefined): Deployment[] {
  if (!raw) return []
  const list: unknown = JSON.parse(raw)
  if (!Array.isArray(list)) throw new Error('FLEET_JSON: not a list')
  const out = list.map((d: Record<string, unknown>, i) => {
    for (const k of ['name', 'url', 'token', 'color']) if (typeof d?.[k] !== 'string' || !d[k]) throw new Error(`FLEET_JSON[${i}]: no ${k}`)
    if (d.channel !== undefined && (typeof d.channel !== 'string' || !/^[CG][A-Z0-9]{6,}$/.test(d.channel))) throw new Error(`FLEET_JSON[${i}]: channel must be a Slack channel id`)
    return {
      name: d.name as string, url: (d.url as string).replace(/\/+$/, ''), token: d.token as string, color: d.color as string,
      ...(d.channel ? { channel: d.channel as string } : {}),
    }
  })
  const names = out.map(d => d.name)
  if (new Set(names).size !== names.length) throw new Error('FLEET_JSON: duplicate names')
  return out
}

// ── what each route answers (the fields read) ─────────────────────────────

interface HealthWire { freshness: { newest_scan: string | null; newest_scan_age: number | null }; gaps: Record<string, number> }
interface IssueWire { phase: string; recovery: unknown; passing?: unknown }
export interface RunWire {
  run: {
    run_id: string; scan: string; kind: string; status: string; started_ts: number | null; trigger?: string | null
    /** The run's reply in `#<deployment>-admin` (a Slack permalink, `scan_runs.thread_url`); absent from a site or D1
     *  that predates it (migration `scan-run-thread-url`). */
    thread_url?: string | null
    /** A manual run's starter's Slack emoji (`emoji=1`): `:name:`. */
    trigger_emoji?: string | null
  }
  secs: number | null
  cost_usd: number | null
  failed_at: string | null
  phase_issues: IssueWire[]
}
interface RunsWire { configured: boolean; runs: RunWire[] }
/** `/api/series`' points: `bytes` (specs/descriptive-field-names.md §3d), or `b` from a deployment whose site predates the
 *  rename. Drop `b` once every deployment serves `bytes` (the spec's follow-up list). */
interface SeriesWire { points: { date: string; bytes?: number; b?: number; objects?: number; o?: number }[] }

// ── the document (its shape: `fleetTypes.ts`) ─────────────────────────────

/** A cap on runs per deployment (`FLEET_RUNS`); the runs kept are those started in the last `HISTORY_DAYS`, whatever the
 *  deployment's scan rate, plus its newest even when older. */
export const DEFAULT_RUNS = 200
/** How far back the runs, `FleetMeta.daily` and `objects` reach: the page's 30-day charts (the card draws the last
 *  `WINDOW_DAYS` of it). */
export const HISTORY_DAYS = 30
/** The scan job's own runs (`kind = 'scan'`, `dt-cloud scan-run`'s default). A deployment's other runs are listed beside them
 *  on its `/scans` but aren't new scans here: maintenance (`compact`, `race`, `merge`), the access-log ingest, re-runs over an
 *  existing scan's outputs (`reproc`) and phase recoveries. */
const SCAN_KIND = 'scan'
/** A deployment that doesn't answer within this is an error, not a stalled document. */
export const FETCH_TIMEOUT_MS = 20_000
const DAY = 86400

const SCAN_ID = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?$/
/** A scan id's time (epoch s, UTC), null for anything else. */
export function scanTime(id: string): number | null {
  const m = SCAN_ID.exec(id)
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] ?? 0), +(m[5] ?? 0)) / 1000 : null
}

/** A Slack emoji as `/api/scan-runs?emoji=1` answers it; nothing else is passed on into the post. */
const SLACK_EMOJI = /^:[a-z0-9_+'-]{1,100}:$/

export const fleetRun = (s: RunWire): FleetRun => ({
  run_id: s.run.run_id,
  scan: s.run.scan,
  status: s.run.status,
  trigger: s.run.trigger === 'schedule' || s.run.trigger === 'manual' ? s.run.trigger : null,
  started: s.run.started_ts,
  secs: s.secs,
  cost_usd: s.cost_usd,
  failed_at: s.failed_at,
  open_issues: s.phase_issues.filter(i => !i.recovery && !i.passing).map(i => i.phase),
  ...(s.run.thread_url !== undefined ? { thread_url: s.run.thread_url } : {}),
  ...(s.run.trigger === 'manual' && s.run.trigger_emoji && SLACK_EMOJI.test(s.run.trigger_emoji) ? { trigger_emoji: s.run.trigger_emoji } : {}),
})

/** 00:00 UTC of the first day of the month before `now`'s (epoch s). */
export function prevMonthStart(now: number): number {
  const d = new Date(now * 1000)
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - 1, 1) / 1000
}

/** Each UTC day's summed cost of `runs` (scan runs, finished, with a cost) since the start of the previous month, oldest
 *  first: the cost reply compares this month's running total with last month's. */
export function costDaily(runs: RunWire[], now: number): CostDay[] {
  const from = prevMonthStart(now)
  const days = new Map<number, number>()
  for (const s of runs) {
    const t = s.run.started_ts
    if (s.run.kind !== SCAN_KIND || s.run.status === 'nop' || s.run.status === 'running' || s.cost_usd === null || t === null || t < from) continue
    const day = Math.floor(t / DAY) * DAY
    days.set(day, (days.get(day) ?? 0) + s.cost_usd)
  }
  return [...days].sort(([a], [b]) => a - b).map(([t, usd]) => ({ t, usd }))
}

/** The meta store's points as the document carries them, relative to `now`. */
export function fleetMeta(points: SeriesWire['points'], now: number): FleetMeta {
  const pts = points.flatMap(p => { const t = scanTime(p.date); return t === null ? [] : [{ scan: p.date, t, bytes: p.bytes ?? p.b ?? 0 }] }).sort((a, b) => a.t - b.t)
  const at = (cut: number): number | null => { const p = pts.filter(q => q.t <= cut).pop(); return p ? p.bytes : null }
  const byDay = new Map<string, MetaPoint>()
  for (const p of pts) if (p.t >= now - HISTORY_DAYS * DAY) byDay.set(new Date(p.t * 1000).toISOString().slice(0, 10), p)
  return { now: pts.length ? pts[pts.length - 1].bytes : null, d1: at(now - DAY), d7: at(now - 7 * DAY), daily: [...byDay.values()] }
}

/** The primary store's object counts, the newest scan per UTC day over `HISTORY_DAYS` (what drives scan time). */
export function fleetObjects(points: SeriesWire['points'], now: number): ObjPoint[] {
  const byDay = new Map<string, ObjPoint>()
  for (const p of points) {
    const t = scanTime(p.date)
    const objects = p.objects ?? p.o
    if (t === null || objects == null || t < now - HISTORY_DAYS * DAY) continue
    const day = new Date(t * 1000).toISOString().slice(0, 10)
    const was = byDay.get(day)
    if (!was || was.t <= t) byDay.set(day, { scan: p.date, t, objects })
  }
  return [...byDay.values()].sort((a, b) => a.t - b.t)
}

type Fetcher = typeof fetch

async function getJson<T>(f: Fetcher, url: string, token: string): Promise<T> {
  const r = await f(url, { headers: { authorization: `Bearer ${token}`, accept: 'application/json' }, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json() as Promise<T>
}

const why = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/** One deployment's part of the document. */
export async function readDeployment(d: Deployment, env: Env, now: number, nRuns: number, f: Fetcher = fetch): Promise<FleetDeployment> {
  const out: FleetDeployment = { name: d.name, url: d.url, color: d.color, ...(d.channel ? { channel: d.channel } : {}), health: null, last: null, runs: [], meta: null, objects: null, errors: {} }
  const token = env[d.token]
  if (typeof token !== 'string' || !token) {
    const e = `no token (${d.token} unset)`
    out.errors = { health: e, runs: e, meta: e, objects: e }
    return out
  }
  await Promise.all([
    getJson<HealthWire>(f, `${d.url}/api/health`, token).then(h => {
      out.health = { newest_scan: h.freshness.newest_scan, newest_scan_age: h.freshness.newest_scan_age, gaps: Object.values(h.gaps).reduce((s, n) => s + n, 0) }
    }, e => { out.errors.health = why(e) }),
    // `emoji=1`: each manual run's starter's Slack emoji, which the post shows in place of 👤
    getJson<RunsWire>(f, `${d.url}/api/scan-runs?emoji=1`, token).then(r => {
      if (!r.configured) { out.errors.runs = 'scan runs are not set up'; return }
      const since = now - HISTORY_DAYS * DAY
      out.runs = r.runs.filter(s => s.run.kind === SCAN_KIND && s.run.status !== 'nop')
        .filter((s, i) => i === 0 || (s.run.started_ts ?? 0) >= since).slice(0, nRuns).map(fleetRun)
      out.last = out.runs[0] ?? null
      out.cost_daily = costDaily(r.runs, now)
    }, e => { out.errors.runs = why(e) }),
    getJson<SeriesWire>(f, `${d.url}/api/series?store=meta`, token).then(s => {
      out.meta = fleetMeta(s.points, now)
    }, e => { out.errors.meta = why(e) }),
    getJson<SeriesWire>(f, `${d.url}/api/series`, token).then(s => {
      out.objects = fleetObjects(s.points, now)
    }, e => { out.errors.objects = why(e) }),
  ])
  return out
}

export const runsOf = (env: Env): number => {
  const n = Number(env.FLEET_RUNS)
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_RUNS
}

/** The whole document, read now. */
export async function readFleet(env: Env, now: number, f: Fetcher = fetch): Promise<FleetDoc> {
  const deployments = parseFleet(env.FLEET_JSON)
  return { now, days: HISTORY_DAYS, deployments: await Promise.all(deployments.map(d => readDeployment(d, env, now, runsOf(env), f))) }
}

/** How long a read document stands at the edge (s). */
export const FLEET_TTL = 300

const colo = (): Cache | null => (typeof caches === 'undefined' ? null : (caches as unknown as { default: Cache }).default)

/** The document, from the colo cache when a read within `FLEET_TTL` is there (`fresh` skips it and refills it). Keyed
 *  by the deployment list and run count, so a config change reads anew. */
export async function fleetDoc(env: Env, opts: { fresh?: boolean; waitUntil?: (p: Promise<unknown>) => void } = {}, f: Fetcher = fetch): Promise<FleetDoc> {
  const cache = colo()
  const key = new Request(`https://fleet.cache/v3/${encodeURIComponent(env.FLEET_JSON ?? '')}/${runsOf(env)}`)
  if (cache && !opts.fresh) {
    const hit = await cache.match(key)
    if (hit) return hit.json() as Promise<FleetDoc>
  }
  const doc = await readFleet(env, Math.floor(Date.now() / 1000), f)
  if (cache) {
    const put = cache.put(key, new Response(JSON.stringify(doc), { headers: { 'content-type': 'application/json', 'cache-control': `max-age=${FLEET_TTL}` } }))
    if (opts.waitUntil) opts.waitUntil(put)
    else await put
  }
  return doc
}
