/**
 * The device API (specs/device-api.md): a Mac running disky enrolls as a capture host, gets a device token (an auth
 * grant scoped `host:<id>`), and uploads its captures through presigned PUTs under `captures/<id>/…` — no bucket key
 * on the Mac. The ingest takes it from the capture's `_SUCCESS.json` as it does m3's own.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { type Ctx, type Env, gateFor, identify, json } from './auth.js'
import { sameOrigin } from './appLink.js'
import { forgetHosts, type HostRow, RESERVED_KEYS } from './hosts.js'
import { presign } from './presign.js'
import { secondaryStores } from './stores.js'

export const ID_RE = /^[a-z0-9][a-z0-9-]{0,31}$/
export const UPLOAD_TTL_S = 900
export const MAX_KEYS = 100
const ROOT_RE = /^[A-Za-z0-9._-]{1,64}$/
const KEY_RE = /^[A-Za-z0-9._-]{1,128}$/
const STAMP_RE = /^(\d{4})-(\d\d)-(\d\d)T(\d\d)-(\d\d)-(\d\d)Z$/
const HOME_RE = /^[A-Za-z0-9._ -]+(\/[A-Za-z0-9._ -]+)*$/
const STAMP_SKEW_MS = 86_400_000

export const deviceScope = (id: string): string => `host:${id}`
const iso = (ms: number): string => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
const noStore = { 'cache-control': 'no-store' }

export interface DeviceRow extends HostRow {
  created_at: string
  created_by: string | null
  grant_id: string | null
  os: string | null
  version: string | null
  seen_at: string | null
}

const row = (db: D1Database, id: string) =>
  db.prepare('SELECT id, store, label, home, root, mark, created_at, created_by, revoked_at, grant_id, os, version, seen_at FROM hosts WHERE id = ?')
    .bind(id).first<DeviceRow>()

const str = (v: unknown, max: number): string | null => (typeof v === 'string' && v.length >= 1 && v.length <= max ? v : null)

/** `POST /api/hosts` — an admin's same-origin session enrolls (or re-enrolls) a host: its device token, once. */
export async function enroll(ctx: Ctx, nowMs = Date.now()): Promise<Response> {
  const { request: req, env } = ctx
  if (!sameOrigin(req)) return json({ error: 'cross-site request refused' }, 403)
  const gate = gateFor(env)
  if (!env.DB || !gate) return json({ error: 'auth backend not configured (DB / SESSION_SECRET)' }, 503)
  const id = await identify(ctx)
  if (!id) return json({ error: 'unauthenticated' }, 401)
  if (id.via !== 'session' || !id.admin || !id.email) return json({ error: 'enrolling a host takes an admin’s signed-in session' }, 403)
  const body = await req.json().catch(() => null) as Record<string, unknown> | null
  const host = typeof body?.host === 'string' ? body.host : ''
  const label = str(body?.label, 64)
  const home = body?.home == null ? null : str(body.home, 256)
  const os = body?.os == null ? null : str(body.os, 64)
  const version = body?.version == null ? null : str(body.version, 32)
  if (!ID_RE.test(host) || RESERVED_KEYS.has(host)) return json({ error: `bad host id '${host}' (want ${ID_RE.source}, not a site route)` }, 400)
  if (!label?.trim()) return json({ error: 'bad label (1–64 chars)' }, 400)
  if (body?.home != null && !(home && HOME_RE.test(home) && !home.split('/').includes('..'))) return json({ error: 'bad home (a relative path, e.g. Users/ryan)' }, 400)
  if ((body?.os != null && !os) || (body?.version != null && !version)) return json({ error: 'bad os / version' }, 400)
  const prior = await row(env.DB, host)
  if (prior?.store === 'primary') return json({ error: `'${host}' is this deployment’s own host` }, 409)
  if (prior && !prior.grant_id) return json({ error: `'${host}' is registered without enrollment (its captures come from the deployment’s own keys)` }, 409)
  if (!prior && secondaryStores(env)[host]) return json({ error: `'${host}' is already a store` }, 409)
  const email = id.email.toLowerCase()
  if (prior?.grant_id) await gate.revoke(prior.grant_id, nowMs)
  const { grant, token } = await gate.mint({
    name: `disky device ${host}`,
    note: 'a Mac enrolled through the disky app: uploads its captures, reads its own status',
    email: null,
    scopes: [deviceScope(host)],
    maxRedeems: null,
    expiresAt: null,
    createdBy: email,
  }, nowMs)
  if (prior) {
    await env.DB.prepare('UPDATE hosts SET label = ?, home = ?, os = ?, version = ?, grant_id = ?, revoked_at = NULL WHERE id = ?')
      .bind(label, home, os, version, grant.id, host).run()
  } else {
    await env.DB.prepare('INSERT INTO hosts (id, store, label, home, os, version, grant_id, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
      .bind(host, host, label, home, os, version, grant.id, iso(nowMs), email).run()
  }
  forgetHosts(env)
  return json({ id: host, store: host, token }, prior ? 200 : 201, noStore)
}

type Caller = { kind: 'device'; row: DeviceRow } | { kind: 'admin'; row: DeviceRow }

/** The caller on `/api/hosts/<id>…`: its own device token (a live grant scoped to it, the row's current one), or —
 *  where `admin` — an admin's session. */
async function caller(ctx: Ctx, hostId: string, admin: boolean, nowMs: number): Promise<Caller | Response> {
  const { env } = ctx
  const gate = gateFor(env)
  if (!env.DB || !gate) return json({ error: 'auth backend not configured (DB / SESSION_SECRET)' }, 503)
  if (!ID_RE.test(hostId)) return json({ error: 'bad host id' }, 400)
  const auth = await gate.authenticate(ctx.request)
  if (!auth) return json({ error: 'unauthenticated' }, 401)
  const r = await row(env.DB, hostId)
  if (auth.kind === 'grant') {
    if (!auth.scopes.includes(deviceScope(hostId))) return json({ error: 'not this host’s token' }, 403)
    // A re-enrollment replaced the token, or the host was disconnected: this one is dead.
    if (!r || r.grant_id !== auth.grant.id || r.revoked_at) return json({ error: 'device token revoked' }, 401)
    await env.DB.prepare('UPDATE hosts SET seen_at = ? WHERE id = ?').bind(iso(nowMs), hostId).run()
    return { kind: 'device', row: r }
  }
  const id = await identify(ctx)
  if (!admin || !id?.admin) return json({ error: 'forbidden' }, 403)
  if (!r) return json({ error: `no host '${hostId}'` }, 404)
  return { kind: 'admin', row: r }
}

/** `GET /api/hosts/<id>` — the host's row and its store's newest ingest. */
export async function hostStatus(ctx: Ctx, hostId: string, nowMs = Date.now()): Promise<Response> {
  const c = await caller(ctx, hostId, true, nowMs)
  if (c instanceof Response) return c
  const r = c.kind === 'device' ? { ...c.row, seen_at: iso(nowMs) } : c.row
  const run = await ctx.env.DB!.prepare(
    "SELECT scan, status, finished_ts, updated_ts, error FROM scan_runs WHERE store = ? AND kind = 'ingest' ORDER BY started_ts DESC, updated_ts DESC LIMIT 1",
  ).bind(r.store).first<{ scan: string; status: string; finished_ts: number | null; updated_ts: number; error: string | null }>()
  return json({
    id: r.id, store: r.store, label: r.label, home: r.home, os: r.os, version: r.version,
    created_at: r.created_at, seen_at: r.seen_at, revoked: !!r.revoked_at,
    last_ingest: run ? { scan: run.scan, status: run.status, at: iso((run.finished_ts ?? run.updated_ts) * 1000), error: run.error } : null,
  }, 200, noStore)
}

/** `DELETE /api/hosts/<id>` — disconnect: the token dies, the store's history stays, later captures are refused. */
export async function disconnect(ctx: Ctx, hostId: string, nowMs = Date.now()): Promise<Response> {
  const c = await caller(ctx, hostId, true, nowMs)
  if (c instanceof Response) return c
  if (c.kind === 'admin' && !c.row.grant_id) return json({ error: `'${hostId}' was never enrolled` }, 409)
  if (c.row.grant_id) await gateFor(ctx.env)!.revoke(c.row.grant_id, nowMs)
  await ctx.env.DB!.prepare('UPDATE hosts SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?').bind(iso(nowMs), hostId).run()
  forgetHosts(ctx.env)
  return json({ revoked: true }, 200, noStore)
}

/** The capture stamp's instant, or null when it isn't one. */
function stampMs(stamp: string): number | null {
  const m = stamp.match(STAMP_RE)
  if (!m) return null
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])
  return Number.isNaN(t) || iso(t) !== `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z` ? null : t
}

/** `POST /api/hosts/<id>/uploads` — presigned PUTs for one batch of a capture's keys (its device token only). */
export async function uploads(ctx: Ctx, hostId: string, nowMs = Date.now()): Promise<Response> {
  const c = await caller(ctx, hostId, false, nowMs)
  if (c instanceof Response) return c
  const env = ctx.env as Env
  const body = await ctx.request.json().catch(() => null) as { root?: unknown; stamp?: unknown; keys?: unknown } | null
  const root = typeof body?.root === 'string' ? body.root : ''
  const stamp = typeof body?.stamp === 'string' ? body.stamp : ''
  const keys = body?.keys
  if (!ROOT_RE.test(root)) return json({ error: `bad root (want ${ROOT_RE.source})` }, 400)
  const t = stampMs(stamp)
  if (t === null) return json({ error: 'bad stamp (want %Y-%m-%dT%H-%M-%SZ)' }, 400)
  if (Math.abs(t - nowMs) > STAMP_SKEW_MS) return json({ error: 'stamp more than a day from now' }, 400)
  if (!Array.isArray(keys) || !keys.length || keys.length > MAX_KEYS) return json({ error: `want 1–${MAX_KEYS} keys` }, 400)
  const bad = keys.find(k => typeof k !== 'string' || !KEY_RE.test(k) || k === '.' || k === '..')
  if (bad !== undefined) return json({ error: `bad key ${JSON.stringify(bad)} (want ${KEY_RE.source})` }, 400)
  const { STORE_ENDPOINT: endpoint, STORE_BUCKET: bucket, STORE_ACCESS_KEY_ID: accessKeyId, STORE_SECRET_ACCESS_KEY: secretAccessKey } = env
  if (!endpoint || !bucket || !accessKeyId || !secretAccessKey) return json({ error: 'upload store not configured' }, 503)
  const base = `${endpoint.replace(/\/$/, '')}/${bucket}/captures/${hostId}/${root}/${stamp}`
  const now = new Date(nowMs)
  const urls: Record<string, string> = {}
  for (const k of new Set(keys as string[])) {
    urls[k] = await presign({ method: 'PUT', url: `${base}/${k}`, accessKeyId, secretAccessKey, region: env.STORE_REGION ?? 'auto', expires: UPLOAD_TTL_S, now })
  }
  return json({ urls, expires: iso(nowMs + UPLOAD_TTL_S * 1000) }, 200, noStore)
}
