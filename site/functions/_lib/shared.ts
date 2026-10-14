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
const typical = new Map<string, number>()
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
 *  the memo still holds it). A dead request's work is evicted at once: it's never joined. */
export function track(p: Promise<unknown>, j: Join, evict: () => void): void {
  const e = new Entry(evict, j.name, j.req)
  entries.set(p, e)
  if (j.req?.dead) return e.drop()
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
    e.drop()
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
export async function shared<T>(map: Map<string, Promise<T>>, key: string, make: () => Promise<T>, waitMs: number, j?: Join): Promise<T> {
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
  return cur && cur !== held ? shared(map, key, make, waitMs, j) : own(start())
}

/** A computation shared only while in flight: a caller finding `key` pending joins it (`join`), and once it's
 *  evicted runs `make` itself — in its own context, unbounded (it's that request's own I/O). The entry leaves the map
 *  once it settles, either way. Unlike `shared`, which memoizes: for work whose result is cached apart. */
export async function inFlight<T>(map: Map<string, Promise<T>>, key: string, make: () => Promise<T>, j: Join): Promise<T> {
  const held = map.get(key)
  if (held) {
    const got = await join(held, j)
    if (got !== STALLED) return got
    const cur = map.get(key)
    if (cur && cur !== held) return inFlight(map, key, make, j)
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
