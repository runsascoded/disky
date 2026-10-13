/** The static name index's runs (specs/static-append.md): a base generation plus one small run per newer scan
 *  (two scans of one day are two runs), each its own little generation under `deltas/<first>[_<last>]/`
 *  (`shards.json`, `sx/`, `catalog/`; scan ids), listed by the newest immutable `manifests/<scan id>.json` (scan
 *  ids sort in time order). Readers query every tier and merge:
 *
 *  - Suffix rows: a run holds the versions opened in it and **close records** (a version from an older tier,
 *    its rows with their final `vt`). Rows equal on `(s, path, usr, vf)` are one version's; the smallest `vt`
 *    wins. A first hit is decided per row by `s` and `path` alone, so combining the tiers' first hits by `(path,
 *    usr, vf)` with the smallest `vt` equals combining their rows; liveness (`vf ≤ D < vt`) is applied after.
 *  - Catalog: a member iff some tier holds its header; the newest tier's header (`rows`, and `n` = its cells
 *    across every tier) and the union of the tiers' cells.
 *
 *  Without a manifest (or a `list`-less `Blobs`) the tiers are the base alone: today's reader. Answers for a
 *  scan already indexed never change when a run lands (a later close sets `vt` after it), so only the hit
 *  lists, which span every scan, are versioned by the manifest's newest scan.
 *
 *  A broken tier (a publish caught mid-write: a run listed before its files are all there, or a corrupt one) is
 *  never a failed request: the stack is cut at it (`Tiers`), the scans before it answer exactly as before, and
 *  its scans and every later one's are `scan-not-indexed` until it loads. */
import { type CatalogIo, type CatalogMeta, type Member, StaticCatalog } from './staticCatalog.js'
import { type Blobs, cmp, FirstHits, type Io, type IndexCache, type GroupIndex, StaticNames } from './staticNames.js'
import { type HexRule, ruleOf, sameRule } from './hexRuns.js'
import { newestManifest } from './manifests.js'

export interface RunInfo { key: string; first: string; last: string; level: number; scans: string[] }
export interface Manifest { gen: string; date: string; base_scans?: number; scans: string[]; runs: RunInfo[] }

/** `Blobs` over a subdirectory of another's keys. */
export function subBlobs(b: Blobs, dir: string): Blobs {
  const k = (key: string) => `${dir}/${key}`
  return {
    range: (key, offset, length) => b.range(k(key), offset, length),
    suffix: (key, n) => b.suffix(k(key), n),
    json: key => b.json(k(key)),
    ...(b.list ? { list: (prefix: string) => b.list!(k(prefix)).then(keys => keys.map(x => x.slice(dir.length + 1))) } : {}),
  }
}

/** The readers of one tier (the base: `dir` null). */
export interface Tier { dir: string | null; names: StaticNames; catalog: StaticCatalog }
/** A tier whose files failed to load (`dir` '' the base): the error (naming the key), and when. */
export interface Broken { dir: string; error: string; at: number }
/** The tiers a reader may use: the base and the runs up to the first broken one (tiers are cumulative, so a scan
 *  at or after a broken tier is never answered from the ones before it), the scans they cover, and a version
 *  naming that stack. `broken`: the tier the stack was cut at. */
export interface TierState { version: string; scans: string[]; tiers: Tier[]; manifest: Manifest | null; broken?: Broken; dirOnly?: string[]; hexRuns: HexRule | null }

/** The base's scans whose source was a v1 index (`scans.json` `version: 1`): it lists directories only, so a
 *  name match on one of its files is invisible there — the index holds that scan, but not its objects. */
export const dirOnlyScans = (doc: { scans: { id: string; version?: number }[] }): string[] =>
  doc.scans.filter(s => s.version === 1).map(s => s.id).sort()
/** Make a tier's readers over its `Blobs` (index caches keyed by `dir`, so tiers never share an entry). */
export type MakeTier = (blobs: Blobs, dir: string | null) => Omit<Tier, 'dir'>

const message = (e: unknown): string => e instanceof Error ? e.message : String(e)

/** The newest manifest's tiers, re-listed at most every `ttlMs` (tier readers, and their isolate caches, are
 *  kept across refreshes).
 *
 *  A tier is usable iff its `catalog/meta.json` loads (checked when the state is built; any other file is
 *  checked by the reads themselves) and no read of its files has failed in the last `brokenTtlMs` (`broke`).
 *  The state is cut at the first tier that isn't: its scans and every later one's drop out (a caller's
 *  `scan-not-indexed`), the ones before it answer as before. A cut state is re-built after `brokenTtlMs` (not
 *  `ttlMs`), re-checking the broken tier, so a publish caught mid-write heals within that. */
export class Tiers {
  private held = new Map<string, Tier>()
  private broken = new Map<string, Broken>()
  private at = -Infinity
  private cut = false
  private cur?: Promise<TierState>
  constructor(
    readonly blobs: Blobs,
    readonly make: MakeTier,
    readonly ttlMs = 300_000,
    readonly now: () => number = () => Date.now(),
    readonly brokenTtlMs = 30_000,
    readonly log: (msg: string) => void = msg => console.error(msg),
  ) {}

  private tier(dir: string | null): Tier {
    const k = dir ?? ''
    let t = this.held.get(k)
    if (!t) { t = { dir, ...this.make(dir == null ? this.blobs : subBlobs(this.blobs, dir), dir) }; this.held.set(k, t) }
    return t
  }

  /** The base tier's readers (whatever the state). */
  base(): Tier { return this.tier(null) }

  /** Record tier `dir` as broken (logged with the error, which names the key; once per `brokenTtlMs`, however
   *  many concurrent reads found it). */
  private mark(dir: string | null, error: unknown): void {
    const b: Broken = { dir: dir ?? '', error: message(error), at: this.now() }
    const was = this.broken.get(b.dir)
    if (was && b.at - was.at < this.brokenTtlMs) return
    this.broken.set(b.dir, b)
    this.log(`static tiers: ${b.dir || 'the base'} is broken; its scans and every later tier's are not indexed until it loads: ${b.error}`)
  }

  /** A read of tier `dir`'s files failed: the next `state()` is cut at it. */
  broke(dir: string | null, error: unknown): void {
    this.mark(dir, error)
    this.cur = undefined
    this.at = -Infinity
  }

  /** Whether `x` is usable at `t`: not broken within `brokenTtlMs`, and its `catalog/meta.json` loads (held once
   *  loaded, so a healthy tier costs nothing past its first state). */
  private async usable(x: Tier, t: number): Promise<boolean> {
    const k = x.dir ?? ''
    const b = this.broken.get(k)
    if (b && t - b.at < this.brokenTtlMs) return false
    try {
      await x.catalog.info()
      this.broken.delete(k)
      return true
    } catch (e) {
      this.mark(x.dir, e)
      return false
    }
  }

  state(): Promise<TierState> {
    const t = this.now()
    if (this.cur && t - this.at < (this.cut ? Math.min(this.ttlMs, this.brokenTtlMs) : this.ttlMs)) return this.cur
    const prev = this.cur
    this.at = t
    const p = (async (): Promise<TierState> => {
      const manifest = await latestManifest(this.blobs)
      const all = [this.tier(null), ...(manifest?.runs ?? []).map(r => this.tier(r.key))]
      const ok = await Promise.all(all.map(x => this.usable(x, t)))
      // Every tier answers under the base's hex-run rule (its `catalog/meta.json` `hex_runs`): a run recording
      // another is broken (cut there), never mixed in.
      const hexRuns = ok[0] ? ruleOf((await all[0].catalog.info()).hex_runs) : null
      for (let i = 1; i < all.length; i++) {
        if (!ok[i]) continue
        const r = ruleOf((await all[i].catalog.info()).hex_runs)
        if (!sameRule(r, hexRuns)) { this.mark(all[i].dir, new Error(`static tiers: ${all[i].dir}/catalog/meta.json hex_runs ${JSON.stringify(r)} disagrees with the base's ${JSON.stringify(hexRuns)}`)); ok[i] = false }
      }
      const n = ok.indexOf(false)
      this.cut = n >= 0
      if (n === 0) return { version: 'none', scans: [], tiers: [], manifest, broken: this.broken.get(''), hexRuns: null }
      if (!manifest) {
        try {
          const doc = await this.blobs.json<{ scans: { id: string; version?: number }[] }>('scans.json')
          return { version: 'base', scans: doc.scans.map(s => s.id).sort(), tiers: all, manifest: null, dirOnly: dirOnlyScans(doc), hexRuns }
        } catch (e) {
          this.mark(null, e)
          this.cut = true
          return { version: 'none', scans: [], tiers: [], manifest: null, broken: this.broken.get(''), hexRuns: null }
        }
      }
      // The base's dir-only scans (its `scans.json`; a run appends one scan of the current format).
      const dirOnly = await this.blobs.json<{ scans: { id: string; version?: number }[] }>('scans.json').then(dirOnlyScans, () => [])
      if (n < 0) return { version: manifest.date, scans: [...manifest.scans].sort(), tiers: all, manifest, dirOnly, hexRuns }
      // Cut at run `n - 1`: its scans and every later run's drop out; the stack's hit lists are those of the
      // manifest that ended at the run before it (versioned by its newest scan, as that manifest was).
      const runs = manifest.runs.slice(0, n - 1), dropped = new Set(manifest.runs.slice(n - 1).flatMap(r => r.scans))
      return {
        version: runs.length ? runs[runs.length - 1].last : 'base',
        scans: manifest.scans.filter(s => !dropped.has(s)).sort(),
        tiers: all.slice(0, n),
        manifest,
        broken: this.broken.get(manifest.runs[n - 1].key),
        hexRuns,
        dirOnly,
      }
    })()
    this.cur = p
    // A failed refresh keeps serving the last good state (and retries on the next call).
    p.catch(() => { if (this.cur === p) { this.cur = prev; this.at = -Infinity } })
    return p
  }

  /** `f` over every usable tier of the current state. A tier whose read fails is `broke`n and `f` re-run over the
   *  re-cut stack (the lowest failing tier first), so the result is always a whole stack's: `state` is the one
   *  it was read over (its `scans` the ones the result is exact on). */
  async each<T>(f: (t: Tier, state: TierState) => Promise<T>): Promise<{ state: TierState; got: T[] }> {
    for (let tries = 0; ; tries++) {
      const state = await this.state()
      const got = await Promise.allSettled(state.tiers.map(t => f(t, state)))
      const bad = got.findIndex(g => g.status === 'rejected')
      if (bad < 0) return { state, got: got.map(g => (g as PromiseFulfilledResult<T>).value) }
      const reason = (got[bad] as PromiseRejectedResult).reason
      if (tries > state.tiers.length) throw reason
      this.broke(state.tiers[bad].dir, reason)
    }
  }
}

/** The newest manifest (`manifests.ts`: a scan's `<D>.json` or a merge's revision `<D>.mNNN.json`), or null (none, or
 *  no `list`). */
export async function latestManifest(blobs: Blobs): Promise<Manifest | null> {
  if (!blobs.list) return null
  const name = newestManifest((await blobs.list('manifests/')).filter(k => k.startsWith('manifests/')).map(k => k.slice('manifests/'.length)))
  if (name === null) return null
  const m = await blobs.json<Manifest>(`manifests/${name}`)
  if (!Array.isArray(m.scans) || !Array.isArray(m.runs) || typeof m.date !== 'string') throw new Error(`static runs: bad manifests/${name}`)
  return m
}

/** First-hit folds of one literal from several tiers, combined: per `(path, usr, vf)` the smallest `vt`. */
export function combineFolds(key: string, folds: FirstHits[], rule: HexRule | null = null): FirstHits {
  const out = new FirstHits(key, rule)
  const at = new Map<string, number>()
  for (const f of folds) {
    out.rows_read += f.rows_read
    out.rows_matching += f.rows_matching
    for (const h of f.hits) {
      const k = `${h.path}\0${h.usr}\0${h.vf}`
      const i = at.get(k)
      if (i == null) { at.set(k, out.hits.length); out.hits.push(h) } else if (h.vt < out.hits[i].vt) out.hits[i] = h
    }
  }
  return out
}

const sumIo = (ios: Io[]): Io => {
  const io: Io = { ...ios[0], ms: { ...ios[0].ms }, tiers: ios.length }
  for (const x of ios.slice(1)) {
    io.groups += x.groups; io.bytes += x.bytes; io.rows_read += x.rows_read; io.rows_matching += x.rows_matching
    for (const [k, v] of Object.entries(x.ms)) io.ms[k] = Math.max(io.ms[k] ?? 0, v)
  }
  return io
}

const noIo = (): Io => ({ shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none', ms: {} })

/** The suffix reader over every usable tier (`Tiers.each`), with `StaticNames`' interface; each answer carries the
 *  stack it was read over (`version`) and the scans it is exact on (`scans`). */
export class TieredNames {
  constructor(readonly tiers: Tiers) {}

  async version(): Promise<string> { return (await this.tiers.state()).version }
  /** The generation's hex-run rule (the base's; every usable tier agrees), null = the full index. */
  async hexRuns(): Promise<HexRule | null> { return (await this.tiers.state()).hexRuns }
  /** The current stack: its version and the scans it covers. */
  async snapshot(): Promise<{ version: string; scans: string[] }> { const { version, scans } = await this.tiers.state(); return { version, scans } }

  /** Σ over tiers of the rows `key`'s range spans (row-group granular): an upper bound on rows read, and since
   *  a range's rows only grow and close records only add, ≤ V still proves `key` is no catalog member. */
  async extent(key: string, io: Io): Promise<{ rows: number; scans: string[] } | null> {
    const { state, got } = await this.tiers.each(async t => { const x = noIo(); return { x, e: await t.names.extent(key, x) } })
    const exts = got.map(g => g.e)
    Object.assign(io, { shard: got[0]?.x.shard ?? null, index: got[0]?.x.index ?? 'none', tiers: state.tiers.length })
    if (exts.every(e => e == null)) return null
    io.groups = exts.reduce((a, e) => a + (e ? e.b - e.a : 0), 0)
    return { rows: exts.reduce((a, e) => a + (e?.rows ?? 0), 0), scans: state.scans }
  }

  /** `key`'s first hits over every tier; `maxRows` refuses (null) when the tiers' summed extent is above it. */
  async read(key: string, maxRows = Infinity): Promise<{ io: Io; fold: FirstHits | null; version: string; scans: string[] }> {
    if (maxRows !== Infinity) {
      const probe = noIo()
      const ext = await this.extent(key, probe)
      if (ext && ext.rows > maxRows) { const { version, scans } = await this.tiers.state(); return { io: { ...probe, rows_read: ext.rows }, fold: null, version, scans } }
    }
    const { state, got } = await this.tiers.each((t, st) => t.names.read(key, Infinity, { rule: st.hexRuns }))
    const io = got.length ? sumIo(got.map(g => g.io)) : { ...noIo(), tiers: 0 }
    return { io, fold: combineFolds(key, got.map(g => g.fold!), state.hexRuns), version: state.version, scans: state.scans }
  }

  async answer(key: string, dates: string[], maxRows = Infinity) {
    const { io, fold, scans } = await this.read(key, maxRows)
    return { io, answer: fold ? fold.answer(dates) : null, scans }
  }
}

/** The catalog over every usable tier, with `StaticCatalog`'s `info`/`lookup` (a lookup's `scans`: the ones it is
 *  exact on). */
export class TieredCatalog {
  constructor(readonly tiers: Tiers) {}

  /** The base's `meta.json` (V is the generation's). */
  info(): Promise<CatalogMeta> { return this.tiers.base().catalog.info() }

  async lookup(q: string): Promise<{ io: CatalogIo; member: Member | null; scans: string[] }> {
    const { state, got } = await this.tiers.each(t => t.catalog.lookup(q, { partial: true }))
    const { tiers, scans } = state
    if (!got.length) return { io: { groups: 0, bytes: 0, rows_read: 0, index: 'isolate' }, member: null, scans }
    const io: CatalogIo = { ...got[0].io }
    for (const g of got.slice(1)) { io.groups += g.io.groups; io.bytes += g.io.bytes; io.rows_read += g.io.rows_read }
    const members = got.map(g => g.member).filter((m): m is Member => m != null)
    if (!members.length) return { io, member: null, scans }
    const head = members[members.length - 1]
    const cells = members.flatMap(m => m.cells).sort((x, y) => cmp(x.bucket, y.bucket) || (x.vf < y.vf ? -1 : x.vf > y.vf ? 1 : 0))
    // The newest tier's header counts the cells of every tier through it (a cut stack's too).
    if (head.n != null && cells.length !== head.n) throw new Error(`static catalog: ${JSON.stringify(q)} header counts ${head.n} cells, the ${tiers.length} tiers hold ${cells.length}`)
    return { io, member: { q, rows: head.rows, cells }, scans }
  }
}

/** The tiers' readers over one `Blobs`, with the given per-tier index caches. */
export function tiers(blobs: Blobs, opts: {
  indexCache?: (dir: string | null) => IndexCache<GroupIndex> | undefined
  catalogCache?: (dir: string | null) => IndexCache<import('./staticCatalog.js').CatalogIndex> | undefined
  clock?: () => Promise<number>
  ttlMs?: number
  now?: () => number
  /** A cut state's lifetime (`Tiers`). */
  brokenTtlMs?: number
  log?: (msg: string) => void
} = {}): { tiers: Tiers; names: TieredNames; catalog: TieredCatalog; scans: () => Promise<string[]>; dirOnly: () => Promise<string[]> } {
  const t = new Tiers(blobs, (b, dir) => ({
    names: new StaticNames(b, opts.indexCache?.(dir), opts.clock),
    catalog: new StaticCatalog(b, opts.catalogCache?.(dir)),
  }), opts.ttlMs, opts.now, opts.brokenTtlMs, opts.log)
  return { tiers: t, names: new TieredNames(t), catalog: new TieredCatalog(t), scans: async () => (await t.state()).scans, dirOnly: async () => (await t.state()).dirOnly ?? [] }
}
