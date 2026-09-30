import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { CONTAINER_BG, contrastEdge, DEFAULT_PALETTE, slotColor } from './colors'
import { DustHatch } from './DustHatch'
import type { FoldedNode, LayoutConfig } from './layout'
import { edgeEmphFactor, isFolded, layoutCells } from './layout'
import { foldSmall, foldThin, squarify, squarifyRemainder } from './squarify'
import { TreemapCanvas, renderMapToCanvas, type CanvasHit } from './TreemapCanvas'
import { OutlineOverlay } from './OutlineOverlay'
import type { OutlineGroups } from './outlines'
import { categoricalStyle, resolveRing, type StyleOpts } from './cellStyle'
import { canvasToPngBlob, composeExport, copyPng, defaultExportFilename, downloadPng } from './exportImage'
import type { ExportKind, ExportOptions } from './exportImage'
import { CheckIcon, CopyIcon, DownloadIcon, FullscreenIcon } from './chromeIcons'
import { useHoverPin } from './useHoverPin'

/**
 * Generic squarified treemap.
 *
 * Data shape is opaque: all extraction happens through accessors. That's
 * what lets marin's dense per-path `{ n, b, o, tm, sh, us, cb }` node and
 * disk-tree's `{ path, size, kind, children }` node share this component.
 *
 * The presentation surface is all slot-based, and every slot has a working
 * default:
 *   - `colorForCell` — override cell background/text/hatch. Default: an
 *     8-slot categorical hue by top-level index (stable across renders).
 *   - `renderTooltip` — return a React node for the hover/pin tooltip.
 *     Default: label + size (via `formatSize`).
 *   - `renderRollup` — extra row rendered above the map (e.g. team/user
 *     rollup + $/mo). Default: none.
 *   - `renderLegend` — legend row (right side of the crumbs bar).
 *   - `onCellClick` — override the built-in drill-on-branch,
 *     pin-tooltip-on-leaf behavior.
 *
 * Interaction is baked-in but overridable: click a branch to drill; click a
 * leaf to pin its tooltip; Backspace/Escape pops the drill stack; unpin
 * with Escape or an outside click.
 */

export interface TreemapProps<T> {
  root: T
  /**
   * Start drilled to this path (must begin with `root`). Applied on mount and
   * whenever `root`'s identity changes — crumbs still show the full ancestry,
   * so a store with one meaningful top-level container can open inside it.
   */
  initialPath?: T[]
  /**
   * Controlled drill path (must begin with `root`). When given, the component
   * renders this path and reports every drill/crumb/Backspace gesture through
   * `onPathChange` instead of keeping its own state — so a consumer can put
   * the path in the URL, or command a drill from outside (a table row, a
   * search hit). Mutually exclusive with `initialPath`.
   */
  path?: T[]
  /** Extract this node's *own* aggregated size (bytes, count, whatever). */
  getSize: (n: T) => number
  /** Extract children; return `undefined` or `[]` for leaves. */
  getChildren: (n: T) => T[] | undefined
  /**
   * Whether this node has children *that may not be loaded yet*. Only
   * consulted alongside `loadChildren`; without it, "has children" is just
   * `getChildren(n)?.length`.
   *
   * This is the distinction a lazily-loaded tree needs and `getChildren`
   * can't express: no children in hand means "leaf" to a fully-materialized
   * tree but "not fetched yet" to a paged one. A server that answers with a
   * bounded depth (disk-tree's `/api/scan?uri=…&depth=N`) knows the
   * difference and usually says so — `n_children > 0`, `kind === 'dir'`.
   */
  hasChildren?: (n: T) => boolean
  /**
   * Fetch a node's children on demand. Called when the *viewed* node's
   * children aren't in hand and `hasChildren` says it has some — one fetch
   * per drill, never per rendered cell, so drilling costs one request rather
   * than fanning out over everything on screen.
   *
   * Resolved children are cached inside the component (keyed by `getId`) for
   * as long as `root` is unchanged; `onChildrenLoaded` lets a consumer that
   * owns its own tree persist them too. Rejections surface through
   * `renderLoadError` with a retry.
   */
  loadChildren?: (n: T, path: T[]) => Promise<T[]>
  /** Called after `loadChildren` resolves, before the cells render. */
  onChildrenLoaded?: (n: T, path: T[], children: T[]) => void
  /** Map-area content while the viewed node's children load. Default: "Loading…". */
  renderLoading?: (n: T, path: T[]) => ReactNode
  /** Map-area content when a load rejects. Default: the message + a Retry button. */
  renderLoadError?: (n: T, path: T[], error: Error, retry: () => void) => ReactNode
  /** Human label shown in the cell (and in breadcrumbs). */
  getLabel: (n: T) => string
  /** Stable key for this node — defaults to the joined path label. */
  getId?: (n: T, path: T[]) => string
  /** Format `getSize`'s return value for display. Default: raw number toLocaleString. */
  formatSize?: (n: number) => string

  /**
   * Cell background + text color + optional hatch overlay. Return `null`
   * (or `undefined`) to defer to the default categorical palette for that
   * cell — useful for callers that only want to override a subset of cells
   * (e.g. only synthetic "…" placeholders).
   *
   * `ctx` carries render-time facts the data alone can't know: the cell's
   * on-screen dims and whether it renders nested child tiles (`hasKids`) —
   * containers usually want a neutral bg so the nested tiles carry the data
   * colors, and leaf-only treatments (hatch, highlight-dim) key off it.
   */
  colorForCell?: (n: T, path: T[], depth: number, ctx: CellCtx) => CellStyle | null | undefined
  /**
   * Post-resolution style transform, applied to whatever `colorForCell` or
   * the default palette produced — so lenses (e.g. the age fade in
   * `colors.ts`) *stack* on any color mode instead of replacing it. Return
   * null/undefined to leave the resolved style untouched. Not called for
   * synthetic folded tiles.
   */
  lens?: (n: T, path: T[], depth: number, ctx: CellCtx, style: CellStyle) => CellStyle | null | undefined
  /**
   * Collapse single-child wrapper chains into one cell labeled `a/…/z` (the
   * cell is the deepest node; the chain is recorded in the drill path).
   * Off by default — chains carry real information in some trees.
   */
  collapseChains?: boolean
  /** Optional extra content rendered inside the cell after the label. */
  renderCellExtra?: (n: T, path: T[], ctx: CellCtx) => ReactNode
  /** Tooltip body; return null to suppress the tooltip. */
  renderTooltip?: (n: T, path: T[]) => ReactNode
  /**
   * Tooltip placement. `'float'` (default) is a fixed tip that tracks the
   * pointer; `'dock'` renders one panel below the footer that updates in place
   * as the pointer moves through a lineage — no tip chasing the cursor or
   * covering cells/controls, and a phone (no hover) taps to pin into it. The
   * empty panel keeps a small footprint so the layout doesn't jump.
   */
  tipMode?: 'float' | 'dock'
  /** Dock mode only: a resting card shown in the docked tip panel when no cell
   *  is hovered (e.g. the current root's summary), instead of the empty hint. */
  renderTipDefault?: (n: T, path: T[]) => ReactNode
  /** Extra row above the map (e.g. rollup / totals). */
  renderRollup?: (n: T, path: T[]) => ReactNode
  /** Right side of the breadcrumbs bar. */
  renderLegend?: (n: T, path: T[]) => ReactNode
  /**
   * Replaces the default `— <size>` suffix after the breadcrumbs (e.g. to
   * add object counts / $-estimates). Return null to render no suffix.
   */
  renderCrumbSuffix?: (n: T, path: T[]) => ReactNode
  /** Row rendered below the map (e.g. a usage-hint footer). */
  renderFooter?: (n: T, path: T[]) => ReactNode
  /**
   * Right side of the cell size line — e.g. "$1.2/mo". Rendered inline with
   * the size when the cell is big enough to fit a subtitle; `dims` is the
   * cell's box (plus `hasKids`/`fade`) so the consumer can size its content
   * to the room left after the label (the size span never shrinks, so an
   * over-long subtitle would crush the name) and tell branches from leaves —
   * `n`'s own children may be lazily loaded rather than in hand.
   */
  renderCellSubtitle?: (n: T, path: T[], ctx: CellCtx) => ReactNode

  /**
   * Override the click handler. Return `true` to skip the built-in
   * drill/pin behavior (i.e. the consumer handled it).
   */
  onCellClick?: (n: T, path: T[], event: React.MouseEvent) => boolean | void
  /**
   * Make leaf-rendered cells real anchors (`<a href>`): native cursor,
   * middle/cmd-click, link hints (Vimium), crawlability. Return `undefined`
   * for cells that shouldn't be links. Cells that render nested tiles stay
   * `<div>`s (anchors can't nest), so this suits shallow maps best. Plain
   * clicks are `preventDefault`ed and flow through `onCellClick`/drill/pin
   * as usual — an SPA router intercepts while the href keeps its native
   * affordances.
   */
  cellHref?: (n: T, path: T[]) => string | undefined
  /** Called whenever the drill path changes (drill in, drill back). */
  onPathChange?: (path: T[]) => void
  /**
   * Fired when the pointer enters a cell (with the node + its path from root)
   * and again with `null` when it leaves the map / all cells — the outward
   * mirror of the inward `lens` highlight, so a consumer can reflect the map's
   * hover into another view (e.g. file-tree's split listing lights the row for
   * the hovered tile). Fires once per cell change, not per pixel; `null` on
   * leave. Debounce/grace is the consumer's concern. No behavior change when
   * absent. `path` is the root→cell node array, same shape as `onPathChange`.
   */
  onCellHover?: (n: T | null, path: T[]) => void
  /**
   * Fold small-area cells (below `minCellArea` in px²) into a synthetic
   * "…" tile. Pass `null` to disable folding.
   */
  minCellArea?: number | null
  /**
   * Fold cells whose *short side* renders below `minCellSide` px into one
   * synthetic tile — catches the tall, skinny slivers a dominant sibling
   * squeezes out, which have enough area to escape `minCellArea` but are too
   * narrow to hover or label. Pass `null` to disable. Default: 7.
   */
  minCellSide?: number | null
  /**
   * Build the folded stand-in as a *first-class* `T` (label, aggregated
   * size, and whatever the consumer's tooltip/colors need). When given, the
   * folded tile gets normal label/tooltip/click treatment; when omitted, a
   * synthetic gray `(+n)` tile with no tooltip is used.
   */
  mergeSmall?: (small: T[]) => T
  /** Show the fullscreen toggle button. Default: true. */
  fullscreen?: boolean
  /**
   * Add copy-PNG / download-PNG buttons to the bar (and `⌘/Ctrl+Shift+C` to
   * copy) that export the current view as an image. `true` ≡ `{}` (the bare
   * map); `{ title: true }` composites the crumb text above it. Canvas
   * renderer only — the export reads the map's `<canvas>` pixels directly (no
   * DOM-to-image dependency); ignored under the DOM renderer.
   */
  exportable?: boolean | ExportOptions<T>
  /** Fired after each export, with the PNG blob and how it left (for a
   *  "copied ✓" toast / analytics; the copy/download itself is already done). */
  onExport?: (blob: Blob, ctx: { kind: ExportKind }) => void
  /**
   * Wrap a control-bar button (export copy/download, fullscreen) with your own
   * tooltip lib — MUI `<Tooltip>`, `@floating-ui/react`, Radix, … — instead of
   * the browser-native `title`. Given the button's label and element, return
   * the wrapped node. When set, the native `title` is suppressed so tips
   * aren't doubled. Omitted → native `title` (the core stays dependency-free).
   */
  renderTip?: (label: string, button: ReactNode) => ReactNode
  /** Render the breadcrumbs/legend bar. Default: true. */
  chrome?: boolean
  /** Render in-cell labels. Default: true. (`false` + `chrome={false}` ≈ a redacted/og render.) */
  showLabels?: boolean
  /**
   * Where the inline size sits on a cell's first line (branch title bars and
   * short leaves — tall leaves always drop the size to a second line under the
   * name). `'left'` (default) sets it right after the name; `'right'` pushes
   * it to the cell's far edge. Both renderers honor it identically.
   */
  sizeAlign?: 'left' | 'right'
  /**
   * Minimum cell width (CSS px) at which a branch title-bar or short leaf
   * shows its size *inline* beside the name. Below it the name gets the whole
   * line (the size stays in the tooltip, and on a tall leaf's 2nd line).
   * Raise it when names matter more than sizes (long paths). Default: 90.
   */
  inlineSizeMinWidth?: number
  /** Extra className on the outer wrapper. */
  className?: string
  /** Style overrides on the map area. */
  mapStyle?: CSSProperties
  /**
   * Per-level fade applied to nested (depth > 0) cell *backgrounds*.
   * Default: 0.82.
   *
   * Each cell paints its background on a dedicated layer at opacity
   * `max(rootFade × depthFade^depth, fadeFloor)` — deeper paint recedes, but
   * label ink never fades: text renders outside the faded layer at full
   * strength at every depth. Pass `1` to keep saturation constant and lean on
   * borders for structure.
   */
  depthFade?: number
  /** Background opacity of the outermost (depth 0) cells. Default: 0.92. */
  rootFade?: number
  /**
   * Floor on the depth fade, so deep backgrounds don't wash out entirely
   * (depth 4 at the defaults would otherwise paint at ~0.5). Default: 0.75.
   */
  fadeFloor?: number
  /**
   * How sibling cells tile a parent's canvas.
   *
   * - `'gaps'` (default): every cell insets 2px per side, leaving gutters
   *   in the container color and rounded corners. Reads comfortably, but
   *   painted area under-represents by ~perimeter/area — a 6×6px cell
   *   paints 4×4 (44% loss), so area-proportionality can't hold at the
   *   coarsest and finest displayed levels at once.
   * - `'shared'`: cells occupy their exact squarify rects and share edges;
   *   each boundary is one stroke (`borderWidth`), half drawn by each
   *   neighbor as an inset ring in `--dt-treemap-edge` (defaults to the
   *   container color, so it reads like a thin gutter) — the paint layer
   *   insets by that much to expose it. Near-exact areas; the stroke is the
   *   only residual.
   * - A callback decides per subtree — called for the node whose children
   *   are being tiled, with their laid-out density — e.g. `'shared'` for
   *   dense leaf fields (`medianChildArea < 100`), `'gaps'` elsewhere.
   */
  tiling?: Tiling | ((n: T, path: T[], depth: number, ctx: TilingCtx) => Tiling)
  /**
   * Shared-mode stroke width (CSS px) for cells at `depth` (0 = the viewed
   * node's children). Default: `max(1, 2 − depth)` — a 2px stroke at the top
   * level (the same width as the `gaps` gutter, so shared tiling never reads
   * *wider* than gaps) and a hairline everywhere below. Dust cells (<14px)
   * are capped at 1px.
   */
  borderWidth?: (depth: number, ctx: CellDims) => number
  /**
   * Shared-tiling only: emphasize shallow (top-level) boundaries over deep ones
   * so the tree's coarse structure reads at a glance — the fix for "top-level
   * edges are hard to see". Multiplies each cell's stroke width by
   * `1 + edgeEmphasis · max(0, 2 − depth)`, so depth-0 edges get `1 + 2·e`,
   * depth-1 `1 + e`, and depth-2+ are unchanged. Composes with `borderWidth`
   * (which sets the base per-depth width) and `edgeContrast` (the stroke color).
   * `0` = uniform (current behavior). Try `~0.75`–`1.5` for a clear step.
   * Default: 0.
   */
  edgeEmphasis?: number
  /**
   * In shared tiling, default each cell's half-stroke to a luminance-contrast
   * color derived from its own face (dark stroke on light cells, light on
   * dark) instead of the neutral `--dt-treemap-edge` gutter — so borders read
   * on any palette, including grey-on-grey fields. Default: true. A per-cell
   * `CellStyle.edge` from `colorForCell` still wins; a container cell whose
   * face is an unparseable `var()` falls back to the gutter. Set `false` to
   * keep the flat neutral gutter for every cell.
   */
  edgeContrast?: boolean
  /**
   * Render the default synthetic fold tile (the `(+n)` "(other)" stand-in) as
   * a canvas cross-hatch whose rules tighten toward the lower-right and whose
   * density scales with the folded count — reading as "dust" distinct from a
   * real cell — instead of a flat grey block. Hovering the tile maps the
   * cursor to the specific folded child under it (a squarify of the folded
   * items), so the tail stays interrogable without one DOM node per item.
   * Only applies to the built-in fold (no `mergeSmall`). Default: true.
   */
  dustTexture?: boolean
  /**
   * Hierarchical default coloring (no effect when `colorForCell` is set): each
   * top-level dir keeps its categorical "macro" hue, its descendants get a
   * per-L2-subtree "micro" variation of it (so sibling subtrees read as
   * related-but-distinct), and container cells are tinted a recessed shade of
   * their hue instead of a flat grey — so directory headers carry color. Off by
   * default (the neutral-container look is unchanged); opt in per consumer.
   */
  nestedHues?: boolean
  /**
   * Render a "detail" slider in the chrome bar that scales the fold thresholds
   * live (`minCellArea` and `minCellSide`), so a viewer can trade legibility
   * against completeness without a code change — drag toward *fine* to split
   * the dust back into cells, toward *coarse* to fold more away. Seeds at the
   * given thresholds (multiplier 1). Requires `chrome`. Default: false.
   */
  foldControl?: boolean
  /**
   * Lay each level with {@link squarifyRemainder} instead of plain squarify:
   * when one child dominates, the rest are given their own legible side-by-side
   * band instead of the tall skinny slivers a plain squarify squeezes them
   * into. Trades exact area-proportionality (the tail draws a little larger,
   * the dominant a little smaller) for hoverable, labelable tail cells — the
   * alternative to folding the tail into one `(+n)` dust tile. Falls back to a
   * plain squarify at any level with no cramped tail. Default: false.
   *
   * The sliver threshold is `minCellSide` (or 7 when folding is off); pass a
   * number to set the tail band's minimum fraction of the long axis (default
   * 0.14) — raise it if the tail still reads thin.
   */
  remainderTail?: boolean | number
  /**
   * Which renderer draws the map body. `'dom'` (default) is the mature
   * absolutely-positioned-`<div>` renderer with full feature parity. `'canvas'`
   * paints the entire map to a single `<canvas>` — one paint loop, no DOM node
   * per cell, for the 1e3–1e6-cell maps the DOM renderer bogs down on — and
   * hit-tests the retained layout, routing hits into the *same* drill/tooltip
   * handlers. Every surrounding affordance (crumbs, fold slider, drill, pinned
   * tips, lazy-load) is shared. Canvas parity is still filling in: segments
   * (makeup stripes), chain-label text, `cellHref` anchors, and the in-map
   * loading/error overlay are DOM-only for now (the overlay still renders atop
   * the canvas). Default: `'dom'`.
   */
  renderer?: 'dom' | 'canvas'
  /**
   * Canvas renderer only: build a thin DOM overlay of focusable
   * anchors/buttons over the largest cells, restoring the keyboard focus,
   * screen-reader labels, Vimium hints, `cellHref` links, and crawlability a
   * single `<canvas>` can't provide. The overlay is transparent and
   * `pointer-events:none` — the canvas handles every mouse interaction; the
   * overlay exists for keyboard/AT/crawlers, so focusing a cell scrubs
   * (`onCellHover`) and Enter/activate routes through the same drill/click path
   * as a mouse click. Bounded by `a11yMaxCells`/`a11yMinSide` so the node count
   * stays a fraction of the DOM renderer's. Ignored by the DOM renderer (whose
   * cells are already real elements). Default: true.
   */
  a11yLinks?: boolean
  /**
   * Canvas a11y overlay: cap on the number of overlay elements, largest cells
   * first — the knob that keeps a huge map's overlay bounded. Default: 400.
   */
  a11yMaxCells?: number
  /**
   * Canvas a11y overlay: only cells whose shorter side is ≥ this many px get an
   * overlay element (0 = every labeled/container cell up to the cap). Raise it
   * to mirror only the comfortably-clickable cells. Default: 0.
   */
  a11yMinSide?: number
  /**
   * Stroke, once, the outer perimeter of the union of rendered cells sharing a
   * consumer-supplied group key — so a run of adjacent same-mark siblings reads
   * as one bordered region instead of a lattice of doubled cell frames (spec
   * `specs/treemap-mark-union-outlines.md`). An overlay above the fills, below
   * tooltips, `pointer-events:none`; redrawn with layout. Off when unset.
   */
  outlineGroups?: OutlineGroups<T>
}

export type Tiling = 'gaps' | 'shared'

/** Context for the `tiling` callback: the parent's canvas and its children's layout. */
export interface TilingCtx extends CellDims {
  nChildren: number
  /** median laid-out child area in px² — the density cue */
  medianChildArea: number
}

export interface CellStyle {
  /** background CSS color/gradient */
  bg?: string
  /** ink color for the label */
  ink?: string
  /** repeating-gradient overlay (e.g. the class-lens hatch marin uses) */
  hatch?: string
  /**
   * Shared-tiling stroke for THIS cell, overriding `--dt-treemap-edge`. Each
   * cell paints its own half of every boundary it shares, so neighbours may
   * choose different colors — each half then contrasts with the face it
   * borders (one stroke color can't serve a bright cell and a dark one).
   * `CellCtx.fade` gives the background opacity applied at this depth, so a
   * consumer can compute the cell's true composited luminance.
   */
  edge?: string
  /** opacity multiplier (0–1). Combined with the built-in depth fade; applies
   * to the cell's background layer, never its label ink. */
  opacity?: number
  /**
   * Proportional makeup stripes for a leaf-rendered cell (no nested tiles at
   * the current depth — folds and max-depth leaves): instead of one dominant
   * color, the cell renders these as inset slices along its longer axis,
   * proportional to `frac`. The inset frame (painted `bg`) plus the shared
   * outer border distinguish a composition breakdown from real child tiles.
   * Ignored when fewer than 2 segments, when the cell renders children, or
   * when the cell is too small to read.
   */
  segments?: { color: string; frac: number }[]
  /**
   * An emphasis ring around this cell — for brushing / selection highlights.
   * Unlike `edge` (the shared-mode gutter half-stroke, which `gaps` mode
   * ignores), `ring` is honored in BOTH tiling modes and is painted as a
   * box-shadow, so it never affects layout. `width` in px; `color` any CSS
   * color; `inset` draws it inside the cell box (default true) rather than
   * outside. It follows the cell's corner radius and composites over whatever
   * structural `edge`/gutter the cell already has. A bare string is shorthand
   * for `{ color }` at the default width.
   */
  ring?: string | { color: string; width?: number; inset?: boolean }
}

export interface CellDims {
  w: number
  h: number
}

/** Render-time context passed to `colorForCell`. */
export interface CellCtx extends CellDims {
  /** Whether this cell renders nested child tiles at the current size. */
  hasKids: boolean
  /** Background opacity applied at this depth (the depth fade), so consumers
   * can compute what their color actually composites to on screen. */
  fade: number
  /** With `collapseChains`, how many single-child levels this cell collapsed
   * beyond its first (0 = not a chain): `path` ends at the deepest node, so
   * the cell's own top node is `path[path.length - 1 - chain]`. Passed to
   * `renderCellExtra` only. */
  chain?: number
}

const DEFAULT_SLOTS = DEFAULT_PALETTE

const defaultFormat = (n: number) => n.toLocaleString('en-US')

const defaultBorderWidth = (depth: number) => Math.max(1, 2 - depth)

const medianArea = (rs: { w: number; h: number }[]): number => {
  if (!rs.length) return 0
  const a = rs.map(r => r.w * r.h).sort((x, y) => x - y)
  return a[Math.floor(a.length / 2)]
}

const defaultId = <T,>(_n: T, path: T[], getLabel: (n: T) => string): string =>
  path.map(getLabel).join('/')

interface TipState<T> {
  x: number
  y: number
  key: string
  node: T
  path: T[]
}

/** Centered overlay for the lazy-load loading/error states. */
const STATUS_STYLE: CSSProperties = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 2,
  textAlign: 'center',
  padding: 12,
  color: 'var(--dt-treemap-ink, #d0d0d8)',
  background: 'var(--dt-treemap-status-bg, rgba(0,0,0,0.35))',
  pointerEvents: 'auto',
}

export function Treemap<T>({
  root,
  initialPath,
  path: pathProp,
  getSize,
  getChildren,
  hasChildren,
  loadChildren,
  onChildrenLoaded,
  renderLoading,
  renderLoadError,
  getLabel,
  getId,
  formatSize = defaultFormat,
  collapseChains = false,
  colorForCell,
  lens,
  renderCellExtra,
  renderTooltip,
  tipMode = 'float',
  renderTipDefault,
  renderRollup,
  renderLegend,
  renderCrumbSuffix,
  renderFooter,
  renderCellSubtitle,
  onCellClick,
  cellHref,
  onPathChange,
  onCellHover,
  minCellArea = 16,
  minCellSide = 7,
  mergeSmall,
  fullscreen = true,
  exportable,
  onExport,
  renderTip,
  chrome = true,
  showLabels = true,
  sizeAlign = 'left',
  inlineSizeMinWidth = 90,
  className,
  mapStyle,
  depthFade = 0.82,
  rootFade = 0.92,
  fadeFloor = 0.75,
  tiling = 'gaps',
  borderWidth = defaultBorderWidth,
  edgeEmphasis = 0,
  edgeContrast = true,
  dustTexture = true,
  nestedHues = false,
  foldControl = false,
  remainderTail = false,
  renderer = 'dom',
  a11yLinks = true,
  a11yMaxCells = 400,
  a11yMinSide = 0,
  outlineGroups,
}: TreemapProps<T>) {
  // Live fold-threshold multiplier driven by the optional "detail" slider:
  // >1 folds more (coarser), <1 folds less (finer). Scales area linearly and
  // the short-side threshold by its square root (side ~ √area).
  const [foldMul, setFoldMul] = useState(1)
  const effMinCellArea = minCellArea == null ? null : minCellArea * foldMul
  const effMinCellSide = minCellSide == null ? null : minCellSide * Math.sqrt(foldMul)
  const [pathState, setPathState] = useState<T[]>(initialPath?.[0] === root ? initialPath : [root])
  const controlled = pathProp !== undefined
  const path = controlled && pathProp[0] === root ? pathProp : pathState
  const [tip, setTip] = useState<TipState<T> | null>(null)
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 })
  const mapRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  // The live map `<canvas>` (canvas renderer only), so image export can read
  // its pixels directly (`exportable`).
  const canvasElRef = useRef<HTMLCanvasElement | null>(null)
  // Transient "copied ✓ / saved ✓" confirmation on the export buttons.
  const [flash, setFlash] = useState<ExportKind | null>(null)
  const flashTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => { if (flashTimer.current) clearTimeout(flashTimer.current) }, [])
  // Grace timer so the hover tip survives the cell→tip gap: leaving the map
  // schedules a clear, entering the tip cancels it. Lets you move into the tip
  // and use its controls/links without pinning (the tip is anchored, not
  // mouse-following, so it stays put while you reach for it).
  const tipClear = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  // Last cell key reported through `onCellHover`, so it fires once per change.
  const hoverKeyRef = useRef<string | null>(null)
  const cancelTipClear = () => {
    if (tipClear.current) { clearTimeout(tipClear.current); tipClear.current = undefined }
  }

  const pin = useHoverPin<string>({ excludeRefs: [tipRef] })
  const [pinnedTip, setPinnedTip] = useState<TipState<T> | null>(null)
  useEffect(() => {
    if (pin.pinned === null) setPinnedTip(null)
  }, [pin.pinned])

  const node = path[path.length - 1]

  // Every drill/crumb/Backspace gesture routes through here: controlled mode
  // only reports (the consumer renders the new `path` prop back); uncontrolled
  // keeps its own state and reports from the effect below, so the mount and
  // root-reset paths — which no gesture produces — report too.
  const go = useCallback(
    (p: T[]) => {
      if (!controlled) setPathState(p)
      setTip(null)
      if (controlled) onPathChange?.(p)
    },
    [controlled, onPathChange],
  )

  // Reset drill path when root changes (respecting `initialPath` when it
  // belongs to the new root). Controlled mode skips this — the consumer's
  // `path` prop is recomputed against the new root by the consumer. Skipped on
  // mount too: `useState` already seeded `path`, and re-seeding it here would
  // report a second, identical `[root]` through the effect below.
  const mountedRoot = useRef(root)
  useEffect(() => {
    if (controlled || mountedRoot.current === root) return
    mountedRoot.current = root
    setPathState(initialPath?.[0] === root ? initialPath : [root])
    setTip(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- initialPath applies per-root, not on its own changes
  }, [root])

  // Report the path this component owns: mount (incl. `initialPath`), drills,
  // and the root-change reset. Controlled mode reports from `go` instead, where
  // no local state changes and this effect would never fire.
  useEffect(() => {
    if (!controlled) onPathChange?.(path)
  }, [controlled, path, onPathChange])

  // Track container size: measure synchronously, then observe for changes.
  // The initial ResizeObserver delivery is not guaranteed to arrive — in a
  // hidden/background tab it never did, leaving a correctly-sized container
  // rendering zero cells until something resized it.
  useLayoutEffect(() => {
    const el = mapRef.current
    if (!el) return
    setSize({ w: el.clientWidth, h: el.clientHeight })
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Backspace/Escape pops the drill stack — unless another listener already
  // consumed the key (a hotkey layer clearing a selection, closing a modal…).
  // Listens on `window`, the end of the bubbling path, so a hotkey layer on
  // `document` or `window` gets to `preventDefault` first. (mgu `gcs`.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if ((e.key === 'Backspace' || e.key === 'Escape') && path.length > 1) {
        go(path.slice(0, -1))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [path, go])

  const idFor = useCallback(
    (n: T, p: T[]) => (getId ? getId(n, p) : defaultId(n, p, getLabel)),
    [getId, getLabel],
  )

  // Lazily-fetched children, keyed by node id. Dropped when `root` changes,
  // since a new root is a different tree (a different scan, in disk-tree's
  // case) and its ids mean nothing here.
  const [fetched, setFetched] = useState<Map<string, T[]>>(() => new Map())
  const [pending, setPending] = useState<string | null>(null)
  const [failed, setFailed] = useState<{ key: string; error: Error } | null>(null)
  const [retries, setRetries] = useState(0)
  useEffect(() => {
    setFetched(new Map())
    setPending(null)
    setFailed(null)
  }, [root])

  const childrenOf = useCallback(
    (n: T, p: T[]): T[] | undefined => getChildren(n) ?? fetched.get(idFor(n, p)),
    [getChildren, fetched, idFor],
  )
  /** Drillable: has children in hand, or says it has some we can fetch. */
  const expandable = useCallback(
    (n: T, p: T[]): boolean =>
      (childrenOf(n, p)?.length ?? 0) > 0 || (!!loadChildren && !!hasChildren?.(n)),
    [childrenOf, loadChildren, hasChildren],
  )

  // One fetch per drill: only the *viewed* node loads, never the cells it
  // renders. Nested tiles come from the depth the server already returned.
  const viewKey = idFor(node, path)
  const viewNeedsLoad =
    !!loadChildren && !getChildren(node)?.length && !fetched.has(viewKey) && !!hasChildren?.(node)
  useEffect(() => {
    if (!viewNeedsLoad || !loadChildren) return
    let live = true
    setPending(viewKey)
    setFailed(null)
    loadChildren(node, path).then(
      kids => {
        if (!live) return
        // Cache even a superseded load — the data is valid, and it spares a
        // refetch if the user drills back in.
        setFetched(m => (m.has(viewKey) ? m : new Map(m).set(viewKey, kids)))
        setPending(p => (p === viewKey ? null : p))
        onChildrenLoaded?.(node, path, kids)
      },
      (e: unknown) => {
        if (!live) return
        setPending(p => (p === viewKey ? null : p))
        setFailed({ key: viewKey, error: e instanceof Error ? e : new Error(String(e)) })
      },
    )
    return () => { live = false }
    // `node`/`path` are pinned by `viewKey`; `retries` re-runs a failed load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey, viewNeedsLoad, retries])

  const retry = useCallback(() => {
    setFailed(null)
    setRetries(r => r + 1)
  }, [])

  // Categorical color slots by top-level index (used when no colorForCell is given).
  const topLevelSlot = useMemo(() => {
    const kids = childrenOf(root, [root]) ?? []
    return new Map(kids.map((k, i) => [getLabel(k), slotColor(i, DEFAULT_SLOTS)]))
  }, [root, childrenOf, getLabel])

  // Build a folded stand-in from a set of small/thin items: consumer
  // `mergeSmall` builds a first-class T; the default builds a synthetic
  // FoldedNode (flattening any nested folds so `.count` stays accurate).
  const mergeItems = useCallback(
    (small: (T | FoldedNode<T>)[]): T | FoldedNode<T> => {
      if (mergeSmall) return mergeSmall(small as T[])
      const flat: T[] = []
      let sum = 0
      for (const s of small) {
        if (isFolded(s)) {
          flat.push(...s.children)
          sum += s.size
        } else {
          flat.push(s)
          sum += getSize(s)
        }
      }
      return { __folded: true, count: flat.length, size: sum, children: flat }
    },
    [getSize, mergeSmall],
  )

  // Fold small-area items at any level, before layout.
  const fold = useCallback(
    (raw: (T | FoldedNode<T>)[], w: number, h: number): (T | FoldedNode<T>)[] => {
      if (effMinCellArea == null) return raw
      const sz = (it: T | FoldedNode<T>) => (isFolded(it) ? it.size : getSize(it))
      return foldSmall<T | FoldedNode<T>>(raw, w, h, sz, mergeItems, effMinCellArea)
    },
    [getSize, effMinCellArea, mergeItems],
  )

  // Foldable children of the currently-viewed node.
  const children = useMemo(
    () => fold((childrenOf(node, path) ?? []).slice(), size.w, size.h),
    [node, path, size, childrenOf, fold],
  )

  // One layout pass over already-folded items. `remainderTail` swaps plain
  // squarify for `squarifyRemainder` (dominated tail gets its own side-by-side
  // band instead of slivers) at every level; otherwise it's plain squarify,
  // and the top-level `foldThin` below stays exactly as it was.
  const layTiles = useCallback(
    (items: (T | FoldedNode<T>)[], x: number, y: number, w: number, h: number) => {
      const sz = (n: T | FoldedNode<T>) => (isFolded(n) ? n.size : getSize(n))
      if (remainderTail) {
        const frac = typeof remainderTail === 'number' ? remainderTail : 0.14
        return squarifyRemainder<T | FoldedNode<T>>(items, x, y, w, h, sz, effMinCellSide ?? 7, frac)
      }
      return squarify<T | FoldedNode<T>>(items, x, y, w, h, sz)
    },
    [getSize, remainderTail, effMinCellSide],
  )

  const rects = useMemo(
    () => {
      const laid = layTiles(children, 0, 0, size.w, size.h)
      // Area folding can't see a cell's short side, so a dominant sibling still
      // squeezes the rest into unhoverable slivers; fold those by geometry and
      // re-lay once (the merged tile lands in the remainder as a single cell).
      // `remainderTail` already widens the tail, so it skips this.
      if (!remainderTail && effMinCellSide != null) {
        const sz = (n: T | FoldedNode<T>) => (isFolded(n) ? n.size : getSize(n))
        const refolded = foldThin<T | FoldedNode<T>>(laid, effMinCellSide, mergeItems)
        if (refolded) return squarify<T | FoldedNode<T>>(refolded, 0, 0, size.w, size.h, sz)
      }
      return laid
    },
    [children, size, getSize, remainderTail, effMinCellSide, mergeItems, layTiles],
  )

  // Background opacity at a given nesting depth. Applied per-cell to the
  // `.dt-treemap-bg` layer (not the cell div), so ancestors' fades never
  // compound and label ink stays full-strength at every depth.
  const fadeAt = useCallback(
    (d: number) => Math.max(rootFade * depthFade ** d, fadeFloor),
    [rootFade, depthFade, fadeFloor],
  )

  // Tiling mode for a node's children (`depth` = the children's depth).
  const tilingFor = useCallback(
    (n: T, p: T[], depth: number, w: number, h: number, rs: { w: number; h: number }[]): Tiling =>
      typeof tiling === 'function'
        ? tiling(n, p, depth, { w, h, nChildren: rs.length, medianChildArea: medianArea(rs) })
        : tiling,
    [tiling],
  )
  const rootMode = tilingFor(node, path, 0, size.w, size.h, rects)

  // Canvas renderer: the placed-cell tree (geometry only) for the whole map,
  // laid once and reused for paint + hit-test. Only built in canvas mode.
  const placedCells = useMemo(() => {
    // The canvas renderer paints from these; the DOM renderer doesn't, but the
    // outline overlay needs the geometry either way — so build them whenever an
    // `outlineGroups` overlay is present, not only for the canvas renderer.
    if (renderer !== 'canvas' && !outlineGroups) return []
    const cfg: LayoutConfig<T> = {
      getSize, getLabel, childrenOf, showLabels, collapseChains, borderWidth, edgeEmphasis, fold, layTiles, tilingFor,
    }
    return layoutCells(rects, path, rootMode, cfg)
  }, [
    renderer, !!outlineGroups, rects, path, rootMode, getSize, getLabel, childrenOf,
    showLabels, collapseChains, borderWidth, edgeEmphasis, fold, layTiles, tilingFor,
  ])

  // Stable style bundle for the canvas paint. Memoized so the paint effect
  // isn't restarted by the component's own hover/tooltip re-renders — those
  // change none of these inputs (consumer props keep their identity across
  // internal state changes), so a hover never re-triggers a full repaint.
  const styleOpts = useMemo<StyleOpts<T>>(
    () => ({ colorForCell, lens, getLabel, topLevelSlot, defaultSlots: DEFAULT_SLOTS, dustTexture, edgeContrast, nestedHues, fadeAt }),
    [colorForCell, lens, getLabel, topLevelSlot, dustTexture, edgeContrast, nestedHues, fadeAt],
  )

  // Hit → action, shared by both renderers: a DOM cell's event and a canvas
  // pointer hit resolve to a `(node, path, key)` and route through here, so the
  // tooltip/pin/drill behavior is written once.
  const activateHover = (node: T, path: T[], key: string, x: number, y: number) => {
    cancelTipClear()
    pin.hover(key)
    // Outward hover signal (once per cell change, not per pixel).
    if (hoverKeyRef.current !== key) {
      hoverKeyRef.current = key
      onCellHover?.(node, path)
    }
    setTip(prev => (prev?.key === key ? prev : { x, y, key, node, path }))
  }
  // Fire `onCellHover(null)` once, when hover actually leaves all cells —
  // called alongside every `pin.hover(null)` (map leave, tip leave, canvas leave).
  const clearHover = () => {
    if (hoverKeyRef.current !== null) {
      hoverKeyRef.current = null
      onCellHover?.(null, [])
    }
  }
  // Stray-tip guard: a hover tip that outlived its cell (the pointer left via
  // a path no mouseleave covered — a re-laid tip under the cursor, the window
  // edge, a portal boundary) would otherwise sit there until the next cell
  // hover. Any pointer movement outside a cell, the tip, or the map clears it;
  // a pinned tip is the pin's business (outside click / Esc).
  useEffect(() => {
    if (!tip || pinnedTip) return
    const onMove = (e: MouseEvent) => {
      const t = e.target as Element | null
      if (t?.closest?.('.dt-treemap-cell, .dt-treemap-tip, .dt-treemap-map, .dt-treemap-canvas')) return
      cancelTipClear()
      pin.hover(null)
      clearHover()
      setTip(null)
    }
    document.addEventListener('mousemove', onMove)
    return () => document.removeEventListener('mousemove', onMove)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!tip, !!pinnedTip])
  const activatePin = (node: T, path: T[], key: string, x: number, y: number) => {
    // Reuse the hover tip's anchor for the same cell, so pinning doesn't jump
    // the tooltip from the cell to the click point.
    const ax = tip?.key === key ? tip.x : x
    const ay = tip?.key === key ? tip.y : y
    pin.togglePin(key)
    setPinnedTip(p => (p?.key === key ? null : { x: ax, y: ay, key, node, path }))
  }
  const activateClick = (
    node: T, path: T[], key: string, drillable: boolean, x: number, y: number, e: React.MouseEvent,
  ) => {
    if (onCellClick && onCellClick(node, path, e)) return
    // A branch drills; ⌥-click pins it instead (its tip carries the per-cell
    // controls), so a deep cell can be acted on without drilling. (mgu `gcs`.)
    if (drillable && !e.altKey) { pin.clearPin(); go(path) }
    else activatePin(node, path, key, x, y)
  }

  const cell = (
    kid0: T | FoldedNode<T>,
    kidPath0: T[],
    r: { x: number; y: number; w: number; h: number },
    depth: number,
    mode: Tiling,
  ): ReactNode => {
    const folded = isFolded(kid0)
    // Single-child wrapper chains render as ONE cell labeled `a/…/z`: each
    // wrapper level otherwise costs a title strip + gutter, so a deep
    // single-child spine eats area and reads as noise (five nested boxes
    // all holding the same bytes). The cell *is* the deepest node — its
    // children, drill target, and id — with the chain recorded in the path,
    // so crumbs and tooltips still show every level.
    const chained = collapseChains && !folded
      ? (() => {
          let cur = kid0 as T
          let p = kidPath0
          const labels = [getLabel(cur)]
          for (;;) {
            const only = childrenOf(cur, p)
            if (!only || only.length !== 1) break
            cur = only[0]
            p = [...p, cur]
            labels.push(getLabel(cur))
          }
          return labels.length > 1 ? { node: cur, path: p, labels } : null
        })()
      : null
    const kid: T | FoldedNode<T> = chained ? chained.node : kid0
    const kidPath = chained ? chained.path : kidPath0
    const chainLabels = chained?.labels ?? null
    const kidSize = isFolded(kid) ? kid.size : getSize(kid)
    const kidLabel = folded
      ? `(+${(kid as FoldedNode<T>).count})`
      : chainLabels
        ? (chainLabels.length > 3 ? `${chainLabels[0]}/…/${chainLabels[chainLabels.length - 1]}` : chainLabels.join('/'))
        : getLabel(kid as T)
    const kidChildren = folded ? undefined : childrenOf(kid as T, kidPath)
    // Drillable even with nothing in hand, when a loader can go get it.
    const kidDrillable = !folded && expandable(kid as T, kidPath)
    const showLbl = showLabels && r.w > 36 && r.h > 13
    const dust = Math.min(r.w, r.h) < 14
    const shared = mode === 'shared'
    // This cell's own stroke (shared mode): half of it is drawn inside this
    // cell as an inset ring, the neighbor draws the other half.
    const bw = shared ? Math.min(borderWidth(depth, { w: r.w, h: r.h }) * edgeEmphFactor(depth, edgeEmphasis), dust ? 1 : Infinity) : 0
    const edge = bw / 2
    // Built-in adaptive half-stroke, computed once the cell's face is resolved
    // (below). Filled after `style` is known.
    let builtinEdge: string | null = null
    // Children canvas. Gaps mode pads 3px inside the cell; shared mode fills
    // to the cell's own half-stroke so the children's outer strokes meet it.
    // Their tiling mode is decided from a first layout's density, and the
    // layout redone at the mode's dims when they differ.
    // (Sized from the cell's *box* — in gaps mode that's the rect minus the
    // 2px gutter — so children never overrun the clipped cell.)
    const boxW = shared ? r.w : r.w - (dust ? 1 : 2)
    const boxH = shared ? r.h : r.h - (dust ? 1 : 2)
    let kw = boxW - 4
    let kh = boxH - (showLbl ? 21 : 4)
    let kidsMode: Tiling = 'gaps'
    let kids: ReturnType<typeof squarify<T | FoldedNode<T>>> = []
    if (kidChildren && kidChildren.length > 0 && r.w > 90 && r.h > 44) {
      const lay = (w: number, h: number) =>
        layTiles(fold(kidChildren.slice(), w, h), 0, 0, w, h)
      kids = lay(kw, kh)
      kidsMode = tilingFor(kid as T, kidPath, depth + 1, r.w, r.h, kids)
      if (kidsMode === 'shared') {
        kw = boxW - 2 * edge
        kh = boxH - (showLbl ? 20 + edge : 2 * edge)
        kids = lay(kw, kh)
      }
    }

    // Cell color falls through: consumer override → default categorical.
    // A consumer's `colorForCell` may return null/undefined to defer, so we
    // can't just conditionally *call* it; we call it and then fall through
    // if the result is nullish. This lets a caller style only the cells it
    // cares about (e.g. synthetic placeholders) and inherit defaults for
    // the rest.
    const explicit = folded
      ? null
      : colorForCell?.(kid as T, kidPath, depth, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0 })
    let style: CellStyle
    if (explicit) {
      style = explicit
    } else if (folded) {
      // Dust texture wants a faint ground so the hatch reads on top; the flat
      // fallback keeps the old solid block.
      style = dustTexture
        ? { bg: 'var(--dt-treemap-folded-ground, rgba(120, 120, 135, 0.12))', ink: 'var(--dt-treemap-folded-ink, #d0d0d8)' }
        : { bg: 'var(--dt-treemap-folded, #4a4a52)', ink: 'var(--dt-treemap-folded-ink, #d0d0d8)' }
    } else {
      style = categoricalStyle(kidPath, kids.length > 0, getLabel, topLevelSlot, DEFAULT_SLOTS, nestedHues)
    }
    if (lens && !folded) {
      style = lens(kid as T, kidPath, depth, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0 }, style) ?? style
    }
    // Adaptive edge default: only in shared mode, only when the consumer didn't
    // pin one, and only for a parseable face (container `var()` faces fall
    // through to the neutral gutter, keeping their thin-gutter look).
    if (shared && edgeContrast && !style.edge) {
      builtinEdge = contrastEdge(style.bg, fadeAt(depth))
    }
    const ring = resolveRing(style.ring)

    const cellKey = folded
      ? `__folded_${depth}_${r.x}_${r.y}`
      : idFor(kid as T, kidPath)

    // Position→child hit map for the dust tile: a squarify of the folded items
    // over the cell box, so hovering/clicking the hatch resolves to a specific
    // folded item. Guarded by item count so a very long tail doesn't re-lay on
    // every render; larger tails stay drawn but non-interrogable.
    const foldedNode = folded ? (kid as FoldedNode<T>) : null
    const dustHits = foldedNode && dustTexture && foldedNode.children.length > 1
      && Math.min(r.w, r.h) >= 10 && foldedNode.children.length <= 4000
      ? squarify<T>(foldedNode.children, 0, 0, boxW, boxH, getSize)
      : null
    /** Which folded item sits under a pointer event over the dust tile. */
    const dustHitAt = (e: React.MouseEvent): { it: T; path: T[]; key: string } | null => {
      if (!dustHits) return null
      const box = (e.currentTarget as HTMLElement).getBoundingClientRect()
      const lx = e.clientX - box.left
      const ly = e.clientY - box.top
      const hit = dustHits.find(rc => lx >= rc.x && lx < rc.x + rc.w && ly >= rc.y && ly < rc.y + rc.h)
      if (!hit) return null
      const p = [...kidPath, hit.it]
      return { it: hit.it, path: p, key: idFor(hit.it, p) }
    }

    const showTip = (e: React.MouseEvent) => {
      e.stopPropagation()
      // Anchor to the cell's top-left (not the entry cursor), so the tip lands
      // in the same spot regardless of which edge the pointer came in from, and
      // so it's stable to move into (it doesn't chase the cursor). Matches the
      // canvas renderer.
      const b = (e.currentTarget as HTMLElement).getBoundingClientRect()
      if (folded) {
        const hit = dustHitAt(e)
        if (!hit) return
        activateHover(hit.it, hit.path, hit.key, b.left, b.top)
        return
      }
      activateHover(kid as T, kidPath, cellKey, b.left, b.top)
    }
    // Real-anchor cells (`cellHref`): only leaf-rendered ones — a cell with
    // nested tiles would nest <a>s, which HTML forbids. Modified/middle
    // clicks keep native behavior (new tab); plain clicks preventDefault and
    // flow through the normal handler so SPA routers can intercept.
    const href = cellHref && !folded && kids.length === 0 ? cellHref(kid as T, kidPath) : undefined
    const onClick = (e: React.MouseEvent) => {
      e.stopPropagation()
      if (folded) {
        const hit = dustHitAt(e)
        if (!hit) return
        activatePin(hit.it, hit.path, hit.key, e.clientX, e.clientY)
        return
      }
      if (href) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1) return
        e.preventDefault()
      }
      activateClick(kid as T, kidPath, cellKey, kidDrillable, e.clientX, e.clientY, e)
    }
    // `branch` / `chain` chrome (consumers hang inset rings / doubled edges
    // off these) only when the cell is big enough for that treatment to read
    // — a dense field of small drillable or chain-collapsed tiles must not
    // all grow dark inner rings. `branch` also applies whenever children
    // actually render (a container needs its edge at any size).
    const chromeOk = Math.min(r.w, r.h) >= 28

    const CellTag: 'a' | 'div' = href ? 'a' : 'div'
    return (
      <CellTag
        key={cellKey}
        {...(href && { href })}
        className={'dt-treemap-cell' + (kidDrillable && (kids.length > 0 || chromeOk) ? ' branch' : '') + (dust ? ' dust' : '') + (chainLabels && chromeOk ? ' chain' : '') + (shared ? ' shared' : '') + (cellKey === pinnedTip?.key ? ' pinned' : '')}
        style={{
          position: 'absolute',
          left: r.x,
          top: r.y,
          // Gaps: 2px gutter (1px for dust) around the squarify rect.
          // Shared: the exact rect — neighbors abut, the stroke is the gutter.
          width: Math.max(0, shared ? r.w : r.w - (dust ? 1 : 2)),
          height: Math.max(0, shared ? r.h : r.h - (dust ? 1 : 2)),
          // Opaque base under the faded paint layer: a cell's fade recedes
          // toward the container color, and ancestor bg never shows through
          // descendants — it surfaces only in title bars and gutters. Stays
          // the container color in both modes: the paint layer is translucent
          // (depth fade), so a tinted base would wash every cell toward it.
          background: 'var(--dt-treemap-container-bg, #202024)',
          // Shared: this cell's half of the shared stroke, painted in the ring
          // the inset paint layer leaves bare (an inset shadow under a
          // full-bleed paint layer would never show). Gaps: an outer ring in
          // the gutter, transparent by default so dark-palette consumers can
          // opt into brighter sibling separation via the var.
          // (boxShadow, not outline — :focus owns the outline.)
          // A consumer `ring` (brush/selection emphasis) stacks first (on top)
          // so it reads over the structural gutter, in either tiling mode; it
          // follows the cell's `borderRadius` automatically.
          boxShadow: [
            ring && `${ring.inset ? 'inset ' : ''}0 0 0 ${ring.width}px ${ring.color}`,
            shared
              ? `inset 0 0 0 ${edge}px ${style.edge ?? builtinEdge ?? 'var(--dt-treemap-edge, var(--dt-treemap-container-bg, #202024))'}`
              : '0 0 0 1px var(--dt-treemap-cell-border, transparent)',
          ].filter(Boolean).join(', '),
          // Anchors must not fall through to the page's link color when the
          // consumer sets no ink.
          color: style.ink ?? (href ? 'inherit' : undefined),
          borderRadius: shared ? 0 : dust ? 1.5 : 3,
          overflow: 'hidden',
          boxSizing: 'border-box',
          cursor: href || kidDrillable ? 'pointer' : 'default',
          ...(href && { textDecoration: 'none' }),
        }}
        tabIndex={folded ? -1 : 0}
        // Leaf cells hover their whole body; branch cells hover only their
        // title-bar label (below) — so sweeping across a branch's children
        // never dips into the parent's tooltip through the inter-child gaps.
        // Clearing lives on the `.dt-treemap-map` container, not per-cell, so
        // child→child is smooth (the tip persists across the gap instead of
        // blinking off).
        onMouseMove={kids.length > 0 ? undefined : showTip}
        onClick={onClick}
        onKeyDown={e => e.key === 'Enter' && onClick(e as unknown as React.MouseEvent)}
      >
        {/* All paint (bg, hatch, makeup stripes) lives on this layer so the
            depth fade (and any per-cell `style.opacity`) dims backgrounds
            only — label ink renders outside it, full-strength at every
            depth. */}
        <div
          className="dt-treemap-bg"
          style={{
            position: 'absolute',
            inset: shared ? edge : 0,
            background: style.bg,
            ...(style.hatch && { backgroundImage: style.hatch }),
            // Dashed frame marks the dust tile as not-a-real-cell.
            ...(foldedNode && dustTexture && Math.min(r.w, r.h) >= 8 && {
              border: '1px dashed var(--dt-treemap-folded-edge, rgba(150, 150, 165, 0.55))',
              boxSizing: 'border-box' as const,
            }),
            opacity: fadeAt(depth) * (style.opacity ?? 1),
            pointerEvents: 'none',
          }}
        >
        {/* Dust hatch: the folded "(other)" tail, drawn as rules that tighten
            toward the lower-right, density scaled by the folded count — a
            texture distinct from real cells. Hit-detection (above) maps a
            hover back to a specific folded item. */}
        {foldedNode && dustTexture && Math.min(r.w, r.h) >= 6 && (
          <DustHatch w={boxW} h={boxH} count={foldedNode.count} />
        )}
        {/* Makeup stripes: a leaf/fold cell with a mixed composition renders
            proportional inset slices (longer axis) instead of one dominant
            blob. The `bg` frame showing through the inset + the single outer
            border reads as "one blob, split by share" — visibly distinct from
            real child tiles (separate bordered cells with their own labels). */}
        {style.segments && style.segments.length > 1 && kids.length === 0 && !dust
          && Math.min(r.w, r.h) >= 18 && (() => {
            const inset = shared ? Math.max(1, bw) : 3
            const gap = 1
            const horiz = r.w >= r.h // slice along the longer axis
            const span = (horiz ? r.w : r.h) - 2 * inset - gap * (style.segments!.length - 1)
            if (span < style.segments!.length * 2) return null
            const total = style.segments!.reduce((s, x) => s + x.frac, 0) || 1
            let at = inset
            return style.segments!.map((s, i) => {
              const len = (s.frac / total) * span
              const rect = horiz
                ? { left: at, top: inset, width: len, height: Math.max(0, r.h - 2 * inset - 2) }
                : { left: inset, top: at, width: Math.max(0, r.w - 2 * inset - 2), height: len }
              at += len + gap
              return (
                <div key={i} style={{
                  position: 'absolute', ...rect, background: s.color,
                  borderRadius: 2, pointerEvents: 'none',
                }} />
              )
            })
          })()}
        </div>
        {showLbl && (
          <div
            className={'dt-treemap-lbl' + (r.w < 64 ? ' sm' : '')}
            style={{
              position: 'relative',
              zIndex: 1, // above makeup stripes (positioned siblings)
              padding: 'var(--dt-treemap-lbl-pad, 2px 4px)',
              fontSize: r.w < 64
                ? 'var(--dt-treemap-lbl-fs-sm, 0.72rem)'
                : 'var(--dt-treemap-lbl-fs, 0.85rem)',
              lineHeight: 1.2,
              // Flex, not one nowrap run: a long name ellipsizes while the
              // size span keeps its width, instead of pushing it out of view.
              display: 'flex',
              alignItems: 'baseline',
              gap: 6,
              overflow: 'hidden',
              // branch title bar is the parent's own hover target (leaf labels
              // stay pointer-events:none so the body handles them)
              pointerEvents: kids.length > 0 ? 'auto' : 'none',
            }}
            onMouseMove={kids.length > 0 ? showTip : undefined}
          >
            <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{kidLabel}</span>
            {/* Inline size only for branch title-bars and short leaves; a tall
                leaf drops it to a 2nd line (below) so the name gets the full
                first line and isn't crowded by the size. */}
            {(kids.length > 0 || r.h <= 34) && r.w > inlineSizeMinWidth && (
              <span className="sz" style={{ opacity: 0.75, whiteSpace: 'nowrap', flex: 'none', marginLeft: sizeAlign === 'right' ? 'auto' : undefined }}>
                {formatSize(kidSize)}
                {!folded && renderCellSubtitle && (
                  <span style={{ marginLeft: 4 }}>{renderCellSubtitle(kid as T, kidPath, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0 })}</span>
                )}
              </span>
            )}
          </div>
        )}
        {/* Size on a second line for any leaf tall enough to hold one — the
            first line then belongs entirely to the (possibly long) name. */}
        {showLbl && kids.length === 0 && r.h > 34 && (
          <div
            className="dt-treemap-lbl2"
            style={{
              position: 'relative',
              zIndex: 1,
              padding: '0 4px',
              fontSize: 'var(--dt-treemap-lbl-fs-sm, 0.72rem)',
              lineHeight: 1.2,
              opacity: 0.75,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              pointerEvents: 'none',
            }}
          >
            {formatSize(kidSize)}
            {!folded && renderCellSubtitle && (
              <span style={{ marginLeft: 4 }}>{renderCellSubtitle(kid as T, kidPath, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0 })}</span>
            )}
          </div>
        )}
        {!folded && renderCellExtra && renderCellExtra(kid as T, kidPath, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0, chain: chainLabels ? chainLabels.length - 1 : 0 })}
        {kids.length > 0 && kidChildren && (
          <div
            className="dt-treemap-inner"
            style={{
              position: 'absolute',
              inset: kidsMode === 'shared'
                ? `${showLbl ? 20 : edge}px ${edge}px ${edge}px ${edge}px`
                : `${showLbl ? 20 : 3}px 3px 3px 3px`,
            }}
          >
            {kids
              .filter(s => s.w >= 3 && s.h >= 3)
              .map(s => cell(s.it, isFolded(s.it) ? kidPath : [...kidPath, s.it as T], s, depth + 1, kidsMode))}
          </div>
        )}
      </CellTag>
    )
  }

  const goFullscreen = () => {
    const el = wrapRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen()
  }

  // Image export (`exportable`): read the live map canvas, optionally composite
  // the crumb text as a title, then copy or download the PNG. Canvas renderer
  // only (the buttons only render when a canvas is present).
  const exportOpts: ExportOptions<T> | null =
    exportable ? (exportable === true ? {} : exportable) : null
  const doExport = useCallback(
    async (kind: ExportKind) => {
      if (!exportOpts || !mapRef.current) return
      const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1
      // Base image: the live canvas when the canvas renderer is up; otherwise
      // (DOM renderer) render the same placed cells to an offscreen canvas with
      // the identical paint machinery — so export is renderer-independent and
      // costs nothing until a button is actually clicked.
      let src = canvasElRef.current
      if (!src) {
        if (size.w <= 0 || size.h <= 0) return
        const cfg: LayoutConfig<T> = {
          getSize, getLabel, childrenOf, showLabels, collapseChains, borderWidth, edgeEmphasis, fold, layTiles, tilingFor,
        }
        const cells = layoutCells(rects, path, rootMode, cfg)
        const off = document.createElement('canvas')
        renderMapToCanvas(
          off, cells, size.w, size.h,
          { styleOpts, getSize, getLabel, formatSize, sizeAlign, inlineSizeMinWidth, dustTexture },
          mapRef.current,
        )
        src = off
      }
      const cs = getComputedStyle(mapRef.current)
      const ink = cs.color || 'rgb(230, 230, 238)'
      const transparent = (c: string | undefined) => !c || c === 'transparent' || c === 'rgba(0, 0, 0, 0)'
      let bg: string | undefined = cs.backgroundColor
      if (transparent(bg) && typeof document !== 'undefined') bg = getComputedStyle(document.body).backgroundColor
      if (transparent(bg)) bg = `rgb(${CONTAINER_BG[0]}, ${CONTAINER_BG[1]}, ${CONTAINER_BG[2]})`
      const title = exportOpts.title ? `${path.map(getLabel).join('/')} — ${formatSize(getSize(node))}` : null
      const out = composeExport(src, { title, bg: bg!, ink, dpr })
      const blob = await canvasToPngBlob(out)
      const filename = exportOpts.filename?.({ node, path }) ?? defaultExportFilename(getLabel(node))
      let outcome: ExportKind = kind
      if (kind === 'copy') {
        const copied = await copyPng(blob)
        if (!copied) { downloadPng(blob, filename); outcome = 'download' } // Firefox/insecure → save.
      } else {
        downloadPng(blob, filename)
      }
      setFlash(outcome)
      if (flashTimer.current) clearTimeout(flashTimer.current)
      flashTimer.current = setTimeout(() => setFlash(null), 1600)
      onExport?.(blob, { kind: outcome })
    },
    [
      exportOpts, path, node, getLabel, getSize, formatSize, onExport, size, rects, rootMode,
      styleOpts, sizeAlign, inlineSizeMinWidth, dustTexture,
      childrenOf, showLabels, collapseChains, borderWidth, edgeEmphasis, fold, layTiles, tilingFor,
    ],
  )
  const showExport = !!exportOpts
  // Wrap a bar button with the consumer's tooltip (if any); else identity, and
  // the button keeps its native `title`.
  const withTip = (label: string, button: ReactNode): ReactNode => (renderTip ? renderTip(label, button) : button)
  const iconBtn: CSSProperties = {
    background: 'transparent', border: 'none', color: 'inherit', cursor: 'pointer',
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
    padding: 2, lineHeight: 0, fontSize: '1.05em',
  }

  const tipToShow = pinnedTip ?? tip
  // Clamp the tip to the viewport using its MEASURED size — consumers widen
  // it with CSS (max-width overrides), so a fixed guess overflows the edge.
  const [tipDims, setTipDims] = useState<{ w: number; h: number } | null>(null)
  useLayoutEffect(() => {
    const el = tipRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setTipDims(d => (d && Math.abs(d.w - r.width) < 1 && Math.abs(d.h - r.height) < 1 ? d : { w: r.width, h: r.height }))
  })
  const tipContent = tipToShow && renderTooltip
    ? renderTooltip(tipToShow.node, tipToShow.path)
    : tipToShow
      ? (
        <>
          <div style={{ fontWeight: 500 }}>{getLabel(tipToShow.node)}</div>
          <div style={{ opacity: 0.75, fontSize: '0.85em' }}>{formatSize(getSize(tipToShow.node))}</div>
        </>
      )
      : null

  return (
    <div
      className={'dt-treemap' + (className ? ` ${className}` : '')}
      ref={wrapRef}
      onKeyDown={showExport ? (e => {
        // ⌘/Ctrl+Shift+C copies the current view (when focus is inside the map).
        if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'c' || e.key === 'C')) {
          e.preventDefault()
          void doExport('copy')
        }
      }) : undefined}
      style={{ display: 'flex', flexDirection: 'column', width: '100%', height: '100%' }}
    >
      {chrome && <div
        className="dt-treemap-bar"
        style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '2px 6px', minHeight: 22 }}
      >
        <nav
          className="dt-treemap-crumbs"
          aria-label="Path"
          style={{ display: 'flex', gap: 3, alignItems: 'center', flexWrap: 'wrap', flex: 1, minWidth: 0 }}
        >
          {path.map((n, i) => (
            <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
              {i > 0 && <span style={{ opacity: 0.4 }}>/</span>}
              {i < path.length - 1 ? (
                <a
                  tabIndex={0}
                  role="link"
                  style={{ cursor: 'pointer', textDecoration: 'underline' }}
                  onClick={() => go(path.slice(0, i + 1))}
                >
                  {getLabel(n)}
                </a>
              ) : (
                <span style={{ fontWeight: 500 }}>{getLabel(n)}</span>
              )}
            </span>
          ))}
          <span style={{ opacity: 0.6, marginLeft: 6, whiteSpace: 'nowrap' }}>
            {renderCrumbSuffix ? renderCrumbSuffix(node, path) : <>— {formatSize(getSize(node))}</>}
          </span>
        </nav>
        {renderLegend && <div className="dt-treemap-legend">{renderLegend(node, path)}</div>}
        {foldControl && (minCellArea != null || minCellSide != null) && (
          <label
            className="dt-treemap-fold"
            title="Detail: fold fewer cells (fine) ↔ more (coarse)"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 4, whiteSpace: 'nowrap', fontSize: '0.8em', opacity: 0.75 }}
          >
            <span>detail</span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.02}
              // Right = finer (lower multiplier); left = coarser (higher).
              value={(2 - Math.log2(foldMul)) / 4}
              onChange={e => setFoldMul(2 ** (2 - 4 * +e.target.value))}
              style={{ width: 72 }}
            />
          </label>
        )}
        {showExport && (() => {
          const okStyle = { ...iconBtn, color: 'var(--dt-treemap-ok, #3fb950)' }
          const copyLabel = flash === 'copy' ? 'Copied ✓' : 'Copy PNG to clipboard (⌘/Ctrl+Shift+C)'
          const dlLabel = flash === 'download' ? 'Saved ✓' : 'Download PNG'
          return (
            <>
              {withTip(copyLabel, (
                <button
                  className="dt-treemap-export-copy"
                  onClick={() => void doExport('copy')}
                  title={renderTip ? undefined : copyLabel}
                  aria-label="Copy image to clipboard"
                  style={flash === 'copy' ? okStyle : iconBtn}
                >
                  {flash === 'copy' ? <CheckIcon /> : <CopyIcon />}
                </button>
              ))}
              {withTip(dlLabel, (
                <button
                  className="dt-treemap-export-dl"
                  onClick={() => void doExport('download')}
                  title={renderTip ? undefined : dlLabel}
                  aria-label="Download image"
                  style={flash === 'download' ? okStyle : iconBtn}
                >
                  {flash === 'download' ? <CheckIcon /> : <DownloadIcon />}
                </button>
              ))}
            </>
          )
        })()}
        {fullscreen && withTip('Toggle fullscreen', (
          <button
            className="dt-treemap-fs"
            onClick={goFullscreen}
            title={renderTip ? undefined : 'Toggle fullscreen'}
            aria-label="Toggle fullscreen"
            style={iconBtn}
          >
            <FullscreenIcon />
          </button>
        ))}
      </div>}
      {(() => {
        const r = renderRollup?.(node, path)
        return r ? <div className="dt-treemap-rollup">{r}</div> : null
      })()}
      <div
        className="dt-treemap-map"
        ref={mapRef}
        role="application"
        aria-label="Treemap"
        style={{ position: 'relative', flex: 1, minHeight: 0, ...mapStyle }}
        onMouseLeave={() => {
          // Don't clear immediately — give the pointer time to reach the tip
          // (cancelled by the tip's onMouseEnter).
          cancelTipClear()
          tipClear.current = setTimeout(() => { pin.hover(null); clearHover(); setTip(null) }, 180)
        }}
      >
        {renderer === 'canvas'
          ? (size.w > 0 && size.h > 0 && (
              <TreemapCanvas<T>
                cells={placedCells}
                width={size.w}
                height={size.h}
                styleOpts={styleOpts}
                getSize={getSize}
                getLabel={getLabel}
                formatSize={formatSize}
                sizeAlign={sizeAlign}
                idFor={idFor}
                expandable={expandable}
                dustTexture={dustTexture}
                cellHref={cellHref}
                inlineSizeMinWidth={inlineSizeMinWidth}
                a11yLinks={a11yLinks}
                a11yMaxCells={a11yMaxCells}
                a11yMinSide={a11yMinSide}
                pinnedKey={pinnedTip?.key ?? null}
                canvasRef={exportable ? canvasElRef : undefined}
                onHover={(hit: CanvasHit<T>, x, y) => activateHover(hit.node, hit.path, hit.key, x, y)}
                onClick={(hit: CanvasHit<T>, e) =>
                  hit.foldChild
                    ? activatePin(hit.node, hit.path, hit.key, e.clientX, e.clientY)
                    : activateClick(hit.node, hit.path, hit.key, hit.drillable, e.clientX, e.clientY, e)}
                onLeave={() => {
                  cancelTipClear()
                  tipClear.current = setTimeout(() => { pin.hover(null); clearHover(); setTip(null) }, 180)
                }}
              />
            ))
          : rects.filter(r => r.w >= 3 && r.h >= 3).map(r => cell(r.it, isFolded(r.it) ? path : [...path, r.it as T], r, 0, rootMode))}
        {outlineGroups && size.w > 0 && size.h > 0 && (
          <OutlineOverlay<T> cells={placedCells} width={size.w} height={size.h} groups={outlineGroups} />
        )}
        {failed?.key === viewKey ? (
          <div className="dt-treemap-status error" style={STATUS_STYLE}>
            {renderLoadError
              ? renderLoadError(node, path, failed.error, retry)
              : (
                <>
                  <div>Couldn’t load {getLabel(node)}: {failed.error.message}</div>
                  <button onClick={retry} style={{ marginTop: 6, cursor: 'pointer' }}>Retry</button>
                </>
              )}
          </div>
        ) : pending === viewKey ? (
          <div className="dt-treemap-status loading" style={STATUS_STYLE}>
            {renderLoading ? renderLoading(node, path) : 'Loading…'}
          </div>
        ) : null}
      </div>
      {(() => {
        const f = renderFooter?.(node, path)
        return f ? <div className="dt-treemap-footer">{f}</div> : null
      })()}
      {/* Docked tip: one panel below the footer that updates in place as the
          pointer moves through a lineage — no tip chasing the cursor or
          covering the cells and controls under it. Always rendered (a faint
          hint while empty) so the layout doesn't jump. A phone, with no hover,
          taps to pin into this same panel. */}
      {tipMode === 'dock' ? (() => {
        const defaultContent = renderTipDefault ? renderTipDefault(node, path) : null
        const body = tipContent ?? defaultContent
          ?? <span className="dt-treemap-tipdock-hint">Hover a cell for its details.</span>
        // `resting` = a real default card (root summary); `empty` = nothing at all.
        const cls = tipContent ? '' : defaultContent ? ' resting' : ' empty'
        return (
        <div
          ref={tipRef}
          className={'dt-treemap-tip dock' + (pinnedTip ? ' pinned' : '') + cls}
          onMouseEnter={cancelTipClear}
          onMouseLeave={() => { if (!pinnedTip) { pin.hover(null); clearHover(); setTip(null) } }}
          style={{ position: 'relative', width: '100%', pointerEvents: 'auto' }}
        >
          {tipContent && pinnedTip && (
            <button
              onClick={() => pin.clearPin()}
              title="Unpin (Esc)"
              className="dt-treemap-tip-x"
              style={{
                position: 'absolute', top: 2, right: 4,
                background: 'transparent', border: 'none', color: 'inherit', cursor: 'pointer',
                fontSize: '1em', opacity: 0.6, pointerEvents: 'auto',
              }}
            >
              ×
            </button>
          )}
          {body}
        </div>
        )
      })() : tipContent && tipToShow ? (
        <div
          ref={tipRef}
          className={'dt-treemap-tip' + (pinnedTip ? ' pinned' : '')}
          onMouseEnter={cancelTipClear}
          onMouseLeave={() => { if (!pinnedTip) { pin.hover(null); clearHover(); setTip(null) } }}
          style={{
            position: 'fixed',
            left: Math.max(4, Math.min(tipToShow.x + 14, (typeof window !== 'undefined' ? window.innerWidth : 1600) - (tipDims?.w ?? 320) - 8)),
            top: Math.max(4, Math.min(tipToShow.y + 14, (typeof window !== 'undefined' ? window.innerHeight : 1200) - (tipDims?.h ?? 80) - 8)),
            background: 'var(--dt-treemap-tip-bg, #1a1a1e)',
            color: 'var(--dt-treemap-tip-ink, #e6e6ea)',
            border: '1px solid var(--dt-treemap-tip-border, #333)',
            borderRadius: 4,
            padding: '6px 10px',
            fontSize: '0.85rem',
            zIndex: 1000,
            maxWidth: 320,
            // The tip anchors over a cell's top-left, so it sits on pixels you'd
            // click — to pin this cell, or (when a different cell is already
            // pinned and its tip overlaps) to pin that one. The container must
            // never eat those clicks; only its × button (below) takes pointer
            // events, so clicks fall through to the canvas/cell underneath.
            pointerEvents: 'none',
            boxShadow: '0 4px 10px rgba(0,0,0,0.35)',
          }}
        >
          {pinnedTip && (
            <button
              onClick={() => pin.clearPin()}
              title="Unpin (Esc)"
              style={{
                position: 'absolute', top: 2, right: 4,
                background: 'transparent', border: 'none', color: 'inherit', cursor: 'pointer',
                fontSize: '1em', opacity: 0.6, pointerEvents: 'auto',
              }}
            >
              ×
            </button>
          )}
          {tipContent}
        </div>
      ) : null}
    </div>
  )
}
