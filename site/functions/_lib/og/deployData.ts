/** A deployment's status card (`@disk-tree/pages-kit/og/deployCard`, open question #87) as its own site draws it: the
 * head of the run thread in `#<deploy>-admin` carries it (`dt_cloud.nominal`), the fleet's post its twin. Its input is
 * what the fleet reads of this site, read here in-process: the store's scan runs (`/api/scan-runs`' `loadScanRuns` +
 * `scanRunsList`) and its root series (`/api/series`' `seriesRead`, ungated as the size-over-time card reads it), each
 * reduced as the fleet document keeps it (`cardRuns`, `dailyPoints`).
 *
 * Its URL (`deployCardUrl`, which `GET /api/scan-runs/card-url` mints for the job's grant) is signed under
 * `SESSION_SECRET` for `IMAGE_TTL_DAYS`, as the page cards are, and carries no credential: Slack fetches it with none.
 * `v` is a digest of the input it was minted over, so a new run (or a new scan's sizes) is a new URL, which Slack
 * fetches anew, and a report with nothing new mints the same URL that day. No wasm here (`deploy.ts` renders). */
import type { D1Database } from '@cloudflare/workers-types'
import { type CardPoint, type CardRun, cardRuns, dailyPoints } from '@disk-tree/pages-kit/og/deployCard'
import type { Env } from '../auth.js'
import { parseLabels } from '../costLabels.js'
import { loadScanRuns, scanRunsList } from '../scanRuns.js'
import { isPrimary, storeKey } from '../stores.js'
import { seriesRead } from '../../api/series.js'
import { scanTime } from '../../../src/scanSlug.js'
import { expDay, IMAGE_TTL_DAYS, imagePath } from './sign.js'

/** The card's kind in its URL: `/og/deploy.png`. */
export const DEPLOY_KIND = 'deploy'
/** The card's dot when the request names no colour: a neutral grey (the fleet's own colour for a deployment is its
 *  `FLEET_JSON`'s, which a site doesn't hold; the job passes it, `nominal.color`). */
export const DEFAULT_COLOR = '#8b8a80'
const COLOR_RE = /^#?([0-9a-fA-F]{6})$/

/** A request's `color` (`#rrggbb` or `rrggbb`) as the signed `c` param (6 lowercase hex digits), or null (none, or not
 *  a colour). */
export const colorParam = (raw: string | null): string | null => (raw ? COLOR_RE.exec(raw)?.[1].toLowerCase() ?? null : null)

export type DeployEnv = Env & { DB?: D1Database; DISKY_LABELS?: string; ROOT_LABEL?: string }

/** What the card reads, minus its theme, colour and time. */
export interface DeployData { name: string; runs: CardRun[]; points: CardPoint[]; runsError?: string }

/** The card's title: the deployment (`DISKY_LABELS`' `deployment`, else the root's label), and a secondary store's key. */
export function deployName(env: DeployEnv): string {
  let deployment: string | undefined
  try {
    deployment = parseLabels(env.DISKY_LABELS).deployment
  } catch (e) {
    console.log(`deploy card: ${(e as Error).message}`)
  }
  const name = deployment ?? env.ROOT_LABEL ?? 'storage'
  return isPrimary(env) ? name : `${name} · ${storeKey(env)}`
}

/** `/api/series`' root points for `env`'s store as the card takes them (epoch s; a point whose scan id isn't a time is
 *  dropped); none when the series can't be read (logged: the strip draws "n/a"). */
export async function seriesPoints(env: DeployEnv, origin: string, now: number): Promise<CardPoint[]> {
  const r = await seriesRead({ request: new Request(`${origin}/api/series`), env }, async () => null)
  if (!r.ok) {
    console.log(`deploy card: series ${r.status} ${(await r.text().catch(() => '')).slice(0, 200)}`)
    return []
  }
  const body = await r.json() as { points: { date: string; bytes: number; objects: number }[] }
  const pts = body.points.flatMap(p => {
    const t = scanTime(p.date)
    return Number.isFinite(t) ? [{ t: t / 1000, bytes: p.bytes, objects: p.objects }] : []
  })
  return dailyPoints(pts, now)
}

/** Where the card's sizes come from: the store's series (`seriesPoints`); a test or a render script passes its own. */
export type SeriesSource = (env: DeployEnv, origin: string, now: number) => Promise<CardPoint[]>

/** The card's input for `env`'s store (its env as the data Functions see it: `withStore`, `withPathStore`). A store with
 *  no scan-run tables, or no D1, draws its header's "scan runs n/a"; a series that can't be read, an empty strip. */
export async function deployData(env: DeployEnv, origin: string, now: number, series: SeriesSource = seriesPoints): Promise<DeployData> {
  const name = deployName(env)
  const [rows, points] = await Promise.all([
    env.DB ? loadScanRuns(env.DB, storeKey(env)) : Promise.resolve(null),
    series(env, origin, now),
  ])
  if (!rows) return { name, runs: [], points, runsError: env.DB ? 'scan runs are not set up' : 'no D1 bound' }
  return { name, runs: cardRuns(scanRunsList(rows, now).runs, now), points }
}

/** `data`'s digest: 12 hex digits of SHA-256 over its JSON, a running run's clock left out (its duration and step split
 *  grow every second; the image, drawn when fetched, shows them then), so reports during one run mint one URL. */
export async function deployVersion(data: DeployData): Promise<string> {
  const runs = data.runs.map(r => (r.status === 'running' ? { scan: r.scan, status: r.status, trigger: r.trigger, started: r.started } : r))
  const h = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify({ ...data, runs }))))
  return [...h.slice(0, 6)].map(b => b.toString(16).padStart(2, '0')).join('')
}

/** The card's signed params: its store (none for the primary), theme (`light`; none = dark), colour and version. */
export const deployParams = (env: DeployEnv, opts: { theme?: 'light'; c?: string | null }, v: string): Record<string, string> => ({
  ...(isPrimary(env) ? {} : { store: storeKey(env) }),
  ...(opts.theme ? { theme: opts.theme } : {}),
  ...(opts.c ? { c: opts.c } : {}),
  v,
})

/** A full card URL for `env`'s store, minted `now` (good through the end of the day `IMAGE_TTL_DAYS` on), and when it
 *  expires (epoch s). */
export async function deployCardUrl(key: CryptoKey, env: DeployEnv, origin: string, now: number, opts: { theme?: 'light'; c?: string | null }): Promise<{ url: string; v: string; day: number }> {
  const v = await deployVersion(await deployData(env, origin, now))
  const day = expDay(now, IMAGE_TTL_DAYS)
  return { url: origin + await imagePath(key, DEPLOY_KIND, deployParams(env, opts, v), 'full', day), v, day }
}
