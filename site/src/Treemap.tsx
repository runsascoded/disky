import { Explain } from './Help'
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { stringParam, useUrlState } from 'use-prms'
import { DustHatch, Treemap as DtTreemap } from '@disk-tree/react'
import type { CellCtx, CellStyle } from '@disk-tree/react'
import { Avatar } from './Avatar'
import { CopyName, copyText } from './CopyName'
import { FaRegCopy } from 'react-icons/fa6'
import { pathCopy, pathCrumbs, pathDisplay, pathText } from './pathCrumbs'
import { cellAction } from './objects'
import { canonId, UserChip, ghHandle, shortName } from './UserChip'
import { dateColor, dateGradientCss, epochDaysToDate, epochDaysToMonth, inkFor, slotColor, userColor } from './colors'
import type { UserIndexEntry } from './colors'
import { OwnerControls } from './OwnerControls'
import type { OwnerIndex } from './owners'
import { ClassMixTip, Tooltip } from './Tooltip'
import type { ColorMode, Pricing, TreeNode } from './types'
import { CLASS_NAMES, classMix, fmtN, fmtUsd, ratePerByte, unclaimedBytes } from './types'
import { SettingsMenu, useRenderer, useTiling } from './prefs'
import { useUnits } from './units'
import { usePerfCommit } from './perf'

// Legend rows inline only the metrics toggled on (swatch + name always show).
// URL param `?li=` — a subset of "spc" (size / percent / cost); absent = "s"
// (just sizes: all three at once made the bar unreadable).
type LiMetric = 's' | 'p' | 'c'
const LI_METRIC_CHIPS: [LiMetric, string, string][] = [
  ['s', 'size', 'Show each legend row’s bytes'],
  ['p', '%', 'Show each legend row’s share of the current view'],
  ['c', '$', 'Show each legend row’s estimated storage cost ($/mo, list price)'],
]

/** The path atop a docked/pinned tooltip, as drillable per-segment crumbs: each
 * ancestor segment drills the map to that level (the deepest — the cell itself,
 * or a folded `(other)` — is inert, bold). A copy icon ejects the whole prefix
 * to the CLI (via `copyText`, which also works off the tailnet dev server's
 * insecure origin). Its own component so the copy state has somewhere to live
 * (renderTooltip is a plain function). */
function PathBar({ path, scheme, home, onDrill }: { path: TreeNode[]; scheme: string; home?: string[]; onDrill?: (p: TreeNode[]) => void }) {
  const [copied, setCopied] = useState(false)
  const names = path.slice(1).map(n => n.n)
  const uri = pathCopy(scheme, names)
  // A store home folds to a `~` lead (drilling to the home dir); a file
  // store's other paths lead with `/`, not `file:///`.
  const { lead, leadSegs } = pathDisplay(scheme, names, home)
  const crumbs = pathCrumbs(names).slice(leadSegs)
  const homeNode = leadSegs ? path.slice(0, leadSegs + 1) : null
  return (
    <div className="path" onClick={e => e.stopPropagation()}>
      <span className="crumbs">
        {homeNode && onDrill && crumbs.length
          ? <button type="button" className="seg" onClick={() => onDrill(homeNode)}>~</button>
          : <span className={'dirname' + (homeNode && !crumbs.length ? ' basename' : '')}>{lead}</span>}
        {crumbs.map(({ name, segs, drillable, last }, i) => (
          <span key={i}>
            {(i > 0 || homeNode) && <span className="sep">/</span>}
            {onDrill && drillable
              ? <button type="button" className="seg" onClick={() => onDrill(path.slice(0, segs.length + 1))}>{name}</button>
              : <span className={'seg' + (last ? ' basename' : '')}>{name}</span>}
          </span>
        ))}
      </span>
      <button
        type="button" className="path-copy" title={copied ? 'copied ✓' : 'Copy path to clipboard'} aria-label="Copy path to clipboard"
        onClick={() => copyText(uri).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200) })}
      >{copied ? <span className="copied">✓</span> : <FaRegCopy aria-hidden />}</button>
    </div>
  )
}

// A top-level prefix holding more than this share of the store is split one
// level deeper for colouring (see catSlot).
// Tree-mode colouring is relative to the *drilled* root: its direct children
// (L1) take the distinct category hues, ranked by size — the macro axis — and
// each L1's own children (L2) fan across shades of that hue — the micro axis;
// deeper cells inherit their L2 ancestor's shade. Drilling re-keys both, so
// whatever you're looking at gets the full palette.
const MAX_SLOTS = 8 // legend entries; the map colours every child (slotHsl)

/**
 * Legend names in one directory tend to share a long run-name prefix
 * (`exp5611_sft_qwen3_1_7b_swe_zero_…` × 4) that carries no information
 * *within* the legend and, on a phone, is the whole first fold. Cluster names
 * whose token-aligned common prefix is long enough, render the prefix once,
 * and give the swatches to the parts that differ. Rank order is kept: a
 * cluster sits where its first member would.
 */
const MIN_PFX = 12
function tokenPrefix(a: string, b: string): string {
  let i = 0
  while (i < a.length && i < b.length && a[i] === b[i]) i++
  const cut = a.slice(0, i).search(/[_\-./][^_\-./]*$/)
  return cut < 0 ? '' : a.slice(0, cut + 1)
}
export function legendGroups(names: string[]): { prefix: string; names: string[] }[] {
  const parent = names.map((_, i) => i)
  const find = (i: number): number => (parent[i] === i ? i : (parent[i] = find(parent[i])))
  for (let i = 0; i < names.length; i++) {
    for (let j = i + 1; j < names.length; j++) {
      if (tokenPrefix(names[i], names[j]).length >= MIN_PFX) parent[find(j)] = find(i)
    }
  }
  const members = new Map<number, string[]>()
  names.forEach((n, i) => { const r = find(i); members.set(r, [...(members.get(r) ?? []), n]) })
  const out: { prefix: string; names: string[] }[] = []
  for (const ms of members.values()) {
    const pfx = ms.length > 1 ? ms.slice(1).reduce((p, n) => tokenPrefix(p, n), ms[0]) : ''
    if (ms.length > 1 && pfx.length >= MIN_PFX) out.push({ prefix: pfx, names: ms })
    else for (const n of ms) out.push({ prefix: '', names: [n] })
  }
  return out
}

/** Nesting levels of tiles a subtree renders as (see `viewLevels`). */
function tileLevels(node: TreeNode): number {
  const kids = node.c ?? []
  if (kids.length === 0) return 0
  const real = kids.filter(c => !c.n.startsWith('('))
  if (real.length === 1 && kids.length === 1) return tileLevels(real[0])
  let deepest = 0
  for (const k of kids) if (!k.n.startsWith('(')) deepest = Math.max(deepest, tileLevels(k))
  return 1 + deepest
}
const rankCache = new WeakMap<TreeNode, Map<string, [number, number]>>()
/** name → [rank, count] over a node's real (non-fold) children, largest first. */
function childRanks(node: TreeNode): Map<string, [number, number]> {
  let m = rankCache.get(node)
  if (!m) {
    const kids = (node.c ?? []).filter(c => !c.n.startsWith('(')).sort((a, b) => b.b - a.b)
    m = new Map(kids.map((c, i): [string, [number, number]] => [c.n, [i, kids.length]]))
    rankCache.set(node, m)
  }
  return m
}

// Micro-hue for the user axis: a cell's owner sets the hue (macro), and its
// storage-class mix nudges the shade (micro) — colder classes (Nearline /
// Coldline / Archive) pull the owner's color toward black, up to ~35% at 100%
// cold. Same hue family, so the legend swatch still identifies the person;
// within one person's band, darker just means colder. `color-mix` keeps this a
// string op that works for any color the palette hands back (hex, hsl, var()).
const coldShade = (color: string, cold: number): string =>
  cold > 0.01 ? `color-mix(in srgb, ${color} ${Math.round(100 - 35 * Math.min(1, cold))}%, black)` : color

export interface DateRange { min: number; max: number }

/** The secondary color axis ("shade by"): a perturbation *within* each cell's
 * primary color. `none` = the primary color as-is; `class` = darker for a
 * larger share of cold storage classes (`coldShade`). Opt-in — the primary
 * axis reads the same as before unless a shade is picked. */
export type ShadeMode = 'none' | 'class'

export interface Highlight {
  user?: string
  /** The unclaimed pool (bytes no person owns). */
  unclaimed?: boolean
}

// Domain wrapper over @disk-tree/react's generic <Treemap>: all layout,
// drill/crumb state, hover-pinning, folding, and keyboard nav live upstream;
// this file supplies marin's business logic (attribution color modes, class
// lens, $-pricing, rollup bar, tooltip content) through the accessor props.
// Scale a user's fleet-wide class-byte mix down to `b` bytes, so the rollup
// tooltip's table totals the $ figure it explains (rate × this view's bytes)
// rather than showing their whole-fleet Ti/$ next to a small slice.
const scaleMix = (mix: Record<string, number>, b: number): Record<string, number> => {
  const tot = Object.values(mix).reduce((s, x) => s + x, 0)
  return tot ? Object.fromEntries(Object.entries(mix).map(([c, x]) => [c, (x * b) / tot])) : mix
}

export function Treemap({ root, mode, shade = 'none', userIdx, dateRange, readRange, hl, onPickUser, onPickUnclaimed, onClearHl, pricing, lens, ownerLensed, scheme = 'gs://', home, redact, ownerIdx, initialPath, path, onPathChange, objects = false, onOpen }: {
  root: TreeNode
  mode: ColorMode
  /** Secondary color axis — see `ShadeMode`. Default `none`. */
  shade?: ShadeMode
  userIdx: Map<string, UserIndexEntry>
  dateRange: DateRange | null
  // Access-log observation window (epoch days) — domain of the read lens.
  readRange?: DateRange | null
  hl?: Highlight | null
  /** Legend-row interactions (plotly-style): hover a row = solo it (others
   *  fade, map dims to it), click = pin (sticky `hl`; click again, any empty
   *  spot, or `x` clears). Absent → rows are inert. */
  onPickUser?: (u: string) => void
  onPickUnclaimed?: () => void
  onClearHl?: () => void
  pricing?: Pricing | null
  lens?: boolean
  /** The view is filtered to one owner (`?o=<user>`): nodes carry that
   * person's slice alone, so the owner panel's inferred owner shows no share. */
  ownerLensed?: boolean
  // URI scheme for cell paths — `gs://` for GCS, `s3://` for CoreWeave.
  scheme?: string
  /** The store home, shown as `~` in the path bar (`Store.home`). */
  home?: string[]
  // OG-image mode: hide every text detail (cell labels, crumb/rollup bars, hint)
  // and render just the colored cells. Never set by the live app.
  redact?: boolean
  // The ownership ledger (`Store.owners`): the owner panel above the map and
  // in the pinned tooltip, with assignment controls for admins.
  ownerIdx?: OwnerIndex | null
  // Start drilled here (e.g. CW's lone bucket) — crumbs keep the ancestry.
  initialPath?: TreeNode[]
  // Controlled drill path + change reporting (upstream contract) — lets the
  // app keep the drill in the URL and command drills from worklist rows.
  path?: TreeNode[]
  onPathChange?: (p: TreeNode[]) => void
  /** The scan lists objects (a store generation — `listsObjects`): every
   *  directory cell drills, childless or not. A v1 scan's leaves are all
   *  directories, and one holding a single object pins instead (`cellAction`). */
  objects?: boolean
  /** An object cell was clicked: its path from the root (the leaf viewer opens it). */
  onOpen?: (p: TreeNode[]) => void
}) {
  usePerfCommit('treemap')
  const { fmtBytes, fmtBytesLike } = useUnits()
  const [liP, setLiP] = useUrlState('li', stringParam('s'))
  const liMetrics = useMemo(() => new Set([...(liP ?? 's')].filter((m): m is LiMetric => m === 's' || m === 'p' || m === 'c')), [liP])
  // Toggling rebuilds the value in canonical "spc" order, so equal selections
  // always serialize identically.
  const liToggle = (m: LiMetric) => setLiP(LI_METRIC_CHIPS.map(([k]) => k).filter(k => liMetrics.has(k) !== (k === m)).join(''))
  // Tiling is a user preference (header toggle): `shared` by default.
  const [tiling] = useTiling()
  const [renderer] = useRenderer()
  // Macro/micro hue for a cell, relative to the drilled root (see childRanks).
  // Every cell path starts with the drilled path, so the view root sits at a
  // fixed index — NOT `len - 2 - depth`: a collapsed chain (`run/…/checkpoints`
  // drawn as one tile) lengthens the path by the chain without adding depth,
  // and that arithmetic then walked past the root and colored by the *bucket's*
  // ranking (every chained run dir came out slot-0 blue).
  const drillLen = (path ?? initialPath)?.length ?? 1
  const slotOf = useCallback(
    (kidPath: TreeNode[]): { slot: number; i: number; n: number } | null => {
      const rootIdx = drillLen - 1
      const viewRoot = kidPath[rootIdx]
      const l1 = kidPath[rootIdx + 1]
      if (!viewRoot || !l1 || l1.n.startsWith('(')) return null
      const slot = childRanks(viewRoot).get(l1.n)?.[0]
      if (slot == null) return null
      const l2 = kidPath[rootIdx + 2]
      const [i, n] = l2 && !l2.n.startsWith('(') ? childRanks(l1).get(l2.n) ?? [0, 1] : [0, 1]
      return { slot, i, n }
    },
    [drillLen],
  )

  const uriOf = (path: TreeNode[]) => scheme + path.slice(1).map(n => n.n).join('/')

  // Fold merger: first-class TreeNode aggregating us/d so folded tiles keep
  // real tooltips (upstream calls this at every nesting level).
  const mergeSmall = useCallback((tiny: TreeNode[]): TreeNode => {
    const b = tiny.reduce((s, it) => s + it.b, 0)
    const o = tiny.reduce((s, it) => s + it.o, 0)
    const us: Record<string, number> = {}
    let wd = 0
    let wdb = 0
    let ma = -1
    for (const it of tiny) {
      for (const [u, ub] of it.us ?? []) us[u] = (us[u] ?? 0) + ub
      if (it.d != null) {
        wd += it.d * it.b
        wdb += it.b
      }
      if (it.a != null && it.a > ma) ma = it.a
    }
    const folded: TreeNode = { n: `(+${tiny.length})`, b, o }
    if (wdb) folded.d = Math.round(wd / wdb)
    if (ma >= 0) folded.a = ma
    const topUs = Object.entries(us).sort((a, c) => c[1] - a[1]).slice(0, 5)
    if (topUs.length) folded.us = topUs as [string, number][]
    return folded
  }, [])

  // Transient solo from hovering a legend row; a pinned `hl` (URL state) wins.
  const [hoverHl, setHoverHl] = useState<Highlight | null>(null)
  const effHl = hl ?? hoverHl
  // Pinned highlight clears on a click anywhere that isn't a legend row, the
  // map, or a control — the plotly "click empty space to unpin" convention.
  useEffect(() => {
    if (!hl || !onClearHl) return
    const onDoc = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (t?.closest('.ri, .dt-treemap-map, button, a, input, select, textarea, [role="dialog"], [role="tooltip"], .tt')) return
      onClearHl()
    }
    document.addEventListener('click', onDoc)
    return () => document.removeEventListener('click', onDoc)
  }, [hl, onClearHl])

  const colorForCell = useCallback(
    (kid: TreeNode, kidPath: TreeNode[], depth: number, ctx: CellCtx): CellStyle => {
      let bg: string
      let ink: string
      let segments: CellStyle['segments']
      // Cold-class share of the cell (NL/CL/AR bytes over all bytes) — the
      // "shade by" micro axis, applied to whichever base color the primary
      // axis picks. Ink is chosen from the base so labels stay legible.
      const cold = shade === 'class' && kid.b ? Object.values(kid.cb ?? {}).reduce((a, b) => a + b, 0) / kid.b : 0
      if (mode === 'tree') {
        const s = slotOf(kidPath)
        const base = s ? slotColor(s.slot, s.i, s.n) : 'var(--other)'
        bg = s ? coldShade(base, cold) : base
        ink = s ? inkFor(base) : 'var(--ink)'
      } else if (ctx.hasKids) {
        // container: neutral so the nested tiles carry the data colors
        bg = 'var(--panel)'
        ink = 'var(--ink)'
      } else if (mode === 'date') {
        if (kid.d != null && dateRange && dateRange.max > dateRange.min) {
          bg = dateColor((kid.d - dateRange.min) / (dateRange.max - dateRange.min))
          ink = inkFor(bg)
        } else {
          bg = 'var(--other)'
          ink = 'var(--ink)'
        }
      } else if (mode === 'read') {
        if (kid.a != null && readRange && readRange.max > readRange.min) {
          bg = dateColor((kid.a - readRange.min) / (readRange.max - readRange.min))
          ink = inkFor(bg)
        } else {
          // Never read since logging began — the sweep-interesting bucket.
          bg = 'var(--never-read)'
          ink = 'var(--ink)'
        }
      } else {
        // user: a wholly-owned (~100%) cell takes its user's color; a mixed
        // cell renders its top users as proportional stripes (with a gray
        // remainder for unclaimed bytes) instead of one blob.
        const [u, ub] = kid.us?.[0] ?? [null, 0]
        if (ub >= 0.98 * kid.b) {
          const base = userColor(u, userIdx)
          bg = coldShade(base, cold)
          ink = inkFor(base)
        } else {
          const us = (kid.us ?? []).filter(([, b]) => b >= 0.06 * kid.b).slice(0, 4)
          const rem = kid.b - us.reduce((s, [, b]) => s + b, 0)
          if (us.length && (us.length > 1 || rem >= 0.06 * kid.b)) {
            segments = us.map(([uu, b]) => ({ color: coldShade(userColor(uu, userIdx), cold), frac: b / kid.b }))
            if (rem >= 0.06 * kid.b) segments.push({ color: userColor(null, userIdx), frac: rem / kid.b })
            bg = 'var(--panel)'
            ink = 'var(--ink)'
          } else if (us.length === 1) {
            // One dominant user, remainder too small to stripe (<6%): their
            // color, not unattributed gray — covers the 94–98% window the
            // wholly-owned fast path above misses.
            const base = userColor(us[0][0], userIdx)
            bg = coldShade(base, cold)
            ink = inkFor(base)
          } else {
            bg = userColor(null, userIdx)
            ink = inkFor(bg)
          }
        }
      }
      // class lens: hatch by colder-class (non-STANDARD) byte fraction — leaf
      // cells only (cells are semi-transparent, so a parent hatch would bleed
      // through all-STANDARD children)
      const coldFrac = lens && !ctx.hasKids ? Object.values(kid.cb ?? {}).reduce((a, b) => a + b, 0) / kid.b : 0
      const hatch = coldFrac > 0.01
        ? `repeating-linear-gradient(135deg, rgb(120 170 255 / ${(0.18 + 0.5 * coldFrac).toFixed(2)}) 0 4px, transparent 4px 9px)`
        : undefined
      // highlight mode: leaf cells not majority-owned by the selected user
      // (or, for the unclaimed pin, not majority-unclaimed) fade back
      let dim = false
      if (effHl && !ctx.hasKids) {
        if (effHl.user) dim = (kid.us?.find(([u]) => u === effHl.user)?.[1] ?? 0) < 0.5 * kid.b
        else if (effHl.unclaimed) dim = unclaimedBytes(kid) < 0.5 * kid.b
      }
      // Shared-edge stroke, per cell: each neighbor paints its own half of a
      // boundary, so the line can adapt to the face it borders. Top-level
      // (bucket) rects take the page background — the strongest seam the
      // theme has — and deeper cells pull their own fill toward it, so even
      // grey-on-grey siblings show a visible edge. Gradient fills (stripe
      // segments paint over bg anyway) keep the themed default.
      // (`--surface` is the page ground; there is no `--bg` token, and an
      // undefined var here silently dropped the whole seam color.)
      const edge = depth === 0
        ? 'var(--surface)'
        : bg.includes('gradient')
          ? undefined
          : `color-mix(in oklab, ${bg} ${depth === 1 ? 40 : 62}%, var(--surface))`
      return { bg, ink, hatch, segments, edge, opacity: dim ? 0.22 : undefined }
    },
    [mode, shade, slotOf, userIdx, dateRange, readRange, effHl, lens],
  )

  // owner roll-up for the current view (user coloring only): everyone ≥1% of
  // the node, at least 5, at most 12; the rest roll into "(other users)";
  // what no person owns is the unclaimed pool. $ figures use class-aware
  // per-user rates when the snapshot carries them.
  const rollupFor = (node: TreeNode) => {
    if (mode !== 'user' || !node.us) return []
    const userRate = (u: string) => pricing && (pricing.userRates?.[u] ?? pricing.blended)
    const us = node.us
    const userTotal = us.reduce((s, [, b]) => s + b, 0)
    const unattr = Math.max(0, node.b - userTotal)
    const shown = us.filter(([, b], i) => i < 5 || (i < 12 && b >= 0.01 * node.b))
    const otherUsers = userTotal - shown.reduce((s, [, b]) => s + b, 0)
    return [
      ...shown.map(([u, b]) => ({ k: u, b, col: userColor(u, userIdx), rate: userRate(u), mix: pricing?.userMix?.[u], hl: { user: u } as Highlight | undefined })),
      ...(otherUsers > 0 ? [{ k: `(other users ×${us.length - shown.length})`, b: otherUsers, col: 'var(--other)', rate: pricing?.blended, mix: undefined, hl: undefined }] : []),
      ...(unattr > 0 ? [{ k: 'unowned', b: unattr, col: 'var(--t-unattr)', rate: pricing?.blended, mix: undefined, hl: { unclaimed: true } as Highlight | undefined }] : []),
    ].sort((a, b) => b.b - a.b)
  }

  // $/mo from the node's own storage-class mix at list price — the same
  // arithmetic as the Storage-classes table — not the store-wide blended
  // rate, which over-charged a Coldline-heavy directory by ~2×.
  const estUsd = (n: TreeNode) => n.b * (n.cb ? ratePerByte(classMix(n)) : pricing?.blended ?? 0)
  const drillPath = path ?? initialPath
  // Levels of tiles the view will draw (the loaded subtree is already
  // pixel-budgeted, so its depth ≈ what renders). Lone-child chains collapse
  // into one tile and don't count; folds are leaves. Drives the seam widths:
  // the fat gutter belongs to a level with two more under it — a flat
  // directory of step dirs is one level and gets hairlines, not the
  // bucket-grade 6px frame around every cell.
  const viewLevels = drillPath?.length ? tileLevels(drillPath[drillPath.length - 1]) : 1

  // Per-cell overlay: a dust hatch on every `(other)` fold, at ANY depth. The
  // server builds these tiles (view.ts: parent − Σ kept kids, with `f` = how
  // many children they stand in for), so they're plain nodes to the core and
  // never hit its own fold hatch — draw the core's `DustHatch` here so a fold
  // reads as "many small things", not a flat grey block.
  const renderCellExtra = (n: TreeNode, _cellPath: TreeNode[], box: { w: number; h: number; fade?: number; hasKids?: boolean; chain?: number }) =>
    n.n.startsWith('(') && box.w >= 8 && box.h >= 8
      ? <DustHatch w={box.w} h={box.h} count={Math.max(1, n.f ?? 1)} />
      : null

  const renderRollup = (node: TreeNode, path: TreeNode[]) => {
    if (redact) return null
    // The rollup follows the ACTIVE color axis: the owner breakdown renders
    // only when the map is colored by user (tree/written/read each key their
    // own legend). The owner panel is active whenever ownerIdx is, in every
    // coloring.
    const rollup = rollupFor(node)
    if (!ownerIdx && !rollup.length) return null
    // Nothing to key here (tree/date/read at a level with no owner panel, no
    // owner rows) — render no rollup at all, so an empty
    // `.dt-treemap-rollup` div doesn't sit as a gap between the crumb bar and
    // the map.
    const rollupRows = rollup.filter(r => r.b >= 0.001 * node.b)
    if (!hasPanel && rollupRows.length === 0) return null
    return (
      <>
        {/* A bucket itself isn't assignable (OwnerControls renders nothing
            there) — no panel for an empty box. */}
        {hasPanel && (
          <div className="root-owner">
            <OwnerControls uri={uriOf(path)} idx={ownerIdx!} node={node} lensed={ownerLensed} userIdx={userIdx} onPickUser={onPickUser} />
            {keys}
          </div>
        )}
        {rollupRows.map(r => {
          // Real per-user rows (not "(other users)"/"unowned") get a GitHub
          // avatar next to the color swatch.
          const isUser = mode === 'user' && !r.k.startsWith('(') && r.k !== 'unowned'
          const pickable = !!r.hl && !!(r.hl.user ? onPickUser : onPickUnclaimed)
          const same = (a: Highlight | null | undefined, b: Highlight | undefined) => !!a && !!b && a.user === b.user && !!a.unclaimed === !!b.unclaimed
          const pinned = same(hl, r.hl)
          // Hover-solo fades the other rows; a pin (the map is scoped to the
          // pinned row's bytes) hides them.
          const faded = !hl && !!hoverHl && !same(hoverHl, r.hl)
          const hidden = !!hl && !pinned
          const pick = () => {
            if (!r.hl) return
            if (pinned) onClearHl?.()
            else if (r.hl.user) onPickUser?.(r.hl.user)
            else if (r.hl.unclaimed) onPickUnclaimed?.()
          }
          return (
          <span
            className={`ri${pickable ? ' pickable' : ''}${pinned ? ' pinned' : ''}${faded ? ' faded' : ''}`}
            hidden={hidden}
            key={r.k}
            onMouseEnter={pickable ? () => setHoverHl(r.hl!) : undefined}
            onMouseLeave={pickable ? () => setHoverHl(null) : undefined}
            onClick={pickable ? pick : undefined}
            title={pickable ? (pinned ? 'Unpin (or press x)' : 'Click to pin this highlight') : undefined}
          >
            <span className="sw" style={{ background: r.col }} />
            {isUser ? <UserChip who={r.k} size={15} /> : r.k}
            {liMetrics.has('s') && <> <b>{fmtBytesLike(r.b, rollup[0]?.b ?? r.b)}</b></>}
            {liMetrics.has('p') && <span className="pct">{((100 * r.b) / node.b).toFixed(1)}%</span>}
            {r.rate != null && liMetrics.has('c') && (
              r.mix ? (
                <Tooltip content={<ClassMixTip mix={scaleMix(r.mix, r.b)} note="assumes this slice mirrors the user's fleet-wide class mix — the table is that mix scaled to this view's bytes" />}>
                  <span className="usd dotted">{fmtUsd(r.b * r.rate)}/mo</span>
                </Tooltip>
              ) : (
                <span className="usd">{fmtUsd(r.b * r.rate)}/mo</span>
              )
            )}
            {pinned && <span className="unpin" aria-hidden>✕</span>}
          </span>
        )})}
        {rollup.length > 0 && (
          <span className="li-metrics" role="group" aria-label="Legend row metrics">
            {LI_METRIC_CHIPS.map(([m, label, tip]) => (
              <Explain key={m} text={tip}>
                <button type="button" aria-pressed={liMetrics.has(m)} className={liMetrics.has(m) ? 'on' : ''} onClick={() => liToggle(m)}>{label}</button>
              </Explain>
            ))}
          </span>
        )}
      </>
    )
  }

  /* One keying strip per view: any CATEGORICAL axis keys through the roll-up
     bar (swatch + label + size + %, presence-filtered) — user,
     and state all render there, so a separate legend for them would be a
     strict-subset duplicate. This legend exists only for encodings the
     roll-up can't key: the date gradients (written/read). The tree palette
     needs no key either: each hue is a child of the drilled node, and those
     children are the map's own labelled tiles — the legend only repeated
     the first row of tile titles (and, on a phone, cost the first fold). */
  const modeLegend = (mode === 'read' && readRange) || (mode === 'date' && dateRange)
    ? () => (
        <>
          {mode === 'read' && readRange ? (
            <>
              <Tooltip content={`No reads observed since access logging began (${epochDaysToDate(readRange.min)}) — activity before that predates the logs, so "never read" really means "not read in the observed window".`}>
                <span className="li has-tt"><span className="sw" style={{ background: 'var(--never-read)' }} />never read*</span>
              </Tooltip>
              <span className="li gradli">
                {epochDaysToDate(readRange.min)}
                <span className="gradbar" style={{ background: dateGradientCss() }} />
                {epochDaysToDate(readRange.max)}
              </span>
            </>
          ) : dateRange ? (
            <span className="li gradli">
              {epochDaysToMonth(dateRange.min)}
              <span className="gradbar" style={{ background: dateGradientCss() }} />
              {epochDaysToMonth(dateRange.max)}
            </span>
          ) : null}
        </>
      )
    : null

  // The tiling toggle rides in the same slot (right of the crumbs, left of ⛶)
  // in every mode: a map-level preference belongs on the map, not in the nav.
  // The keys (⚙, fullscreen) are the last flex items with `margin-left:
  // auto`: they take the free end of the LIs' last line, or wrap to a line of
  // their own only when there's no room — never a mostly-empty row. They sit
  // at the right edge of the owner panel (its head-matter row has the room);
  // without a panel (bucket level, no ledger) they take the free end of the
  // legend's last line instead. The core's own ⛶ is hidden.
  const hasPanel = !!ownerIdx && (path?.length ?? 0) > 2
  const keys = (
    <span className="keys">
      <SettingsMenu />
      <Explain text="Fullscreen (Esc to leave)">
        <button
          type="button" className="fs-btn" aria-label="Fullscreen"
          onClick={e => {
            const el = (e.currentTarget as HTMLElement).closest('.treemap') as HTMLElement | null
            if (!el) return
            if (document.fullscreenElement) void document.exitFullscreen()
            else void el.requestFullscreen()
          }}
        >⛶</button>
      </Explain>
    </span>
  )
  // The legend row exists only for a gradient key (written/read); the keys
  // (⚙, ⛶, outline key) live on the footer row with the totals, so nothing
  // reserves a line above the map for two icons.
  const legend = () => (modeLegend ? <div className="legend">{modeLegend()}</div> : null)

  const renderTooltip = (n: TreeNode, path: TreeNode[]) => {
    const userMode = mode === 'user'
    const mix = classMix(n)
    const classes = n.cb && (
      <div className="classes-row">
        {Object.entries(mix).map(([c, b]) => (
          <span className="tt-cls" key={c}>
            {CLASS_NAMES[c]} {((100 * b) / n.b).toFixed(0)}%
          </span>
        ))}
        {pricing && <span className="usd">{fmtUsd(n.b * ratePerByte(mix))}/mo</span>}
      </div>
    )
    const users = n.us && n.us.length > 0 && (
      <div className="users">
        {n.us.map(([u, b]) => (
          <div className="tt-user" key={u}>
            {userMode && <span className="sw" style={{ background: userColor(u, userIdx) }} />}
            <Avatar github={ghHandle(u)} name={shortName(u)} size={13} /> {shortName(u)} · {fmtBytes(b)}
          </div>
        ))}
      </div>
    )
    return (
      <>
        <PathBar path={path} scheme={scheme} home={home} onDrill={onPathChange} />
        <div className="nums">
          {fmtBytes(n.b)} · {fmtN(n.o)} objects · {((100 * n.b) / root.b).toFixed(2)}% of total
          {n.d != null && <> · mean created {epochDaysToMonth(n.d)}</>}
          {n.a != null
            ? <> · last read {epochDaysToDate(n.a)}</>
            : readRange && !n.n.startsWith('(') && <> · <span className="never-read">no reads since {epochDaysToDate(readRange.min)}</span></>}
        </div>
        {classes}
        {users}
        {/* interactive only when the tooltip is pinned; CSS hides it on hover */}
        {ownerIdx && !n.n.startsWith('(') && <OwnerControls uri={uriOf(path)} idx={ownerIdx} node={n} lensed={ownerLensed} userIdx={userIdx} onPickUser={onPickUser} />}
        {/* The passive preview says where the controls are: any cell, at any
            depth, assigns from its pinned box (the table below only lists the
            drilled node's children). Gone once pinned or hovered into. */}
        {ownerIdx && !n.n.startsWith('(') && <div className="tt-hint tt-pin-hint">{cellAction(n, objects) === 'pin' ? 'click' : '⌥-click'} to pin · assign it here</div>}
      </>
    )
  }

  return (
    <DtTreemap<TreeNode>
      root={root}
      initialPath={initialPath}
      path={path}
      onPathChange={onPathChange}
      // `k` decides (`cellAction`): an object opens in the leaf viewer; a
      // directory drills even when it arrived without `c` (its children fell
      // below this view's pixel budget — the drill's own fetch brings them),
      // where the core would treat it as a leaf and pin its tip.
      onCellClick={(n, p, e) => {
        const act = cellAction(n, objects, e.altKey)
        if (act === 'open' && onOpen) { onOpen(p); return true }
        if (act === 'drill' && onPathChange) { onPathChange(p); return true }
        return false
      }}
      getSize={n => n.b}
      getChildren={n => n.c}
      getLabel={n => n.n}
      getId={(_n, p) => uriOf(p)}
      formatSize={fmtBytes}
      collapseChains
      mergeSmall={mergeSmall}
      colorForCell={colorForCell}
      // Opaque cells. Upstream's default fades every nesting level by 0.82,
      // which compounds: this store nests 6+ deep under `marin/datakit/...`,
      // so leaves landed near 0.4 alpha and every category washed out to the
      // same pale grey-blue. Structure comes from borders instead (app.scss).
      depthFade={1}
      rootFade={1}
      // Depth-emphasized seams: the core default (max(1, 3-depth)) tops out
      // at 1.5px painted per side — invisible between same-grey buckets. Give
      // the top level a fat gutter (3px per side → 6px between buckets), one
      // step down a clear line, leaves a hairline. Colors come from
      // `colorForCell`'s `edge` (page-bg at depth 0, fill-adaptive below).
      // Capped by cell size: drilling into a flat dir puts hundreds of small
      // cells at depth 0, where the bucket-grade 6px seam eats the area
      // shared-edges mode exists to preserve.
      borderWidth={(depth, { w, h }) => {
        const below = Math.max(0, viewLevels - 1 - depth)
        // Width alone is a blunt cue: the top seam gets 3px and one step down
        // 2px, the rest hairlines — nesting reads from the edge color (page
        // background at the top, fill-tinted deeper) and the header bars.
        const base = below >= 2 ? 3 : below === 1 ? 2 : 1
        return Math.min(base, Math.max(1, Math.min(w, h) / 16))
      }}
      tiling={tiling}
      renderer={renderer}
      renderTooltip={renderTooltip}
      // One docked panel below the map (above the table) that updates in place,
      // instead of a tip that chases the pointer up and down a lineage and
      // covers the cells/controls under it.
      tipMode="dock"
      // Resting state (nothing hovered / on a phone): a card about the whole
      // current view, so the panel never collapses to an empty stub.
      renderTipDefault={(n, p) => (
        <div className="tip-viewcard">
          <div className="vc-scope">{p.length > 1 ? pathText(scheme, p.slice(1).map(x => x.n), home) : n.n}</div>
          <div className="nums">
            {fmtBytes(n.b)} · {fmtN(n.o)} objects · {root.b ? ((100 * n.b) / root.b).toFixed(2) : 0}% of total
            {n.d != null && <> · mean created {epochDaysToMonth(n.d)}</>}
          </div>
          <div className="vc-hint">Hover a cell for its details · click one to open it.</div>
        </div>
      )}
      renderCellExtra={renderCellExtra}
      renderRollup={renderRollup}
      renderLegend={redact ? undefined : legend}
      renderCrumbSuffix={node => (
        <>
          — {fmtBytes(node.b)} · {fmtN(node.o)} objects
          {pricing && <> · est. {fmtUsd(estUsd(node))}/mo</>}
        </>
      )}
      renderFooter={redact
        ? undefined
        : node => (
          <div className="hint">
            <span className="stats">{fmtBytes(node.b)} · {fmtN(node.o)} objects{pricing && <> · est. {fmtUsd(estUsd(node))}/mo</>}</span>
            <Tooltip content={<>Click a directory to drill in · click an object to open it · ⌥-click any cell to pin its details · click the path above (or Backspace) to go up · small children fold into “(other)” · j/k select rows in the table below</>}>
              <span className="info" aria-label="how to use the map" tabIndex={0}>ⓘ</span>
            </Tooltip>
            {!hasPanel && keys}
          </div>
        )}
      chrome={!redact}
      showLabels={!redact}
      // Names over sizes: a cell narrower than this shows only its (often
      // long) path segment; the size waits in the tooltip / a tall leaf's 2nd
      // line. The core's default (90px) let a 3-char stub sit beside "694 GiB".
      inlineSizeMinWidth={150}
      // Per-store style hook: deliberate CW/GCS presentation differences live
      // under these classes in app.scss (one codebase, no branches).
      // At the un-drilled root the crumb bar is just the root label + totals —
      // a redundant near-empty row (the topbar already names the view, the
      // footer + resting card carry the totals) that read as a gap under the
      // controls. Hide it there; a drill (breadcrumbs) or a gradient legend
      // brings it back. `bar-hidden` → app.scss.
      className={`treemap store-${scheme === 's3://' ? 'cw' : 'gcs'} tiling-${tiling}${drillLen <= 1 && !modeLegend ? ' bar-hidden' : ''}`}
    />
  )
}
