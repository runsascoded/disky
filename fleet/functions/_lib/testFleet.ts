/** A four-deployment fleet as the deployments' own routes would answer, for the specs and the card goldens
 *  (`scripts/golden.ts`): a week of daily scans each, some with a step issue still open, one failed; `/meta` usage on
 *  two, refused (403) on the others, as gcs and cw answer a read grant today. Deterministic. */
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

interface RunSpec { day: number; hour: number; hours: number; cost: number | null; open?: string[]; status?: string }
const run = (name: string, r: RunSpec) => {
  const started = day0 - r.day * DAY + r.hour * HOUR
  return {
    run: { run_id: `${scanId(started)}-${name}`, scan: scanId(started), kind: 'scan', status: r.status ?? 'succeeded', started_ts: started },
    secs: Math.round(r.hours * HOUR),
    cost_usd: r.cost,
    failed_at: r.status === 'failed' ? 'index' : null,
    phase_issues: (r.open ?? []).map(phase => ({ phase, outcome: 'failed', recovery: null, passing: null })),
  }
}

/** Newest first, as `/api/scan-runs` lists them; a compaction (maintenance) run among them, which the fleet skips. */
const RUNS: Record<string, unknown[]> = {
  gcs: [
    run('gcs', { day: 0, hour: 7, hours: 2.13, cost: null }),
    run('gcs', { day: 1, hour: 7, hours: 2.14, cost: null, open: ['dups'] }),
    run('gcs', { day: 2, hour: 7, hours: 2.76, cost: null, open: ['dups', 'probe'] }),
    run('gcs', { day: 3, hour: 7, hours: 2.18, cost: null }),
    run('gcs', { day: 4, hour: 7, hours: 2.21, cost: null }),
  ],
  cw: [
    { run: { run_id: 'compact-1', scan: scanId(day0), kind: 'compact', status: 'succeeded', started_ts: day0 + 12 * HOUR }, secs: 600, cost_usd: 0.05, failed_at: null, phase_issues: [] },
    run('cw', { day: 0, hour: 1, hours: 1.53, cost: 0.9, open: ['warm-cache'] }),
    run('cw', { day: 1, hour: 1, hours: 1.63, cost: 0.97, open: ['warm-cache'] }),
    run('cw', { day: 2, hour: 1, hours: 1.75, cost: 0.99 }),
    run('cw', { day: 3, hour: 1, hours: 1.78, cost: 1.01, status: 'failed' }),
    run('cw', { day: 4, hour: 1, hours: 1.58, cost: 0.1 }),
  ],
  hf: [0, 1, 2, 3, 4, 5, 6].map(day => run('hf', { day, hour: 5, hours: 0.6 + (day % 3) * 0.02, cost: 0.08 - (day % 2) * 0.004 })),
  r2: [0, 1, 2, 3, 4, 5, 6].map(day => run('r2', { day, hour: 5, hours: 0.5 + (day % 4) * 0.05, cost: 0.13 + (day % 3) * 0.01 })),
}

const series = (start: number, growth: number) =>
  ({ points: [9, 8, 7, 6, 5, 4, 3, 2, 1, 0].map(k => ({ date: scanId(day0 - k * DAY), b: Math.round(start * growth ** (9 - k)), o: 1000 + k })) })

const META: Record<string, unknown> = {
  hf: series(450e6, 1.078),
  r2: series(18.4e9, 1.072),
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
