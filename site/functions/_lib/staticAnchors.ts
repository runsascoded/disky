/** Anchored name search over the static index (specs/search-extensions.md §2; specs/anchored-search.md): `^q` (a
 *  name starts with `q`), `q$` (a name ends with `q`) and `^q$` (a name is `q`), exact on every scan the tiers cover.
 *
 *  Semantics: a path matches when some segment (lowercased) matches; the predicate is monotone down the tree, so the
 *  match roots under a view path P are the paths under P whose name matches and **none of whose ancestor segments
 *  match** — per segment, not "the parent path contains `q`" (`tomato.txt` under `big-tomato/` is a root of
 *  `^tomat`). Each root is one version of one owner slice, as the contains literal's first hits.
 *
 *  Where the rows are:
 *  - `q$`: the suffix shards' rows with `s == q` (a name's suffix from some position equal to `q` is exactly "the
 *    name ends with `q`"; one per version), sorted by `path` — so under P they are the key range
 *    `[(q, P/), (q, P0))`, bounded by whole row groups through the group index and the shards' `path` statistics
 *    (`PathKeys`). The suffix shards are its roots file; no copy is built.
 *  - `^q`, `^q$`: the **name index** (`names/`, the suffix shards' layout): one row per version, `s` = `/` + the
 *    lowercase name. `/` occurs in no segment, so `^q` is the prefix range `/q…` and `^q$` the exact key `/q`
 *    (sorted by path, so scoped like `q$`).
 *
 *  Dispatch, per (key, P):
 *  1. **Light**: the key's whole range (all tiers) ≤ `MAX_ROWS` rows → read once, folded to first hits, held per
 *     isolate and colo per stack version, cut to P (as `SuffixHits`). `q$` reads every tier of the light index
 *     (its sx), the name index's keys the anchored tiers.
 *  2. **Scoped** (`q$`, `^q$` past 1): over the anchored tiers, the groups meeting `[(k, P/), (k, P0))`; their rows
 *     ≤ `R + 4·Σ rg` (each tier's bound is at most its true rows plus four groups: two straddling P's range, two
 *     holding other keys at the ends of `k`'s run) → read, keeping the rows under P.
 *  3. **Rollup**: else `(k, P)` holds more than R rows over the tiers, so the builder (`static_anchors.py`) made its
 *     rollup — per child of P its running totals, K kept plus an exact remainder (the drill's layout,
 *     `anchors/{end,exact}-rollups-index.parquet`), stacked newest tier first (`DRILL_RULES.stack`). P = `''` has
 *     one too (children = buckets).
 *  `^q` reads up to `START_MAX_ROWS` (400K rows) whole. Past that:
 *  - the fleet root: the **starts-with catalog** (`anchors/start/`, the tiers that carry it): per heavy prefix, per
 *    bucket, the running totals of its first hits — exact per-bucket bytes and objects (`bucketsOnly`), the rollup
 *    layout at directory `''`, stacked newest tier first;
 *  - a view path P: the groups of the prefix's range whose path bounds meet `[P/, P0)`. Within the range the rows are
 *    sorted by `(s, path)`, so P's rows are contiguous per name, not across the range; a group's path bounds still
 *    rule it out when they miss P. Read when those groups hold ≤ `START_MAX_ROWS` rows, else `term-too-common`.
 *  Either read is abandoned past `START_MAX_HITS` first hits (the isolate's memory): the whole range then counts as
 *  heavy (the root from the catalog), a scoped one as `term-too-common`.
 *
 *  Tiers: the base and each run in the light index's manifest, up to the first run without `anchors/meta.json` (the
 *  run's name rows and rollups, written last) — their scans are the ones an anchored answer covers (`Found.scans`). */
import { type Found, type HitCache, type HitOpts, type HitSource, MAX_ROWS } from './staticFilter.js'
import { DRILL_RULES, type DrillMeta, GroupFile, type Rollup, type RollupCell, type Top } from './staticDrill.js'
import { type Blobs, cmp, FirstHits, type Hit, type IndexCache, type GroupIndex, type PathKeys, type SxColumns, StaticNames, underRange } from './staticNames.js'
import { combineFolds, subBlobs, type Tiers, type TierState } from './staticRuns.js'
import type { FilterRejectCode } from './indexedOnly.js'
import { dropped, type HexRule, occurs, ruleOf, sameRule, segmentOccurs } from './hexRuns.js'

export const NAMES_DIR = 'names'
export const ANCHORS_DIR = 'anchors'
/** The starts-with catalog's directory under `anchors/` (its own `meta.json`: tiers published before it keep theirs). */
export const START_DIR = 'start'
/** `^q`'s read bound, whole range or scoped under a view path: 400K rows plus two row groups (a cold decode of a few
 *  seconds; `static_anchors.START_MAX_ROWS`). */
export const START_MAX_ROWS = 400_000 + 2 * 8192
/** The first hits a `^q` read may hold (whole range or scoped): measured on gcs `2026-10-08c`, `^train`'s 246,555 first
 *  hits (311K rows decoded) held 72 MB of heap after GC, 135 MB at peak, and 36 MB as the colo cache's JSON — past a
 *  Worker isolate's 128 MB. A read past this many is abandoned as too common (the root falls to the catalog). ~45 MB. */
export const START_MAX_HITS = 150_000

export type Mode = 'start' | 'end' | 'exact'
/** A term as the static index reads it: its lowercase literal and anchors. */
export interface StaticTerm { text: string; start: boolean; end: boolean }

/** The key a term travels under (`HitSource.hits`, cache keys): a plain literal itself, an anchored one marked with
 *  `/` — `/q` (`^q`), `q/` (`q$`), `/q/` (`^q$`). A static literal never holds `/`, so no key is both. */
export const termKey = (t: StaticTerm): string => `${t.start ? '/' : ''}${t.text}${t.end ? '/' : ''}`

/** A key's literal and mode (null: a plain contains literal). */
export function parseKey(key: string): { text: string; mode: Mode | null } {
  const start = key.startsWith('/'), end = key.length > 1 && key.endsWith('/')
  const text = key.slice(start ? 1 : 0, end ? -1 : undefined)
  return { text, mode: start && end ? 'exact' : start ? 'start' : end ? 'end' : null }
}

/** Whether a lowercase segment matches `text` in `mode` (null: contains) under the generation's hex-run rule
 *  (`segmentOccurs`; null: the full index). */
export const segmentMatches = (seg: string, text: string, mode: Mode | null, rule: HexRule | null = null): boolean =>
  segmentOccurs(seg, text, mode, rule)

/** Whether `path` (any case) holds the keyed term: some segment of its lowercase matches (a contains literal: the
 *  lowercase path contains it, which for a `/`-free literal is the same). The view root P holding it is the plain view. */
export function termInPath(key: string, path: string, rule: HexRule | null = null): boolean {
  const { text, mode } = parseKey(key)
  const l = path.toLowerCase()
  if (!mode) return occurs(text, l, rule)
  return path !== '' && l.split('/').some(seg => segmentMatches(seg, text, mode, rule))
}

/** An anchored term's first-hit fold over suffix rows (`q$`, `s` = the name's suffixes) or name rows (`^q`, `^q$`,
 *  `s` = `/` + the name): per row, the key test on `s`, depth ≥ 1, the paths under `under` (a scoped read's groups
 *  hold other paths too), and no ancestor segment matching. Each version holds at most one such row, so no dedup.
 *  `rule`: the generation's hex-run rule (`segmentOccurs`): a `q$` row (a kept suffix in a run's tail) whose
 *  occurrence the rule drops is no match, and an ancestor segment matches only where the term occurs under it. */
export class AnchoredHits extends FirstHits {
  readonly text: string
  readonly mode: Mode
  private lo: string | null = null
  private hi = ''
  /** Past `cap` first hits: the read is abandoned (`hits` emptied, later rows skipped). */
  over = false
  constructor(key: string, under?: string, readonly cap = Infinity, rule: HexRule | null = null) {
    super(key, rule)
    const k = parseKey(key)
    if (!k.mode) throw new Error(`static anchors: ${JSON.stringify(key)} is not an anchored key`)
    this.text = k.text
    this.mode = k.mode
    if (under) [this.lo, this.hi] = underRange(under)
  }

  /** The row key a row is matched on: `q` (suffix rows) or `/q` (name rows). */
  get rowKey(): string { return this.mode === 'end' ? this.text : `/${this.text}` }

  override add(cols: SxColumns): void {
    if (this.over) return
    const k = this.rowKey, exact = this.mode !== 'start', t = this.text, mode = this.mode, rule = this.rule
    this.rows_read += cols.path.length
    for (let i = 0; i < cols.path.length; i++) {
      const s = cols.s[i]
      if (exact ? s !== k : !s.startsWith(k)) continue
      const p = cols.path[i]
      if (this.lo != null && (cmp(p, this.lo) < 0 || cmp(p, this.hi) >= 0)) continue
      const slash = p.lastIndexOf('/')
      if (rule && mode === 'end') {
        const name = p.slice(slash + 1).toLowerCase()
        if (dropped(name, name.length - t.length, t.length, rule)) continue
      }
      this.rows_matching++
      if (cols.depth[i] < 1) continue
      if (slash >= 0 && p.slice(0, slash).toLowerCase().split('/').some(seg => segmentMatches(seg, t, mode, rule))) continue
      this.hits.push({ path: p, depth: cols.depth[i], usr: cols.usr[i], vf: Number(cols.vf[i]), vt: Number(cols.vt[i]), size: cols.size[i], n: cols.n_files[i] })
      if (this.hits.length > this.cap) { this.over = true; this.hits.length = 0; return }
    }
  }
}

/** Tiers' capped folds as one hit list, or null when any went over (or their union does). */
function cappedHits(key: string, folds: AnchoredHits[], cap: number): Hit[] | null {
  if (folds.some(f => f.over)) return null
  const hits = combineFolds(key, folds).hits
  return hits.length > cap ? null : hits
}

/** A tier's `anchors/meta.json`: R, K, the shards' row-group size `rg` (the dispatch slack), the rollup index's
 *  `idx_rg`, per rollup set its `row_groups` (`end_rollups`, `exact_rollups`), a run's `scans`, and the hex-run rule
 *  its rollups were built under (`hex_runs`; absent: none — a generation built before the rule). */
export interface AnchorsMeta extends DrillMeta { scans?: string[]; hex_runs?: unknown }

/** One tier's anchored readers: its name index, rollups and starts-with catalog (the base: `dir` null). */
class AnchorTier {
  private meta?: Promise<AnchorsMeta>
  private startMeta?: Promise<AnchorsMeta>
  readonly names: StaticNames
  readonly rollups: Record<'end' | 'exact', GroupFile>
  readonly start: GroupFile
  constructor(readonly blobs: Blobs, readonly dir: string | null, caches: AnchorCaches) {
    this.names = new StaticNames(subBlobs(blobs, NAMES_DIR), caches.index?.(dir, NAMES_DIR), undefined, 8, caches.keys?.(dir, NAMES_DIR))
    const anchors = subBlobs(blobs, ANCHORS_DIR)
    const meta = () => this.info()
    this.rollups = {
      end: new GroupFile(anchors, 'end', 'rollups', meta, caches.top?.(dir)),
      exact: new GroupFile(anchors, 'exact', 'rollups', meta, caches.top?.(dir)),
    }
    this.start = new GroupFile(subBlobs(anchors, START_DIR), 'start', 'rollups', () => this.startInfo(), caches.top?.(dir == null ? START_DIR : `${dir}/${START_DIR}`))
  }

  /** `anchors/start/meta.json`: the starts-with catalog's liveness marker. */
  startInfo(): Promise<AnchorsMeta> {
    this.startMeta ??= this.blobs.json<AnchorsMeta>(`${ANCHORS_DIR}/${START_DIR}/meta.json`).then(m => {
      for (const k of ['R', 'K', 'idx_rg'] as const) if (!Number.isSafeInteger(m[k]) || m[k] < 1) throw new Error(`static anchors: bad ${this.dir ?? 'base'} start meta.json ${k}`)
      return m
    })
    this.startMeta.catch(() => { this.startMeta = undefined })
    return this.startMeta
  }

  info(): Promise<AnchorsMeta> {
    this.meta ??= this.blobs.json<AnchorsMeta>(`${ANCHORS_DIR}/meta.json`).then(m => {
      for (const k of ['R', 'K', 'rg', 'idx_rg'] as const) if (!Number.isSafeInteger(m[k]) || m[k] < 1) throw new Error(`static anchors: bad ${this.dir ?? 'base'} meta.json ${k}`)
      return m
    })
    this.meta.catch(() => { this.meta = undefined })
    return this.meta
  }
}

/** The colo caches of a tier's anchored indexes (`dir` null: the base; `sub` the shard set). */
export interface AnchorCaches {
  index?: (dir: string | null, sub: string) => IndexCache<GroupIndex> | undefined
  keys?: (dir: string | null, sub: string) => IndexCache<PathKeys> | undefined
  top?: (dir: string | null) => IndexCache<Top> | undefined
}

/** The anchored tiers of a light-index state: the base and the runs (oldest first) up to the first without
 *  `anchors/meta.json`, with the scans they cover and the light tiers they pair with; `start`: the prefix of them
 *  carrying the starts-with catalog (`anchors/start/meta.json`) and its scans. */
interface AnchorState { version: string; scans: string[]; tiers: AnchorTier[]; light: TierState; start: { tiers: AnchorTier[]; scans: string[] } }

/** Answers held per isolate, across (key, path) views: the oldest dropped past this many hits + cells. */
const HELD = 400_000

/** The anchored terms' `HitSource` (keys from `termKey`; plain literals are not its business). */
export class AnchoredSource implements HitSource {
  private tiers = new Map<string, AnchorTier>()
  private live = new Set<string>()
  private held = new Map<string, Promise<Found | null>>()
  private sizes = new Map<string, number>()
  private whyNot = new Map<string, FilterRejectCode>()
  readonly heavy = true
  /** `light`: the light index's tiers (their sx: `q$`'s rows; their manifest: the runs); `blobs` the generation's
   *  keys; `cache` whole-range hit lists across isolates. */
  constructor(readonly light: Tiers, readonly blobs: Blobs, readonly opts: { cache?: HitCache | null; caches?: AnchorCaches; maxRows?: number; startMaxRows?: number; startMaxHits?: number } = {}) {}

  private get startMaxRows(): number { return this.opts.startMaxRows ?? START_MAX_ROWS }
  private get startMaxHits(): number { return this.opts.startMaxHits ?? START_MAX_HITS }

  private tier(dir: string | null): AnchorTier {
    const k = dir ?? ''
    let t = this.tiers.get(k)
    if (!t) { t = new AnchorTier(dir == null ? this.blobs : subBlobs(this.blobs, dir), dir, this.opts.caches ?? {}); this.tiers.set(k, t) }
    return t
  }

  private async isLive(dir: string, marker = `${ANCHORS_DIR}/meta.json`): Promise<boolean> {
    const key = `${dir}/${marker}`
    if (this.live.has(key)) return true
    if (!this.blobs.list) return false
    const yes = (await this.blobs.list(key)).includes(key)
    if (yes) this.live.add(key)
    return yes
  }

  /** The anchored stack over the light index's current state. */
  async state(): Promise<AnchorState> {
    const light = await this.light.state()
    const none = { version: 'none', scans: [], tiers: [], light, start: { tiers: [], scans: [] } }
    if (!light.tiers.length) return none
    let meta: AnchorsMeta
    try { meta = await this.tier(null).info() } catch { return none }
    // Built under another hex-run rule than the generation's (`static_anchors.py` records the one it read): its
    // `q$` rollups would answer another predicate, so the anchored tiers decline (`q$` still reads the light index).
    if (!sameRule(ruleOf(meta.hex_runs), light.hexRuns)) return none
    const runs = light.tiers.slice(1).map(t => t.dir!)
    const prefix = async (ds: string[], marker?: string) => { const live = await Promise.all(ds.map(d => this.isLive(d, marker))); const n = live.indexOf(false); return n < 0 ? ds : ds.slice(0, n) }
    const kept = await prefix(runs)
    const scansOf = (dirs: string[]) => { const dropped = new Set((light.manifest?.runs ?? []).filter(r => !dirs.includes(r.key)).flatMap(r => r.scans)); return light.scans.filter(s => !dropped.has(s)) }
    // The starts-with catalog: the base's, then each run's up to the first without one (none at all without the base's).
    const baseStart = await this.tier(null).startInfo().then(() => true, () => false)
    const startRuns = baseStart ? await prefix(kept, `${ANCHORS_DIR}/${START_DIR}/meta.json`) : []
    return {
      version: kept.length ? `${light.version}~${kept[kept.length - 1]}` : `${light.version}~base`,
      scans: scansOf(kept),
      tiers: [this.tier(null), ...kept.map(d => this.tier(d))],
      light,
      start: baseStart ? { tiers: [this.tier(null), ...startRuns.map(d => this.tier(d))], scans: scansOf(startRuns) } : { tiers: [], scans: [] },
    }
  }

  why(key: string): FilterRejectCode | undefined { return this.whyNot.get(key) }

  /** Whether the light index is there but the generation has no anchored tiers to read: no `anchors/meta.json`
   *  (a profile built without anchors), or one built under another hex-run rule (`state`). */
  private async anchorless(): Promise<boolean> {
    const st = await this.state()
    return !st.tiers.length && st.light.tiers.length > 0
  }

  async hits(key: string, under: string, opts: HitOpts = {}): Promise<Found | null> {
    const f = await this.read(key, under, opts)
    // `^q`, `^q$` and a heavy `q$` need the anchored tiers: without them it is `anchor-not-indexed` (the term's
    // form, not the scan, is the reason), never the approximate walk. A light `q$` answers from the light index.
    if (!f && parseKey(key).text && await this.anchorless()) this.whyNot.set(key, 'anchor-not-indexed')
    return f
  }

  private async read(key: string, under: string, opts: HitOpts): Promise<Found | null> {
    const { text, mode } = parseKey(key)
    if (!mode || !text) return null
    const t0 = Date.now()
    // A first paint of `^q`'s fleet root while its range is unread: the catalog now (a member's per-bucket totals),
    // the full read after (the view's other request starts it).
    if (opts.firstPaint && mode === 'start' && under === '' && !(await this.wholeSettled(key))) {
      const st = await this.state()
      const f = await this.startScoped(key, '', st)
      if (f) return { ...f, io: { ...f.io, firstPaint: true, ms: Date.now() - t0 } }
    }
    const whole = await this.whole(key, mode)
    if (whole === null) return null
    if (whole !== 'heavy') {
      const hits = under === '' ? whole.hits : whole.hits.filter(h => h.path.startsWith(under + '/'))
      return { hits, io: { ...whole.io, ms: Date.now() - t0 }, scans: whole.scans }
    }
    const st = await this.state()
    if (!st.tiers.length) return null
    // Keyed by the starts-with catalog's tiers too: a run's catalog can go live after its `anchors/meta.json`.
    const k = `${key}\0${under}@${st.version}#${st.start.tiers.length}`
    let p = this.held.get(k)
    if (p) { this.held.delete(k); this.held.set(k, p) } else {
      p = mode === 'start' ? this.startScoped(key, under, st) : this.scoped(key, mode, under, st)
      this.held.set(k, p)
      p.then(f => this.trim(k, f), () => this.held.delete(k))
    }
    const f = await p
    if (!f && mode === 'start') this.whyNot.set(key, 'term-too-common')
    return f && { ...f, io: { ...f.io, ms: Date.now() - t0 } }
  }

  /** `^q` past its whole-range bound under `under`: the fleet root from the starts-with catalog, a view path from the
   *  groups of the prefix's range whose path bounds meet it (≤ `startMaxRows` rows), else null (`term-too-common`). */
  private async startScoped(key: string, under: string, st: AnchorState): Promise<Found | null> {
    const k = `/${parseKey(key).text}`
    if (under === '') {
      if (!st.start.tiers.length) return null
      const lo: [string, string] = [k, ''], hi: [string, string] = [k, '\0']
      const io = { top: 'isolate' as const, index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 }
      const parts = await Promise.all(st.start.tiers.map(async t => {
        const sel = await t.start.select(lo, hi, io)
        return sel.groups?.length ? t.start.read(lo, hi, sel.groups, io) : []
      }))
      const got = DRILL_RULES.stack(parts)
      if (!got) return null
      const rollup: Rollup = { ...toRollup('', got), bucketsOnly: true, scopedBelow: true }
      return { rollup, io: { from: 'anchors', source: 'catalog', ...io, tiers: st.start.tiers.length }, scans: st.start.scans }
    }
    const readers = st.tiers.map(t => t.names)
    const sels = await Promise.all(readers.map(r => r.select(k, newIo(), { under })))
    const upper = sels.reduce((n, s) => n + (s?.rows ?? 0), 0)
    if (upper > this.startMaxRows) return null
    const fold = () => new AnchoredHits(key, under, this.startMaxHits, st.light.hexRuns)
    const got = await Promise.all(readers.map(r => r.read(k, Infinity, { under, fold })))
    const hits = cappedHits(key, got.map(g => g.fold as AnchoredHits), this.startMaxHits)
    if (!hits) return null
    return { hits, io: { from: 'anchors', source: 'scoped', upper, rows_read: got.reduce((n, g) => n + g.io.rows_read, 0), bytes: got.reduce((n, g) => n + g.io.bytes, 0), tiers: readers.length }, scans: st.scans }
  }

  private trim(k: string, f: Found | null): void {
    this.sizes.set(k, f?.hits?.length ?? f?.rollup?.cells.length ?? 0)
    let n = [...this.sizes.values()].reduce((s, x) => s + x, 0)
    for (const old of this.held.keys()) {
      if (n <= HELD || old === k) break
      n -= this.sizes.get(old) ?? 0
      this.held.delete(old); this.sizes.delete(old)
    }
  }

  private wholeHeld = new Map<string, Promise<Whole | 'heavy'>>()
  private settled = new WeakSet<Promise<Whole | 'heavy'>>()

  /** Whether `key`'s whole-range read is held and done (per isolate, at the current stack version). */
  private async wholeSettled(key: string): Promise<boolean> {
    const p = this.wholeHeld.get(`${key}@${(await this.state()).version}`)
    return !!p && this.settled.has(p)
  }

  /** Step 1: the key's whole range when it is light — `q$` over every light tier, the name index's keys over the
   *  anchored tiers — held per stack version (and per colo through `cache`); `'heavy'` past `maxRows`, null when no
   *  tier is there to read. */
  private async whole(key: string, mode: Mode): Promise<Whole | 'heavy' | null> {
    const maxRows = mode === 'start' ? this.startMaxRows : this.opts.maxRows ?? MAX_ROWS
    const st = mode === 'end' ? null : await this.state()
    const light = st?.light ?? await this.light.state()
    const version = st ? st.version : light.version
    const scans = st ? st.scans : light.scans
    if (!(st ? st.tiers.length : light.tiers.length)) return null
    const vkey = `${key}@${version}`
    let p = this.wholeHeld.get(vkey)
    if (!p) {
      p = (async () => {
        const cached = await this.opts.cache?.get(vkey)
        if (cached) return { hits: cached, io: { from: 'cache', version }, scans }
        const k = mode === 'end' ? parseKey(key).text : `/${parseKey(key).text}`
        const cap = mode === 'start' ? this.startMaxHits : Infinity
        const fold = () => new AnchoredHits(key, undefined, cap, light.hexRuns)
        const exact = mode !== 'start'
        const readers = st ? st.tiers.map(t => t.names) : light.tiers.map(t => t.names)
        const sels = await Promise.all(readers.map(r => r.select(k, newIo(), { exact })))
        const rows = sels.reduce((n, s) => n + (s?.rows ?? 0), 0)
        if (rows > maxRows) return 'heavy' as const
        const got = await Promise.all(readers.map(r => r.read(k, Infinity, { exact, fold })))
        const hits = cappedHits(key, got.map(g => g.fold as AnchoredHits), cap)
        if (!hits) return 'heavy' as const
        await this.opts.cache?.put(vkey, hits)
        return { hits, io: { from: mode === 'end' ? 'shards' : 'names', version, rows_read: got.reduce((n, g) => n + g.io.rows_read, 0), bytes: got.reduce((n, g) => n + g.io.bytes, 0), tiers: readers.length }, scans }
      })()
      this.wholeHeld.set(vkey, p)
      const held = p
      p.then(() => this.settled.add(held), () => this.wholeHeld.delete(vkey))
      while (this.wholeHeld.size > 64) this.wholeHeld.delete(this.wholeHeld.keys().next().value!)
    }
    return p
  }

  /** Steps 2–3 for `q$` / `^q$` under `under` over the anchored tiers. */
  private async scoped(key: string, mode: 'end' | 'exact', under: string, st: AnchorState): Promise<Found | null> {
    const text = parseKey(key).text
    const k = mode === 'end' ? text : `/${text}`
    // `q$` reads each tier's suffix shards (the light index's readers, same tiers), `^q$` its name index.
    const readers = mode === 'end' ? st.light.tiers.slice(0, st.tiers.length).map(t => t.names) : st.tiers.map(t => t.names)
    const metas = await Promise.all(st.tiers.map(t => t.info()))
    const bound = metas[0].R + 4 * metas.reduce((n, m) => n + m.rg, 0)
    const sels = await Promise.all(readers.map(r => r.select(k, newIo(), { exact: true, under })))
    const upper = sels.reduce((n, s) => n + (s?.rows ?? 0), 0)
    if (upper <= bound) {
      const fold = () => new AnchoredHits(key, under, Infinity, st.light.hexRuns)
      const got = await Promise.all(readers.map(r => r.read(k, Infinity, { exact: true, under, fold })))
      const hits = combineFolds(key, got.map(g => g.fold!)).hits
      return { hits, io: { from: 'anchors', source: 'roots', upper, rows_read: got.reduce((n, g) => n + g.io.rows_read, 0), tiers: readers.length }, scans: st.scans }
    }
    const lo: [string, string] = [k, under], hi: [string, string] = [k, under + '\0']
    const io = { top: 'isolate' as const, index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 }
    const parts = await Promise.all(st.tiers.map(async t => {
      const gf = t.rollups[mode]
      const sel = await gf.select(lo, hi, io)
      return sel.groups?.length ? gf.read(lo, hi, sel.groups, io) : []
    }))
    const got = DRILL_RULES.stack(parts)
    if (!got) throw new Error(`static anchors: (${JSON.stringify(key)}, ${JSON.stringify(under)}) bounds ${upper} rows but has no rollup`)
    return { rollup: toRollup(under, got), io: { from: 'anchors', source: 'rollup', upper, ...io, tiers: st.tiers.length }, scans: st.scans }
  }
}

/** A stacked rollup (`DRILL_RULES.stack`) as the views read it. */
function toRollup(dir: string, got: NonNullable<ReturnType<typeof DRILL_RULES.stack>>): Rollup {
  const num = (v: unknown) => Number(v)
  const cells: RollupCell[] = got.cells.map(r => ({ kind: r.kind as 1 | 2, child: r.child as string, vf: num(r.vf) * 1000, b: r.b as bigint, o: r.o as bigint }))
  return { dir, kept: num(got.head.vf), rows: num(got.head.b), children: num(got.head.o), cells }
}

type Whole = { hits: Hit[]; io: Record<string, unknown>; scans: string[] }

const newIo = () => ({ shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none' as const, ms: {} })
