/** A four-deployment fleet as the deployments' own routes would answer, for the specs and the card goldens
 *  (`scripts/golden.mjs`) and the page's local mock (`FLEET_MOCK=1 pnpm dev`): 30 days of daily scans each, some with a
 *  step issue still open, a few failed; `/meta` usage on two, refused (403) on the others, as gcs and cw answer a read
 *  grant today; two hand-started cw runs on one day this week, one earlier. Deterministic. */
import type { Env } from './env.js'

/** 2026-11-07 14:00 UTC. */
export const NOW = Date.UTC(2026, 10, 7, 14) / 1000
const DAY = 86400
const HOUR = 3600

export const FLEET = [
  { name: 'gcs', url: 'https://gcs.example.test', token: 'FLEET_TOKEN_GCS', color: '#EA4335' },
  { name: 'cw', url: 'https://cw.example.test', token: 'FLEET_TOKEN_CW', color: '#4C5FF0' },
  { name: 'hf', url: 'https://hf.example.test', token: 'FLEET_TOKEN_HF', color: '#FFD21E' },
  { name: 'r2', url: 'https://r2.example.test', token: 'FLEET_TOKEN_R2', color: '#F38020' },
]

export const fleetEnv = (extra: Partial<Env> = {}): Env => ({
  ASSETS: { fetch: async () => new Response(null, { status: 404 }) },
  FLEET_JSON: JSON.stringify(FLEET),
  FLEET_TOKEN_GCS: 'tok-gcs',
  FLEET_TOKEN_CW: 'tok-cw',
  FLEET_TOKEN_HF: 'tok-hf',
  FLEET_TOKEN_R2: 'tok-r2',
  ...extra,
})

const day0 = Math.floor(NOW / DAY) * DAY
const scanId = (t: number) => new Date(t * 1000).toISOString().slice(0, 10)

interface RunSpec { day: number; hour: number; hours: number; cost: number | null; open?: string[]; status?: string; manual?: boolean; thread?: boolean }
const run = (name: string, r: RunSpec) => {
  const started = day0 - r.day * DAY + r.hour * HOUR
  return {
    run: {
      run_id: `${scanId(started)}${r.manual ? `T${String(r.hour).padStart(2, '0')}00` : ''}-${name}`, scan: scanId(started), kind: 'scan', status: r.status ?? 'succeeded', started_ts: started, trigger: r.manual ? 'manual' : 'schedule',
      ...(r.thread ? { thread_url: `https://slack.example.test/archives/C0${name.toUpperCase()}ADMIN/p${started}000100` } : {}),
    },
    secs: Math.round(r.hours * HOUR),
    cost_usd: r.cost,
    failed_at: r.status === 'failed' ? 'index' : null,
    phase_issues: (r.open ?? []).map(phase => ({ phase, outcome: 'failed', recovery: null, passing: null })),
  }
}

/** Days of history the fixture carries (the fleet's `HISTORY_DAYS`); the card draws only the newest week of it. */
const DAYS = 30
const days = (from: number) => Array.from({ length: DAYS - from }, (_, k) => from + k)

/** Newest first, as `/api/scan-runs` lists them; a compaction (maintenance) run among them, which the fleet skips. gcs
 *  records no cost and posts no run threads; the others' runs each have a thread. */
const RUNS: Record<string, unknown[]> = {
  gcs: [
    run('gcs', { day: 0, hour: 7, hours: 2.13, cost: null }),
    run('gcs', { day: 1, hour: 7, hours: 2.14, cost: null, open: ['dups'] }),
    run('gcs', { day: 2, hour: 7, hours: 2.76, cost: null, open: ['dups', 'probe'] }),
    run('gcs', { day: 3, hour: 7, hours: 2.18, cost: null }),
    run('gcs', { day: 4, hour: 7, hours: 2.21, cost: null }),
    ...days(7).map(day => run('gcs', { day, hour: 7, hours: 2.05 + (day % 5) * 0.04, cost: null, status: day === 16 ? 'failed' : undefined })),
  ],
  cw: [
    { run: { run_id: 'compact-1', scan: scanId(day0), kind: 'compact', status: 'succeeded', started_ts: day0 + 12 * HOUR }, secs: 600, cost_usd: 0.05, failed_at: null, phase_issues: [] },
    run('cw', { day: 0, hour: 1, hours: 1.53, cost: 0.9, open: ['warm-cache'], thread: true }),
    run('cw', { day: 1, hour: 1, hours: 1.63, cost: 0.97, open: ['warm-cache'], thread: true }),
    run('cw', { day: 2, hour: 15, hours: 1.7, cost: 1.05, manual: true, thread: true }),
    run('cw', { day: 2, hour: 9, hours: 1.72, cost: 1.02, manual: true, thread: true }),
    run('cw', { day: 2, hour: 1, hours: 1.75, cost: 0.99, thread: true }),
    run('cw', { day: 3, hour: 1, hours: 1.78, cost: 1.01, status: 'failed', thread: true }),
    run('cw', { day: 4, hour: 1, hours: 1.58, cost: 0.1, thread: true }),
    ...days(7).flatMap(day => [
      ...(day === 11 ? [run('cw', { day, hour: 13, hours: 1.66, cost: 0.98, manual: true, thread: true })] : []),
      run('cw', { day, hour: 1, hours: 1.5 + (day % 4) * 0.06, cost: 0.92 + (day % 3) * 0.03, open: day === 9 ? ['probe'] : undefined, thread: true }),
    ]),
  ],
  hf: days(0).map(day => run('hf', { day, hour: 5, hours: 0.6 + (day % 3) * 0.02, cost: 0.08 - (day % 2) * 0.004, thread: true })),
  r2: days(0).map(day => run('r2', { day, hour: 5, hours: 0.5 + (day % 4) * 0.05, cost: 0.13 + (day % 3) * 0.01, thread: true })),
}

/** A store's points, one a day over `DAYS`, oldest first: `start` 9 days back, growing by `growth` a day. */
const series = (start: number, growth: number) =>
  ({ points: days(0).reverse().map(k => ({ date: scanId(day0 - k * DAY), bytes: Math.round(start * growth ** (9 - k)), objects: 1000 + k })) })

const META: Record<string, unknown> = {
  hf: series(450e6, 1.078),
  r2: series(18.4e9, 1.072),
}

/** Each primary store's root series: its object count (what scan time tracks) growing scan over scan. */
const objSeries = (start: number, growth: number) =>
  ({ points: days(0).reverse().map(k => ({ date: scanId(day0 - k * DAY), bytes: 1, objects: Math.round(start * growth ** (9 - k)) })) })

const PRIMARY: Record<string, unknown> = {
  gcs: objSeries(148e6, 1.004),
  cw: objSeries(120e6, 1.03),
  hf: objSeries(271e3, 1.003),
  r2: objSeries(3.5e6, 1.006),
}

const health = (newest: string, age: number, gaps: Record<string, number>) => ({ now: NOW, freshness: { newest_scan: newest, newest_scan_age: age }, gaps, scans: [], stores: [] })

const HEALTH: Record<string, unknown> = {
  gcs: health(scanId(day0), 7 * HOUR, { path: 0, search: 1 }),
  cw: health(scanId(day0), 13 * HOUR, { path: 0, search: 0 }),
  hf: health(scanId(day0), 9 * HOUR, {}),
  r2: health(scanId(day0), 9 * HOUR, { path: 0 }),
}

/** Each request the fleet makes, by URL: what the deployment answers, or `null` for a 403. */
export const ANSWERS: Record<string, unknown> = Object.fromEntries(FLEET.flatMap(d => [
  [`${d.url}/api/health`, HEALTH[d.name]],
  [`${d.url}/api/scan-runs`, { configured: true, runs: RUNS[d.name] }],
  [`${d.url}/api/series?store=meta`, META[d.name] ?? null],
  [`${d.url}/api/series`, PRIMARY[d.name] ?? null],
]))

export interface Seen { url: string; authorization: string | null }

/** An answer that is only a status. */
export class Status { constructor(readonly status: number) {} }

/** A `fetch` that answers from `answers` (a missing URL: 404; `null`: 403; a `Status`: that status), recording each
 *  request in `seen`. */
export function stubFetch(answers: Record<string, unknown>, seen: Seen[] = []): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    seen.push({ url, authorization: new Headers(init?.headers).get('authorization') })
    if (!(url in answers)) return new Response('not found', { status: 404 })
    const body = answers[url]
    if (body instanceof Status) return new Response('{"error":"x"}', { status: body.status })
    return body === null ? new Response('{"error":"forbidden"}', { status: 403 }) : Response.json(body)
  }) as typeof fetch
}
