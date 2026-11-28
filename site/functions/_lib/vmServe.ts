/** VM mode beside today's indexes (specs/vm-pilot.md "Mechanisms" step 4): each session is pinned to `idx` (the
 * index reader) or `vm` (`vmd`, through `vmAdapter.ts`), and every map answer says which backend gave it.
 *
 * - **Config** (`Env`): `VMD_URL` + `VM_TENANT` + the secret `VM_KEY` turn it on; any unset, it is off (no `be` timing
 *   entry, so no badge; a `?be=vm` is ignored with a note). `VM_SHARE` (0–1): the share of new browser sessions pinned
 *   `vm` (unset: none; the default pin is `idx`).
 * - **The pin** (`pinOf`): `?be=vm|idx` on the request, else the `disky_be` cookie, else `idx`. The middleware writes
 *   the cookie (`pinCookie`): a `?be=` that differs from it re-pins; a page view with neither gets one, `vm` with
 *   probability `VM_SHARE`. The page carries a `vm` pin in its own URL and on its `/api/` requests (`src/backend.ts`).
 * - **What answered** (`stampBe`): `Server-Timing` `be;desc="vm"` / `"idx"` / `"idx-fallback"` (a `vm` session answered
 *   by `idx`), and `x-disky-be-note` saying why, when there is something to say. A union or group root says the
 *   composite of its members' (`beOf`).
 * - **The `vmd` client** (`vmReady`, `vmView`): signed both ways (§4.3 of specs/vm-mode.md); `/v1/status` cached per
 *   isolate (`VMD.statusTtlMs`, a failure `VMD.downTtlMs`), so a down box costs one timeout per isolate per window,
 *   not one per request; a `409 pins` (a new pack swapped in) retried once at the pins `vmd` holds.
 * - **Caching** (`vmServe`): a `vm` answer is kept under the index reader's key plus `&be=vm` and the pack's pins
 *   (`vmKey`), so an `idx` session never reads one, and a new pack never serves an old pack's answer. */
import type { Env } from './auth.js'
import { cacheMatch, cacheStore, type serverTiming } from './edgeCache.js'
import { IsoMap } from './shared.js'
import { capDiffRows, type DiffRow } from './view.js'
import { checkResponse, diffBody, signRequest, type VmAnswer, type VmPins, type VmViewOpts, vmRequest, vmThreshold } from './vmAdapter.js'

export type VmEnv = Pick<Env, 'VMD_URL' | 'VM_TENANT' | 'VM_KEY' | 'VM_SHARE'>
export type Backend = 'idx' | 'vm'
export type Answered = 'idx' | 'vm' | 'idx-fallback'

export const BE_PARAM = 'be'
export const BE_COOKIE = 'disky_be'
export const BE_NOTE = 'x-disky-be-note'
/** How long a pin lasts without a visit that re-pins it. */
const PIN_MAX_AGE = 30 * 86400
/** A `vm` answer's own body version (its key's `&vv=`): bumped when its shape changes, leaving idx keys alone. */
export const VM_RESPONSE_V = 1

/** Every knob of the `vmd` client (tests swap `fetch` and shrink the windows). */
export const VMD = {
  fetch: (r: Request): Promise<Response> => fetch(r),
  /** A `hot` status is trusted this long; a `loading` / `error` one, or an unreachable box, `downTtlMs`. */
  statusTtlMs: 30_000,
  downTtlMs: 10_000,
  statusTimeoutMs: 3_000,
  /** A view's fetch is given up after this; `vmd` itself is told `deadlineMs` (it answers `504`, never partial). */
  viewTimeoutMs: 8_000,
  deadlineMs: 6_000,
}

export const vmOn = (env: VmEnv): boolean => !!(env.VMD_URL?.trim() && env.VM_TENANT?.trim() && env.VM_KEY)

const asBackend = (v: string | null | undefined): Backend | null => (v === 'vm' || v === 'idx' ? v : null)

/** A cookie's value from the request's `Cookie` header, else null. */
export function cookieOf(request: Request, name: string): string | null {
  for (const part of (request.headers.get('cookie') ?? '').split(';')) {
    const i = part.indexOf('=')
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim()
  }
  return null
}

export const VM_OFF_NOTE = 'VM mode is off on this deployment (no VMD_URL, VM_TENANT or VM_KEY): be=vm ignored'

/** The request's pin: `?be=`, else the cookie, else `idx`; always `idx` while VM mode is off (and a `?be=vm` noted). */
export function pinOf(request: Request, env: VmEnv): { pin: Backend; note?: string } {
  const asked = asBackend(new URL(request.url).searchParams.get(BE_PARAM))
  if (!vmOn(env)) return asked === 'vm' ? { pin: 'idx', note: VM_OFF_NOTE } : { pin: 'idx' }
  return { pin: asked ?? asBackend(cookieOf(request, BE_COOKIE)) ?? 'idx' }
}

/** `VM_SHARE` as a probability (unset, malformed or out of range: clamped, else 0). */
export function vmShare(env: VmEnv): number {
  const x = Number(env.VM_SHARE ?? '')
  return env.VM_SHARE?.trim() && Number.isFinite(x) ? Math.min(1, Math.max(0, x)) : 0
}

const cookie = (request: Request, value: string, maxAge: number): string =>
  `${BE_COOKIE}=${value}; Path=/; Max-Age=${maxAge}; SameSite=Lax${new URL(request.url).protocol === 'https:' ? '; Secure' : ''}`

/** The `Set-Cookie` the middleware adds to a response, or null: a `?be=` other than the cookie's re-pins (any
 *  response); a page view (`page`) without a pin gets one, `vm` with probability `VM_SHARE`; with VM mode off, a page
 *  view clears a leftover pin. Not `HttpOnly`: the page reads it (`src/backend.ts`), and it grants nothing. */
export function pinCookie(request: Request, env: VmEnv, page: boolean, rand: () => number = Math.random): string | null {
  const had = asBackend(cookieOf(request, BE_COOKIE))
  if (!vmOn(env)) return page && cookieOf(request, BE_COOKIE) !== null ? cookie(request, '', 0) : null
  const asked = asBackend(new URL(request.url).searchParams.get(BE_PARAM))
  if (asked) return asked === had ? null : cookie(request, asked, PIN_MAX_AGE)
  if (had || !page) return null
  return cookie(request, rand() < vmShare(env) ? 'vm' : 'idx', PIN_MAX_AGE)
}

/** What answered one request, decided as it runs: a `vm` session starts as `idx-fallback` (anything that answers
 *  before `vmd` is asked is the index reader's) and becomes `vm` only when `vmd` answered. */
export class BeTrack {
  readonly on: boolean
  readonly pin: Backend
  answered: Answered
  note?: string
  constructor(request: Request, env: VmEnv) {
    const p = pinOf(request, env)
    this.on = vmOn(env)
    this.pin = p.pin
    this.answered = p.pin === 'vm' ? 'idx-fallback' : 'idx'
    this.note = p.note
  }
  /** `vmd` answered; `note` what its answer leaves out. */
  vm(note?: string): void {
    this.answered = 'vm'
    this.note = note
  }
  /** A `vm` session's request the index reader answers, and why. */
  fallback(why: string): void {
    if (this.pin !== 'vm') return
    this.answered = 'idx-fallback'
    this.note = why
  }
  /** Outside VM mode altogether (a store `vmd` holds no tenant for): `idx`, and why, whatever the pin. */
  outside(why: string): void {
    if (this.pin !== 'vm') return
    this.answered = 'idx'
    this.note = why
  }
}

/** A note as a header value: ASCII, one line, at most 300 characters. */
const headerSafe = (s: string): string => s.replace(/[^\x20-\x7e]/g, '?').slice(0, 300)

const BE_RE = /(?:^|,)\s*be;desc="([^"]*)"/

/** Which backend a response says answered it (its `Server-Timing` `be`), and its note. */
export function beOfResponse(res: Response): { be?: Answered; note?: string } {
  const m = BE_RE.exec(res.headers.get('server-timing') ?? '')
  const note = res.headers.get(BE_NOTE) ?? undefined
  return { ...(m ? { be: m[1] as Answered } : {}), ...(note ? { note } : {}) }
}

/** `res` saying what answered it (`t`): the `be` timing entry while VM mode is on, the note whenever there is one.
 *  A copy when its headers are immutable. */
export function stampBe(res: Response, t: { on: boolean; answered: Answered; note?: string }): Response {
  if (!t.on && !t.note) return res
  const set = (r: Response) => {
    if (t.on) {
      // One `be` entry: a member's (copied through a composite) is replaced, never doubled.
      const rest = (r.headers.get('server-timing') ?? '').split(/,\s*/).filter(e => e && !/^be;/.test(e))
      r.headers.set('server-timing', [...rest, `be;desc="${t.answered}"`].join(', '))
    }
    if (t.note) r.headers.set(BE_NOTE, headerSafe(t.note))
    else r.headers.delete(BE_NOTE)
  }
  try {
    set(res)
    return res
  } catch {
    const copy = new Response(res.body, res)
    set(copy)
    return copy
  }
}

/** A composite answer's backend from its parts' (a union's or a group's member reads): any part that fell back makes
 *  it `idx-fallback`; else any `vm` part makes it `vm`; else `idx`. Notes: each distinct one, `; `-joined. */
export function beOf(request: Request, env: VmEnv, reads: readonly Response[]): { on: boolean; answered: Answered; note?: string } {
  const own = new BeTrack(request, env)
  const parts = reads.map(beOfResponse)
  const bes = parts.map(p => p.be)
  const answered: Answered = bes.includes('idx-fallback') ? 'idx-fallback' : bes.includes('vm') ? 'vm' : bes.length ? 'idx' : own.answered
  const notes = [...new Set([own.note, ...parts.map(p => p.note)].filter((n): n is string => !!n))]
  return { on: own.on, answered, ...(notes.length ? { note: notes.join('; ') } : {}) }
}

/** Why a request kind isn't sent to `vmd` yet (null: it is), and whether that is a fallback or outside VM mode (a store
 *  with no tenant). Each names the spec section that says what `vmd`'s answer lacks. */
export function vmWhyNot(o: { primary: boolean; store?: string; query?: unknown; owner?: unknown; lens?: unknown; by?: string; restricted?: boolean; descent?: boolean }): { why: string; outside?: true } | null {
  if (!o.primary) return { why: `store '${o.store}' has no vmd tenant`, outside: true }
  if (o.restricted) return { why: 'path-restricted link: never sent to vmd' }
  if (o.query) return { why: 'name filter (q=): vmd\'s answer has no match lists yet (specs/vm-mode.md 7.2)' }
  if (o.lens || o.owner || o.by) return { why: 'owner scope (lens, o, by): vmd doesn\'t fold the live ledger yet (specs/vm-mode.md 2.1)' }
  if (o.descent) return { why: 'delta map (dm=delta): vmd draws by max(before, after), the index reader\'s descent by change' }
  return null
}

/** What a `vm` subtree's nodes leave out (specs/vm-mode.md §7.2): the badge's note on a `vm` answer. */
export const VM_NODE_NOTE = 'nodes carry no mtime, last read, owners, class breakdown or provenance yet (specs/vm-mode.md 7.2)'

/** The notes the middleware gives answers no route stamped (a request kind `vmd` doesn't serve at all). */
export const KIND_NOTES: Record<string, string> = {
  '/api/subtree': 'answered by the index reader before the backend choice',
  '/api/diff': 'answered by the index reader before the backend choice',
  '/api/series': 'size over time: vmd has no series yet',
  '/api/name-summary': 'search summary: vmd has no name summary yet',
}

/** The middleware's stamp on a map answer no route stamped (`KIND_NOTES`' paths): `idx`, or a `vm` session's fallback
 *  with the kind's note. Other paths pass through. */
export function stampKind(res: Response, request: Request, env: VmEnv): Response {
  const note = KIND_NOTES[new URL(request.url).pathname]
  if (note === undefined || beOfResponse(res).be) return res
  const t = new BeTrack(request, env)
  t.fallback(note)
  return stampBe(res, t)
}

// --- the `vmd` client --------------------------------------------------------------------------------------------

/** `GET /v1/status`'s body for one tenant (specs/vm-mode.md §4.1). */
export interface VmdStatus {
  tenant: string
  state: 'hot' | 'loading' | 'error'
  current?: { pins: VmPins; scans: number; newest?: string | null; classes?: number[] } | null
  error?: string | null
}

type Known = { at: number; ttl: number; status?: VmdStatus; down?: string }
const known = new IsoMap<string, Known>('vmd-status')
const knownKey = (env: VmEnv): string => `${env.VMD_URL}|${env.VM_TENANT}`

/** Tests: forget every cached status. */
export const resetVmd = (): void => known.clear()

const errText = (text: string): string => {
  try {
    const j = JSON.parse(text) as { error?: string; message?: string }
    return [j.error, j.message].filter(Boolean).join(': ') || text.slice(0, 120)
  } catch {
    return text.slice(0, 120)
  }
}

/** One signed call: its status, body, and whether the reply's signature is the tenant key's. */
async function call(env: VmEnv, method: 'GET' | 'POST', path: string, body: string, timeoutMs: number): Promise<{ status: number; text: string; signed: boolean }> {
  const key = env.VM_KEY!
  const tenant = env.VM_TENANT!.trim()
  const h = await signRequest(key, tenant, method, path, body)
  const res = await VMD.fetch(new Request(`${env.VMD_URL!.trim().replace(/\/+$/, '')}${path}`, {
    method,
    headers: { ...h, ...(method === 'POST' ? { 'content-type': 'application/json' } : {}) },
    ...(method === 'POST' ? { body } : {}),
    signal: AbortSignal.timeout(timeoutMs),
  }))
  const text = await res.text()
  return { status: res.status, text, signed: await checkResponse(key, tenant, h['x-disky-ts'], text, res.headers.get('x-disky-sig')) }
}

/** The tenant's status, cached per isolate. */
export async function vmdStatus(env: VmEnv, now: number = Date.now()): Promise<{ status: VmdStatus } | { why: string }> {
  const k = knownKey(env)
  const c = known.get(k)
  if (c && now - c.at < c.ttl) return c.status ? { status: c.status } : { why: c.down! }
  const down = (why: string) => {
    known.set(k, { at: now, ttl: VMD.downTtlMs, down: why })
    return { why }
  }
  try {
    const r = await call(env, 'GET', '/v1/status', '', VMD.statusTimeoutMs)
    if (r.status !== 200) return down(`vmd status ${r.status}: ${errText(r.text)}`)
    if (!r.signed) return down('vmd status: the reply\'s signature isn\'t the tenant key\'s')
    const status = JSON.parse(r.text) as VmdStatus
    known.set(k, { at: now, ttl: status.state === 'hot' ? VMD.statusTtlMs : VMD.downTtlMs, status })
    return { status }
  } catch (e) {
    return down(`vmd unreachable: ${(e as Error).message ?? e}`)
  }
}

/** The pins to ask `vmd` at, when it can answer `scans` now: hot, with a pack holding them. */
export async function vmReady(env: VmEnv, scans: readonly string[]): Promise<{ pins: VmPins } | { why: string }> {
  const s = await vmdStatus(env)
  if ('why' in s) return s
  const { state, current, error } = s.status
  if (state === 'loading' && !current) return { why: 'vmd is loading the tenant\'s pack' }
  if (!current) return { why: `vmd has no pack for the tenant${error ? `: ${error}` : ''}` }
  const newer = current.newest ? scans.filter(x => x > current.newest!) : []
  if (newer.length) return { why: `scan ${newer[0]} is newer than vmd's pack (newest ${current.newest})` }
  return { pins: current.pins }
}

/** One view from `vmd` at `pins`; a `409 pins` (another pack swapped in since the status was read) is asked once more at
 *  the pins `vmd` holds, which then replace the cached status's. */
export async function vmView(env: VmEnv, o: Omit<VmViewOpts, 'pins'>, pins: VmPins): Promise<{ answer: VmAnswer } | { why: string }> {
  let req
  try {
    req = vmRequest({ ...o, pins, deadlineMs: VMD.deadlineMs })
  } catch (e) {
    return { why: (e as Error).message }
  }
  for (let attempt = 0; ; attempt++) {
    let r
    try {
      r = await call(env, 'POST', '/v1/view', JSON.stringify(req), VMD.viewTimeoutMs)
    } catch (e) {
      known.set(knownKey(env), { at: Date.now(), ttl: VMD.downTtlMs, down: `vmd unreachable: ${(e as Error).message ?? e}` })
      return { why: `vmd unreachable: ${(e as Error).message ?? e}` }
    }
    if (r.status === 409 && r.signed && attempt === 0) {
      const have = (JSON.parse(r.text) as { have?: VmPins | null }).have
      if (!have) return { why: 'vmd: the loaded pack\'s pins differ, and it names none' }
      const c = known.get(knownKey(env))
      if (c?.status?.current) known.set(knownKey(env), { ...c, status: { ...c.status, current: { ...c.status.current, pins: have } } })
      req = { ...req, pins: have }
      continue
    }
    if (r.status !== 200) {
      // `loading` is a state the status should say next time: forget it.
      if (r.status === 503) known.delete(knownKey(env))
      return { why: `vmd ${r.status}: ${errText(r.text)}` }
    }
    if (!r.signed) return { why: 'vmd: the reply\'s signature isn\'t the tenant key\'s' }
    return { answer: JSON.parse(r.text) as VmAnswer }
  }
}

/** A `vm` answer's cache key: the index reader's for the same request, plus the backend, the pack's pins and the `vm`
 *  body's version, so neither backend's answer is ever served for the other's. */
export const vmKey = (idxKey: Request, pins: VmPins): Request =>
  new Request(`${idxKey.url}&be=vm&vv=${VM_RESPONSE_V}&vp=${encodeURIComponent(`${pins.gen}/${pins.manifest}`)}`)

/** What a `vm` body says of the answer behind it (beside the route's own fields). */
export const vmMeta = (a: VmAnswer) => ({ tenant: a.tenant, pins: a.pins, ms: Math.round(a.timing.total_ms) })

/** `/api/diff`'s body from a diff's answer: `diffBody`'s rows cut to `top` as the index reader's are (`capDiffRows`:
 *  the expanded skeleton, the largest changes, each expanded row's `(other)`), none for `summary=1`. A `vm` answer reads
 *  no point lookups (`lookups: 0`): `vmd`'s tree is exact on both sides. */
export function vmDiffBody(a: VmAnswer, o: { from: string; to: string; path: string; top: number; summary: boolean }) {
  const d = diffBody(a)
  const { rows, cut }: { rows: DiffRow[]; cut: boolean } = o.summary ? { rows: [], cut: false } : capDiffRows(d.rows, o.top, d)
  return {
    prev: o.from,
    curr: o.to,
    path: o.path,
    rows,
    bytes_before: d.bytes_before,
    bytes_after: d.bytes_after,
    objects_before: d.objects_before,
    objects_after: d.objects_after,
    expansions: rows.filter(r => r.expanded).length,
    truncated: cut,
    threshold: Math.round(vmThreshold(a)),
    tier: 'vm',
    lookups: 0,
    lookups_capped: false,
    vm: vmMeta(a),
  }
}

/** A view answered by `vmd`: its colo / KV entry when the pack's pins have one, else `vmd`'s answer as `body` shapes it,
 *  kept under the pins it came back with. `{ why }`: `vmd` can't answer it now; the route falls back. */
export async function vmServe(
  ctx: { env: Env; waitUntil?: (p: Promise<unknown>) => void },
  st: ReturnType<typeof serverTiming>,
  o: { scans: string[]; view: Omit<VmViewOpts, 'pins'>; idxKey: Request; body: (a: VmAnswer) => unknown },
): Promise<Response | { why: string }> {
  const ready = await st.time('vm-status', vmReady(ctx.env, o.scans))
  if ('why' in ready) return ready
  const key = vmKey(o.idxKey, ready.pins)
  const hit = await st.time('match', cacheMatch(ctx.env, key))
  if (hit) return hit
  st.mark('fixed')
  const v = await st.time('vmd', vmView(ctx.env, o.view, ready.pins))
  if ('why' in v) return v
  st.trace('vmd-pass', v.answer.timing.pass_ms)
  st.trace('vmd-total', v.answer.timing.total_ms)
  return cacheStore(ctx.env, vmKey(o.idxKey, v.answer.pins), JSON.stringify(o.body(v.answer)), { 'server-timing': st.header() }, ctx.waitUntil?.bind(ctx), true)
}
