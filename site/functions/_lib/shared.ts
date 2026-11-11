import { readerCache, type ReaderCache } from './readerRuntime.js'
/** Per-isolate memos of promises shared across requests — and what a request does about another request's work.
 *
 * Workers freeze a cancelled request's pending promises (a viewer navigating away mid-compute): they never settle
 * and never reject, so a plain `Map<key, Promise>` can hold an entry nothing will ever resolve, and every later
 * request that awaits it is cut off by the runtime as "hung" (gcs, 2026-10-10). Worse, a dead request leaves an
 * entry in every memo it had reached (state, handle, footer, footer doc, footer group, row group), and a later read
 * that waited a fixed bound on each in turn waited out each of them, chained (prod, 2026-10-10: ~60 s on a cold
 * date). So work a request shares is *owned* (`Req`, carried on the env under `REQ`) and *tracked* (`track`):
 *
 * - **Cancel evicts.** A request's client disconnect (its `request.signal`; Cloudflare's `enable_request_signal`
 *   compatibility flag) evicts every entry it owns at once, and wakes their joiners.
 * - **One dead entry convicts its owner.** A joiner (`join`) waits on another request's entry only until that entry
 *   is `joinBound` old (a few times what its kind takes to settle here); past that its owner is taken for dead and
 *   every entry it owns is evicted — so the chain is one wait, and no later request ever joins them again.
 * - **One budget per request.** The wall time a request spends blocked on others' entries, across every memo (parallel
 *   joins counted once), is at most `JOIN.budget`.
 *
 * An eviction only costs a duplicate read: the owner's own awaiters still hold its promise, the evicting request
 * computes afresh in its own context and puts its entry in place, and a promise once evicted is never put back (every
 * removal and settle compares identity first). Holders of a real stake (a lock, a slot) must never be shared this
 * way at all — there is no release to wait for. */

/** A `Server-Timing` sink (`serverTiming().trace`). */
export type Trace = (name: string, ms: number, desc?: string) => void

/** What `within`, `join` yield for a promise not settled in time. */
export const STALLED: unique symbol = Symbol('stalled')

/** The bounds on joining another request's in-flight work (a knob object: tests shrink it). An entry of kind `K` is
 *  waited on until it is `k ×` the typical time `K` takes to settle in this isolate (an EWMA of the settled ones;
 *  `cold` before any has), clamped to `[min, max]`. */
export const JOIN = { k: 2, min: 1_000, max: 8_000, cold: 3_000, budget: 8_000 }

// --- provisional scopes (`ahead`): what a request starts beside its gate lands only once the gate passes ----------
//
// The lookups a request starts beside its gate (`ahead`) run in a `Scope` (the async context's, `AsyncLocalStorage`):
// every write they would make to isolate state — a memo's entry (`IsoMap`, `Memo`, the LRUs), a colo cache put
// (`colo`), a KV put or a queued D1 write (`isolateWrite`) — is logged instead, and seen by that request alone (each
// `IsoMap`'s per-scope overlay). The gate passing commits the log in order (`Scope.commit`): the isolate then holds
// exactly what the same lookups started after the gate would have left. Anything else (refused, thrown, returned
// before the gate) discards it: the isolate is left as it was. A scope reads committed state and joins other
// requests' in-flight work (all of it authorized: a provisional entry, pending or settled, is never visible to another
// request — `IsoMap`'s overlay, and `pendingIn`'s for a plain `Map` handed to `shared` / `inFlight`), but never evicts
// or convicts it. So a refused request's kill (`done()`) drops only its own entries: no other request was joining them.

/** A request's provisional writes (see above). */
export class Scope {
  state: 'provisional' | 'committed' | 'discarded' = 'provisional'
  private log: (() => void)[] = []
  private local = new Map<object, Map<unknown, unknown>>()
  /** `owner`'s overlay in this scope (its writes, visible to this request only). */
  overlay<K, V>(owner: object): Map<K, V> {
    let m = this.local.get(owner)
    if (!m) this.local.set(owner, (m = new Map()))
    return m as Map<K, V>
  }
  /** `owner`'s overlay, if this scope wrote to it. */
  peek<K, V>(owner: object): Map<K, V> | undefined { return this.local.get(owner) as Map<K, V> | undefined }
  /** Run `write` at the commit (dropped if the scope is discarded). */
  defer(write: () => void): void {
    if (this.state === 'provisional') this.log.push(write)
  }
  /** The gate passed: every logged write lands, in order; later writes are direct. */
  commit(): void {
    if (this.state !== 'provisional') return
    this.state = 'committed'
    const log = this.log
    this.log = []
    this.local.clear()
    for (const w of log) w()
  }
  /** The gate didn't pass (or was never reached): nothing logged lands, nor anything written later. The overlay stays
   *  the scope's own: its lookups, running on to their end, still read back what they wrote (memos, colo puts, their
   *  own pending entries), so they read nothing twice whenever the refusal lands (`AHEAD_COST` is timing-free), and
   *  it leaves with the scope. */
  discard(): void {
    if (this.state !== 'provisional') return
    this.state = 'discarded'
    this.log = []
  }
}

interface ScopeAls {
  run<R>(store: Scope, f: () => R): R
  getStore(): Scope | undefined
}
// Computed so the Functions bundler leaves it to the runtime (as `usage.ts`' meter store).
const SCOPE_ALS_MODULE = ['node', 'async_hooks'].join(':')
let scopeAlsP: Promise<ScopeAls | null> | null = null
let scopeAls: ScopeAls | null = null
/** Load the scopes' async context (once; null without one: `ahead` then starts nothing before the gate). */
export const loadScopes = (): Promise<ScopeAls | null> =>
  (scopeAlsP ??= import(/* @vite-ignore */ SCOPE_ALS_MODULE)
    .then((m: { AsyncLocalStorage: new () => ScopeAls }) => (scopeAls = new m.AsyncLocalStorage()))
    .catch(() => null))

/** The uncommitted scope this code runs in, if any (a discarded one too: its writes are dropped). */
export const scopeOf = (): Scope | undefined => {
  const s = scopeAls?.getStore()
  return s && s.state !== 'committed' ? s : undefined
}

/** Write to isolate (or colo, KV, D1-queue) state: now, or at the scope's commit. */
export function isolateWrite(write: () => void): void {
  const s = scopeOf()
  if (s) s.defer(write)
  else write()
}
/** `isolateWrite` for an async write (a cache put): awaited now, or started at the commit (not awaited). */
export async function isolatePut(put: () => Promise<unknown>): Promise<void> {
  const s = scopeOf()
  if (!s) {
    await put()
    return
  }
  s.defer(() => { put().catch(() => {}) })
}

/** Isolate state a test can snapshot and clear (`isolateSnapshot`, `clearIsolate`). */
interface Held { name: string; entries(): Iterable<[unknown, unknown]>; clear(): void }
const registry = new Set<WeakRef<Held>>()
/** Hold `h` in the registry, weakly (a dropped handle's memos leave with it): its owner keeps it (the returned value). */
export function registerIsolate(h: Held): Held {
  registry.add(new WeakRef(h))
  return h
}
const refIds = new WeakMap<object, number>()
let nextRef = 0
const refId = (v: unknown): unknown => (v !== null && (typeof v === 'object' || typeof v === 'function') ? `#${refIds.get(v) ?? (refIds.set(v, ++nextRef), nextRef)}` : v)
/** Every registered memo's entries, in order (objects by identity: `#<n>`), keyed by name; empty ones left out. */
export function isolateSnapshot(): Record<string, [unknown, unknown][]> {
  const out: Record<string, [unknown, unknown][]> = {}
  for (const r of registry) {
    const h = r.deref()
    if (!h) { registry.delete(r); continue }
    const es = [...h.entries()].map(([k, v]): [unknown, unknown] => [refId(k), refId(v)])
    if (es.length) (out[h.name] ??= []).push(...es)
  }
  return out
}
/** Tests: empty every registered memo. */
export function clearIsolate(): void {
  for (const r of registry) r.deref()?.clear()
}

const TOMB: unique symbol = Symbol('deleted')
let mapIds = 0
/** A `Map` of isolate state: in a scope (`scopeOf`), its writes go to the scope's overlay (read back by that request)
 *  and land at the commit; elsewhere it is a plain `Map`. */
export class IsoMap<K, V> extends Map<K, V> {
  readonly name: string
  /** Its registry entry (held here: the registry's reference is weak). */
  private readonly held: Held
  constructor(name: string) {
    super()
    this.name = `${name}#${mapIds++}`
    const base = (): Iterable<[unknown, unknown]> => super.entries()
    const clear = () => super.clear()
    this.held = registerIsolate({ name, entries: base, clear })
  }
  private ov(): Map<K, V | typeof TOMB> | undefined { return scopeOf()?.peek(this) }
  override get(k: K): V | undefined {
    const o = this.ov()
    if (o?.has(k)) { const v = o.get(k)!; return v === TOMB ? undefined : v as V }
    return super.get(k)
  }
  override has(k: K): boolean {
    const o = this.ov()
    return o?.has(k) ? o.get(k) !== TOMB : super.has(k)
  }
  override set(k: K, v: V): this {
    const s = scopeOf()
    if (!s) return super.set(k, v)
    const o = s.overlay<K, V | typeof TOMB>(this)
    o.delete(k)
    o.set(k, v)
    s.defer(() => { super.set(k, v) })
    return this
  }
  override delete(k: K): boolean {
    const s = scopeOf()
    if (!s) return super.delete(k)
    const had = this.has(k)
    const o = s.overlay<K, V | typeof TOMB>(this)
    o.delete(k)
    o.set(k, TOMB)
    s.defer(() => { super.delete(k) })
    return had
  }
  override clear(): void {
    const s = scopeOf()
    if (!s) return super.clear()
    for (const k of [...this.keys()]) this.delete(k)
  }
  override *entries(): MapIterator<[K, V]> {
    const o = this.ov()
    if (!o) return yield* super.entries()
    for (const [k, v] of super.entries()) if (!o.has(k)) yield [k, v]
    for (const [k, v] of o) if (v !== TOMB) yield [k, v as V]
  }
  override *keys(): MapIterator<K> { for (const [k] of this.entries()) yield k }
  override *values(): MapIterator<V> { for (const [, v] of this.entries()) yield v }
  override [Symbol.iterator](): MapIterator<[K, V]> { return this.entries() }
  override forEach(f: (v: V, k: K, m: Map<K, V>) => void, self?: unknown): void {
    for (const [k, v] of this.entries()) f.call(self, v, k, this)
  }
  override get size(): number {
    if (!this.ov()) return super.size
    let n = 0
    for (const _ of this.entries()) n++
    return n
  }
}

/** A `Set` of isolate state, as `IsoMap`. */
export class IsoSet<T> {
  private m: IsoMap<T, true>
  constructor(name: string) { this.m = new IsoMap(name) }
  has(v: T): boolean { return this.m.has(v) }
  add(v: T): this { this.m.set(v, true); return this }
  delete(v: T): boolean { return this.m.delete(v) }
  clear(): void { this.m.clear() }
  get size(): number { return this.m.size }
  [Symbol.iterator](): IterableIterator<T> { return this.m.keys() }
}

/** What `shared` / `inFlight` need of their map. */
type Pending<T> = Pick<Map<string, Promise<T>>, 'get' | 'set' | 'delete'>
/** `map` as the request at hand writes its entries (pending and settled promises): an `IsoMap`, or any map outside an
 *  uncommitted scope, as is; a plain `Map` in one through the scope's overlay, as an `IsoMap` would — its entries are
 *  seen by this request alone, so no other request ever joins (or is woken by the kill of) a provisional request's
 *  work, and they land at the commit (a removal only if the map still holds what this request removed); a discarded
 *  scope's stay its own (`Scope.discard`). The scope is the one at hand when the view is taken: its eviction callbacks
 *  (`track`, a kill) may run outside it, and remove nothing from a discarded one. */
function pendingIn<T>(map: Map<string, Promise<T>>): Pending<T> {
  const s = scopeOf()
  if (!s || map instanceof IsoMap) return map
  const over = () => s.overlay<string, Promise<T> | typeof TOMB>(map)
  const view: Pending<T> = {
    get(k) {
      if (s.state === 'committed') return map.get(k)
      const o = s.peek<string, Promise<T> | typeof TOMB>(map)
      if (o?.has(k)) { const v = o.get(k)!; return v === TOMB ? undefined : v }
      return map.get(k)
    },
    set(k, v) {
      if (s.state === 'committed') map.set(k, v)
      else {
        over().set(k, v)
        s.defer(() => { map.set(k, v) })
      }
      return map
    },
    delete(k) {
      if (s.state === 'committed') return map.delete(k)
      const cur = view.get(k)
      if (s.state === 'provisional') {
        over().set(k, TOMB)
        s.defer(() => { if (map.get(k) === cur) map.delete(k) })
      }
      return cur !== undefined
    },
  }
  return view
}

/** The colo cache (the Workers Cache API's default), its puts as isolate writes: in a scope, held for that request
 *  (its own `match`es see them) and put at the commit. */
export function colo(): ReaderCache {
  const c = readerCache() ?? (caches as unknown as { default: Cache }).default
  const s = scopeOf()
  if (!s) return c
  type Put = { buf: ArrayBuffer; init: ResponseInit }
  const keyOf = (r: RequestInfo | URL): string => (typeof r === 'string' ? r : r instanceof URL ? r.href : r.url)
  return {
    async match(r: RequestInfo | URL, o?: CacheQueryOptions) {
      const p = s.peek<string, Put>(c)?.get(keyOf(r))
      return p ? new Response(p.buf.slice(0), p.init) : c.match(r, o)
    },
    async put(r: RequestInfo | URL, res: Response) {
      const buf = await res.arrayBuffer()
      const init = { status: res.status, headers: [...res.headers] }
      s.overlay<string, Put>(c).set(keyOf(r), { buf, init })
      s.defer(() => { c.put(r, new Response(buf, init)).catch(() => {}) })
    },
    async delete(r: RequestInfo | URL, o?: CacheQueryOptions) {
      s.overlay<string, Put>(c).delete(keyOf(r))
      s.defer(() => { c.delete(r, o).catch(() => {}) })
      return true
    },
  }
}

const typical = new IsoMap<string, number>('join-typical')
/** How old an entry of `kind` may get before its owner is taken for dead. */
export const joinBound = (kind: string): number => {
  const t = typical.get(kind)
  return Math.min(JOIN.max, Math.max(JOIN.min, t == null ? JOIN.cold : JOIN.k * t))
}
const settledIn = (kind: string, ms: number): void => {
  const t = typical.get(kind)
  typical.set(kind, t == null ? ms : t + (ms - t) / 8)
}
/** Tests: forget the isolate's settle times. */
export const resetJoinStats = (): void => typical.clear()

/** One request's stake in shared work: the entries it owns, the time it spent waiting on others', and where its
 *  joins are traced. Dead (cancelled, or convicted by a stalled entry): its entries are evicted and it shares no more. */
export class Req {
  dead = false
  trace?: Trace
  private joining = 0
  private since = 0
  private spent = 0
  /** Wall time this request has spent blocked on others' entries: the union of its joins (parallel ones overlap). */
  get waited(): number {
    return this.spent + (this.joining ? Date.now() - this.since : 0)
  }
  /** A join begins / ends. */
  enter(): void {
    if (!this.joining++) this.since = Date.now()
  }
  leave(): void {
    if (!--this.joining) this.spent += Date.now() - this.since
  }
  readonly entries = new Set<Entry>()
  constructor(signal?: AbortSignal | null) {
    if (!signal) return
    if (signal.aborted) this.dead = true
    else signal.addEventListener('abort', () => this.kill(), { once: true })
  }
  kill(): void {
    this.dead = true
    for (const e of [...this.entries]) e.drop()
  }
}
/** Where an env carries its request (a symbol: not a binding or var; object spreads carry it). */
export const REQ: unique symbol = Symbol('request')
export const reqOf = (env: object): Req | undefined => (env as { [REQ]?: Req })[REQ]
/** `env` carrying a request (`signal`: its client disconnect); one already carrying one is returned as is. */
export const withReq = <E extends object>(env: E, signal?: AbortSignal | null): E => (reqOf(env) ? env : { ...env, [REQ]: new Req(signal) })

/** `env`'s request's join of kind `name` (what a memo passes `shared` / `inFlight`: `env`'s request owns what it
 *  starts and joins what another holds). */
export const joinOf = (env: object, name: string): Join => ({ name, req: reqOf(env) })

/** Trace `env`'s request's joins to `trace` (`join-<memo>` ms, `join-evict` per conviction). */
export const traceJoins = (env: object, trace: Trace): void => {
  const r = reqOf(env)
  if (r) r.trace = trace
}

class Entry {
  readonly at = Date.now()
  done = false
  gone = false
  private wake!: () => void
  readonly dropped = new Promise<typeof STALLED>(r => { this.wake = () => r(STALLED) })
  constructor(private readonly evict: () => void, readonly kind: string, readonly owner: Req | undefined) {}
  drop(): void {
    if (this.gone) return
    this.gone = true
    this.evict()
    this.owner?.entries.delete(this)
    this.wake()
  }
}
const entries = new WeakMap<Promise<unknown>, Entry>()

/** A memo's kind (`Server-Timing`'s `join-<name>`, `joinBound`'s stats) and the request at hand. */
export interface Join { name: string; req?: Req }

/** Track `p`, which a memo now holds, as `j.req`'s work of kind `j.name`; `evict` removes it from the memo (only if
 *  the memo still holds it). A dead request's work is evicted at once: it's never joined — unless a scope's overlay
 *  holds it (a refused `ahead`'s lookups running on), which no other request sees: there it is read back, not read
 *  again. */
export function track(p: Promise<unknown>, j: Join, evict: () => void): void {
  const e = new Entry(evict, j.name, j.req)
  entries.set(p, e)
  if (j.req?.dead && !scopeOf()) return e.drop()
  j.req?.entries.add(e)
  p.then(() => settledIn(e.kind, Date.now() - e.at), () => {}).finally(() => {
    e.done = true
    e.owner?.entries.delete(e)
  })
}

const sleep = (ms: number): { p: Promise<typeof STALLED>; clear: () => void } => {
  let timer: ReturnType<typeof setTimeout> | null = null
  return { p: new Promise(r => { timer = setTimeout(() => r(STALLED), ms) }), clear: () => clearTimeout(timer!) }
}

/** `p`'s value, or `STALLED` if it hasn't settled within `ms` (nothing is cancelled; a rejection propagates). The
 *  pending timer is progress to the Workers runtime, so a wait on another request's frozen promise ends here. */
export async function within<T>(p: Promise<T>, ms: number): Promise<T | typeof STALLED> {
  const t = sleep(ms)
  try {
    return await Promise.race([p, t.p])
  } finally {
    t.clear()
  }
}

/** `p` (an entry some memo holds) as `j.req` joins it: its value, or `STALLED` once it was evicted — by its owner's
 *  cancel, its owner's conviction, or here: an entry that outlived `joinBound` convicts its owner (every entry it owns
 *  is evicted), one that only outlived this request's budget is evicted alone. The request's own entries are its own
 *  I/O, awaited as is; an untracked promise is waited on up to its kind's bound. Each wait is traced `join-<name>`. */
export async function join<T>(p: Promise<T>, j: Join): Promise<T | typeof STALLED> {
  const e = entries.get(p)
  if (!e) return within(p, joinBound(j.name))
  if (e.done || (e.owner && e.owner === j.req)) return p
  if (e.gone || e.owner?.dead) {
    // A scope evicts nothing (`Scope`): it computes afresh in its overlay.
    if (!scopeOf()) e.drop()
    return STALLED
  }
  const bound = joinBound(e.kind)
  const left = j.req ? Math.max(0, JOIN.budget - j.req.waited) : Infinity
  const t0 = Date.now()
  const toBound = bound - (t0 - e.at)
  // Waiting out the entry's bound convicts its owner; a wait cut short by the budget says nothing about it.
  const convicts = toBound <= left
  const t = sleep(Math.max(0, Math.min(toBound, left)))
  let got: T | typeof STALLED
  j.req?.enter()
  try {
    got = await Promise.race([p, e.dropped, t.p])
  } finally {
    t.clear()
    j.req?.leave()
    j.req?.trace?.(`join-${j.name}`, Date.now() - t0)
  }
  if (got !== STALLED || e.gone) return got
  // A scope convicts no one (its gate hasn't passed): it computes afresh in its overlay.
  if (scopeOf()) return STALLED
  if (convicts) {
    j.req?.trace?.('join-evict', 1, j.name)
    if (e.owner) e.owner.kill()
    else e.drop()
  } else e.drop()
  return STALLED
}

/** A memo of `make()` at `key`. With `j`, the entry is tracked as `j.req`'s and another request's is joined
 *  (`join`); without, a held entry is waited on up to `waitMs` like the caller's own. The caller's own computation is
 *  bounded by `waitMs` (a stall evicts it and fails). */
export async function shared<T>(base: Map<string, Promise<T>>, key: string, make: () => Promise<T>, waitMs: number, j?: Join): Promise<T> {
  const map = pendingIn(base)
  const start = (): Promise<T> => {
    const q = make()
    map.set(key, q)
    const evict = () => { if (map.get(key) === q) map.delete(key) }
    q.catch(evict)
    if (j) track(q, j, evict)
    return q
  }
  const own = async (q: Promise<T>): Promise<T> => {
    const got = await within(q, waitMs)
    if (got !== STALLED) return got
    if (map.get(key) === q) map.delete(key)
    throw new Stall(key, waitMs)
  }
  const held = map.get(key)
  if (!held) return own(start())
  if (!j || (j.req && entries.get(held)?.owner === j.req)) {
    try {
      return await own(held)
    } catch (e) {
      if (!(e instanceof Stall)) throw e
      return own(start())
    }
  }
  const got = await join(held, j)
  if (got !== STALLED) return got
  // Another request may have put its fresh entry in place meanwhile: that one is joined.
  const cur = map.get(key)
  return cur && cur !== held ? shared(base, key, make, waitMs, j) : own(start())
}

/** A computation shared only while in flight: a caller finding `key` pending joins it (`join`), and once it's
 *  evicted runs `make` itself — in its own context, unbounded (it's that request's own I/O). The entry leaves the map
 *  once it settles, either way. Unlike `shared`, which memoizes: for work whose result is cached apart. */
export async function inFlight<T>(base: Map<string, Promise<T>>, key: string, make: () => Promise<T>, j: Join): Promise<T> {
  const map = pendingIn(base)
  const held = map.get(key)
  if (held) {
    const got = await join(held, j)
    if (got !== STALLED) return got
    const cur = map.get(key)
    if (cur && cur !== held) return inFlight(base, key, make, j)
  }
  const p = make()
  map.set(key, p)
  const evict = () => { if (map.get(key) === p) map.delete(key) }
  track(p, j, evict)
  try {
    return await p
  } finally {
    evict()
  }
}

/** The static name sources' answers held per isolate (`SuffixHits`, `AnchoredSource`, `DrillSource`), under one bound
 *  between them in estimated decoded bytes, the least recently used dropped first. Each source had held up to 400K
 *  hits of its own, ~340 B apiece decoded (a ~90-character path, two bigints, two epoch-ms doubles): the three could
 *  hold several times a Worker's 128 MB, and gcs's fleet-root `q=tokenizer` view (171K roots, ~56 MB of them, and
 *  their forest) ran out of memory (1102) on any isolate already holding other answers. */
export class HeldAnswers {
  private held = new Map<string, { bytes: number; drop: () => void }>()
  private total = 0
  constructor(readonly cap: number) {}

  /** `key` used now: held as the most recently used, at `bytes()` (estimated once); the least recently used are
   *  dropped (their `drop`) while the total is over the cap, and an answer over it alone is not held. */
  keep(key: string, bytes: () => number, drop: () => void): void {
    const e = this.held.get(key)
    if (e) { this.held.delete(key); this.held.set(key, e); return }
    const n = bytes()
    if (n > this.cap) { drop(); return }
    this.held.set(key, { bytes: n, drop })
    this.total += n
    for (const [k, x] of this.held) {
      if (this.total <= this.cap) break
      this.held.delete(k); this.total -= x.bytes; x.drop()
    }
  }

  /** `key` dropped by its owner. */
  forget(key: string): void {
    const e = this.held.get(key)
    if (e) { this.held.delete(key); this.total -= e.bytes }
  }

  /** The estimated bytes held. */
  get bytes(): number { return this.total }
}

/** What the static name sources hold between them, estimated (`hitsBytes`, `cellsBytes`): a quarter of an isolate. */
export const HELD_BYTES = 32 << 20
export const heldAnswers = new HeldAnswers(HELD_BYTES)
let heldIds = 0
/** A held-answer key prefix unique to one source instance (`HeldAnswers` keys span every source). */
export const heldId = (name: string): string => `${name}#${heldIds++}\0`

/** A decoded hit's estimated bytes beyond its path (V8: the object, two bigints, two doubles). */
export const HIT_BYTES = 256
/** Decoded hits' estimated bytes. */
export const hitsBytes = (hits: readonly { path: string }[]): number => hits.reduce((n, h) => n + HIT_BYTES + h.path.length, 0)
/** A rollup's cells' estimated bytes: ~160 apiece beyond the child's name. */
export const cellsBytes = (cells: readonly { child: string }[]): number => cells.reduce((n, c) => n + 160 + c.child.length, 0)

/** An isolate's memo of settled values whose pending computations are shared across requests only as owned, tracked
 *  entries (`inFlight`): a caller finding `key` settled takes the value; finding it pending in another request, joins
 *  it (`join`: bounded, and once it is evicted — its owner cancelled or convicted — computes it in its own context). A
 *  rejection is never held. `max`: settled values kept, the least recently used dropped past it. For a class's
 *  isolate-lived caches, each call passing the request at hand (`reqOf(env)`). */
export class Memo<T> {
  private vals: IsoMap<string, T>
  private pending: IsoMap<string, Promise<T>>
  constructor(readonly name: string, readonly max = Infinity) {
    this.vals = new IsoMap(`memo:${name}`)
    this.pending = new IsoMap(`memo:${name}:pending`)
  }

  /** Whether `key` is settled here. */
  has(key: string): boolean { return this.vals.has(key) }
  /** Whether `key` is settled or pending here. */
  held(key: string): boolean { return this.vals.has(key) || this.pending.has(key) }
  /** `key`'s settled value (now the most recently used), if any. */
  peek(key: string): T | undefined {
    if (!this.vals.has(key)) return undefined
    const v = this.vals.get(key)!
    this.vals.delete(key)
    this.vals.set(key, v)
    return v
  }
  set(key: string, v: T): void {
    this.vals.delete(key)
    this.vals.set(key, v)
    while (this.vals.size > this.max) this.vals.delete(this.vals.keys().next().value!)
  }
  /** Forget `key`'s settled value (a computation pending for it still settles here). */
  delete(key: string): void { this.vals.delete(key) }
  /** The settled keys, least recently used first. */
  keys(): IterableIterator<string> { return this.vals.keys() }
  /** The settled entries, least recently used first (none touched). */
  entries(): IterableIterator<[string, T]> { return this.vals.entries() }
  /** Forget every settled value, and stop sharing the pending computations (they still settle for their owners, and
   *  here unless `keep` says otherwise). */
  clear(): void {
    this.vals.clear()
    this.pending.clear()
  }

  /** `key`'s value: settled here, else `make`'s — this request's own, or joined from the one computing it. `keep`: whether
   *  a settled value is held (default: every one). */
  get(key: string, make: () => Promise<T>, req?: Req, keep?: (v: T) => boolean): Promise<T> {
    if (this.vals.has(key)) return Promise.resolve(this.peek(key)!)
    return inFlight(this.pending, key, async () => {
      // Settled by another request while this one waited on an entry that was then evicted.
      if (this.vals.has(key)) return this.peek(key)!
      const v = await make()
      if (!keep || keep(v)) this.set(key, v)
      return v
    }, { name: this.name, req })
  }
}

/** A request's lookups that don't depend on who asks, started beside its gate (`requireViewer`) instead of after it:
 *  the fixed phase's steps overlapped. `later(f)` is `f`'s promise as a getter — started at once when `eager` (a
 *  request that may pass the gate: `presentsCredentials`) and the scopes' async context is loaded (`loadScopes`), else
 *  on its first call, so a request that can't pass costs nothing past the gate. A started `f` runs in the request's
 *  `Scope`: what it would write to isolate state (memos, the colo cache, KV, queued D1 writes) is held for this request
 *  alone until `commit()` — called once the gate passes — lands it; every return path ends in `done()`, which
 *  discards an uncommitted scope (a refused gate, a throw, a return before the gate: the isolate is left exactly as it
 *  was) and evicts what is still pending (`Req.kill`: never left for another request to join, frozen) — a failed gate's
 *  lookups, or those its answer didn't need. Nothing is used before the gate passes: the request awaits it first. A
 *  rejection is the awaiting caller's alone (an unawaited one is swallowed). */
export interface Ahead {
  later<T>(f: () => Promise<T>): () => Promise<T>
  /** The gate passed: what the lookups wrote lands (and what they write from now on, directly). */
  commit(): void
  /** Discard an uncommitted scope; `kill` (default): evict what is still pending. */
  done(kill?: boolean): void
}
export function ahead(env: object, eager: boolean): Ahead {
  let pending = 0
  const als = eager ? scopeAls : null
  const scope = als ? new Scope() : null
  return {
    later<T>(f: () => Promise<T>): () => Promise<T> {
      let p: Promise<T> | undefined
      const get = (): Promise<T> => {
        if (!p) {
          pending++
          p = als && scope?.state === 'provisional' ? als.run(scope, f) : f()
          p.then(() => { pending-- }, () => { pending-- })
        }
        return p
      }
      if (scope) get()
      return get
    },
    commit(): void {
      scope?.commit()
    },
    done(kill = true): void {
      scope?.discard()
      if (kill && pending) reqOf(env)?.kill()
    },
  }
}

class Stall extends Error {
  constructor(key: string, ms: number) {
    super(`shared ${key}: no result after ${ms} ms`)
  }
}

/** Where this deployment's snapshots live in the data bucket: `snapshots/`
 * for the default (GCS) store, `snapshots/<SNAPSHOTS_SUBDIR>/` for a store
 * published under a named subdir (cw-s3: `cw`). Every Function that lists or
 * reads snapshot dirs goes through this, so a deployment can't list another
 * store's scans (the 2026-09-16 preview charted the GCS fleet on cw). */
export const snapshotsPrefix = (env: { SNAPSHOTS_SUBDIR?: string }): string => {
  const sub = (env.SNAPSHOTS_SUBDIR ?? '').replace(/^\/+|\/+$/g, '')
  return sub ? `snapshots/${sub}/` : 'snapshots/'
}
