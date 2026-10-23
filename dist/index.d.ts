import * as react from 'react';
import { ReactNode, CSSProperties, RefObject } from 'react';
import { P as PlacedCell, C as CellText, a as CellStyle, T as Tiling, S as StyleOpts } from './cellStyle-DVNsfe4P.js';
export { F as FoldedNode, L as LayoutConfig, N as NAME_MIN_PX, R as ResolvedStyle, c as candidates, b as cellStroke, f as fitCandidate, d as flattenPlaced, e as foldedOf, h as hitTest, i as inlineRoom, g as isFolded, l as layoutCells, r as resolveCellStyle } from './cellStyle-DVNsfe4P.js';
export { Rect, foldSmall, foldThin, squarify, squarifyRemainder } from './squarify.js';
export { divergingColor, divergingInk } from './divergingColor.js';
export { parseColor } from './parseColor.js';

/**
 * Grouped-outline geometry: stroke the outer perimeter of the *union* of
 * rendered cells sharing a group key, once — so a run of adjacent same-mark
 * siblings reads as one bordered region, not a lattice of doubled cell frames
 * (spec `specs/treemap-mark-union-outlines.md`, asked for by mgu).
 *
 * The map's cells are axis-aligned, non-overlapping rects on one plane (a
 * tiling), so the union boundary is pure edge arithmetic — no polygon library.
 * A vertical line at `x` borders the group on its left where a group rect ends
 * at `x` and on its right where one begins at `x`; the boundary is exactly
 * where those two occupancies *disagree* (a rect on one side, none on the
 * other). Symmetric-difference of the two interval sets per line gives the
 * boundary segments and, from which side won, the inward normal (for insetting).
 */

interface OutlineGroups<T> {
    /** Group key for a cell, or `null` for none. Same key ⇒ same region. */
    key: (node: T, path: T[]) => string | null;
    /** Stroke color for a group. */
    color: (key: string) => string;
    /** Stroke width in CSS px (default 2). */
    width?: number;
    /** Stroke just inside the union (default) or centered on its boundary. */
    inset?: boolean;
    /**
     * Called after each paint with the keys that produced at least one segment
     * — what is actually on screen, so a consumer's key/legend can list only
     * the groups a viewer can see (a group whose cells all fold below the
     * pixel budget has no outline to explain). Keep it cheap and idempotent:
     * it runs on every relayout.
     */
    onDrawn?: (keys: string[]) => void;
}
interface Rect {
    x: number;
    y: number;
    w: number;
    h: number;
}
/** One boundary segment: endpoints plus the unit inward normal (toward the
 *  group's interior), which insetting offsets along. */
interface OutlineSeg {
    x1: number;
    y1: number;
    x2: number;
    y2: number;
    nx: number;
    ny: number;
}
/**
 * The boundary of the union of `rects` (assumed non-overlapping) as a set of
 * segments with inward normals. Vertical lines first, then horizontal.
 */
declare function unionOutline(rects: Rect[]): OutlineSeg[];
/**
 * Bucket placed cells into groups by key, keeping only the *outermost* cell for
 * each key on any path — a descendant with the same key as an ancestor is
 * already covered by the ancestor's rect (nesting), so including it would
 * overlap and break the union arithmetic. Folded/dust tiles whose node isn't a
 * real `T` are skipped. Insertion order follows first appearance (top-down).
 */
declare function groupRects<T>(cells: PlacedCell<T>[], key: (node: T, path: T[]) => string | null): Map<string, Rect[]>;
interface OutlinePath {
    key: string;
    color: string;
    segs: OutlineSeg[];
}
/** The stroked outlines for every group present in `cells`. */
declare function groupOutlines<T>(cells: PlacedCell<T>[], opts: OutlineGroups<T>): OutlinePath[];

/**
 * Export the current treemap view as a PNG — copy to the clipboard or download.
 *
 * The canvas renderer already draws the whole map (cells + labels) to one
 * `<canvas>` at `devicePixelRatio`, so the pixels a teammate would screenshot
 * already exist: we read them straight off that canvas — no DOM-to-image
 * dependency, crisp on retina without re-rendering. `composeExport` optionally
 * composites a **title line** (the crumb text — root path + total) above the
 * map on an offscreen canvas in the page's current theme colours; the base
 * export is the map alone (what the teammate clipped).
 *
 * Consumed by `<Treemap exportable>` (spec `specs/done/treemap-export-image.md`); the
 * helpers are standalone so a consumer can wire its own affordance.
 */
/** Whether an export was copied to the clipboard or saved as a file. */
type ExportKind = 'copy' | 'download';
/** Context handed to `ExportOptions.filename`. */
interface ExportContext<T> {
    /** The current view's root node (crumb tail). */
    node: T;
    /** Full ancestry from the tree root to `node`. */
    path: T[];
}
/** `Treemap`'s `exportable` prop, spelled out. `true` ≡ `{}` (map only). */
interface ExportOptions<T = unknown> {
    /** Output filename (a `.png` is appended when missing). Default:
     *  `<view-basename>-<YYYYMMDD-HHMM>.png`. */
    filename?: (ctx: ExportContext<T>) => string;
    /** Composite the crumb text (root path + total) as a title line above the
     *  map. Default: false (the bare map, what the teammate clipped). */
    title?: boolean;
}
/** Theme colours + scale for `composeExport`, read off the live map element. */
interface ComposeOptions {
    /** Title line, or null for the bare map (returns the source unchanged). */
    title: string | null;
    /** Title bar background (blended with the map ground). */
    bg: string;
    /** Title text colour (the map's inherited ink). */
    ink: string;
    /** Device pixel ratio the source canvas was drawn at. */
    dpr: number;
}
/**
 * The export canvas for `source`: the map alone when `title` is null, else a
 * new canvas with the title line composited above it (theme colours, a small
 * margin). The source is drawn 1:1 — it's already at `dpr`.
 */
declare function composeExport(source: HTMLCanvasElement, o: ComposeOptions): HTMLCanvasElement;
/** A canvas → PNG `Blob` (rejects if the browser can't encode one). */
declare function canvasToPngBlob(canvas: HTMLCanvasElement): Promise<Blob>;
/**
 * Copy `blob` to the clipboard as `image/png`. Returns false (rather than
 * throwing) when `ClipboardItem` / `clipboard.write` is unavailable — Firefox
 * behind a flag, or a non-secure context — so the caller can fall back to a
 * download. Needs a user gesture + secure context, both of which hold for a
 * button click on an HTTPS page.
 */
declare function copyPng(blob: Blob): Promise<boolean>;
/** Save `blob` as `filename` via a transient object-URL `<a download>`. */
declare function downloadPng(blob: Blob, filename: string): void;
/** `<view-basename>-<YYYYMMDD-HHMM>.png` — the default export filename. */
declare function defaultExportFilename(label: string, when?: Date): string;

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
interface TreemapProps<T> {
    root: T;
    /**
     * Start drilled to this path (must begin with `root`). Applied on mount and
     * whenever `root`'s identity changes — crumbs still show the full ancestry,
     * so a store with one meaningful top-level container can open inside it.
     */
    initialPath?: T[];
    /**
     * Controlled drill path (must begin with `root`). When given, the component
     * renders this path and reports every drill/crumb/Backspace gesture through
     * `onPathChange` instead of keeping its own state — so a consumer can put
     * the path in the URL, or command a drill from outside (a table row, a
     * search hit). Mutually exclusive with `initialPath`.
     */
    path?: T[];
    /** Extract this node's *own* aggregated size (bytes, count, whatever). */
    getSize: (n: T) => number;
    /** Extract children; return `undefined` or `[]` for leaves. */
    getChildren: (n: T) => T[] | undefined;
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
    hasChildren?: (n: T) => boolean;
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
    loadChildren?: (n: T, path: T[]) => Promise<T[]>;
    /** Called after `loadChildren` resolves, before the cells render. */
    onChildrenLoaded?: (n: T, path: T[], children: T[]) => void;
    /** Map-area content while the viewed node's children load. Default: "Loading…". */
    renderLoading?: (n: T, path: T[]) => ReactNode;
    /** Map-area content when a load rejects. Default: the message + a Retry button. */
    renderLoadError?: (n: T, path: T[], error: Error, retry: () => void) => ReactNode;
    /** Human label shown in the cell (and in breadcrumbs). */
    getLabel: (n: T) => string;
    /** Stable key for this node — defaults to the joined path label. */
    getId?: (n: T, path: T[]) => string;
    /** Format `getSize`'s return value for display. Default: raw number toLocaleString. */
    formatSize?: (n: number) => string;
    /** A cell's size text, overriding `formatSize(getSize(n))` (e.g. a signed change, while the area stays `getSize`):
     *  one string, or candidates longest first, each renderer drawing the first that fits the cell (`CellText`). */
    formatCell?: (n: T) => CellText;
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
    colorForCell?: (n: T, path: T[], depth: number, ctx: CellCtx) => CellStyle | null | undefined;
    /**
     * Post-resolution style transform, applied to whatever `colorForCell` or
     * the default palette produced — so lenses (e.g. the age fade in
     * `colors.ts`) *stack* on any color mode instead of replacing it. Return
     * null/undefined to leave the resolved style untouched. Not called for
     * synthetic folded tiles.
     */
    lens?: (n: T, path: T[], depth: number, ctx: CellCtx, style: CellStyle) => CellStyle | null | undefined;
    /**
     * Collapse single-child wrapper chains into one cell labeled `a/…/z` (the
     * cell is the deepest node; the chain is recorded in the drill path).
     * Off by default — chains carry real information in some trees.
     */
    collapseChains?: boolean;
    /** Let complete columns right of a branch's title fill its unused strip. Default: false. */
    reclaimTitleSpace?: boolean;
    /** Quiet one-pixel frames around rendered parent groups, above descendant fills. Off when unset. */
    branchOutline?: string;
    /** Optional extra content rendered inside the cell after the label. */
    renderCellExtra?: (n: T, path: T[], ctx: CellCtx) => ReactNode;
    /** Tooltip body; return null to suppress the tooltip. */
    renderTooltip?: (n: T, path: T[]) => ReactNode;
    /**
     * Tooltip placement. `'float'` (default) is a fixed tip that tracks the
     * pointer; `'dock'` renders one panel below the footer that updates in place
     * as the pointer moves through a lineage — no tip chasing the cursor or
     * covering cells/controls, and a phone (no hover) taps to pin into it. The
     * empty panel keeps a small footprint so the layout doesn't jump.
     */
    tipMode?: 'float' | 'dock';
    /** Dock mode only: a resting card shown in the docked tip panel when no cell
     *  is hovered (e.g. the current root's summary), instead of the empty hint. */
    renderTipDefault?: (n: T, path: T[]) => ReactNode;
    /** Extra row above the map (e.g. rollup / totals). */
    renderRollup?: (n: T, path: T[]) => ReactNode;
    /** Right side of the breadcrumbs bar. */
    renderLegend?: (n: T, path: T[]) => ReactNode;
    /**
     * Replaces the default `— <size>` suffix after the breadcrumbs (e.g. to
     * add object counts / $-estimates). Return null to render no suffix.
     */
    renderCrumbSuffix?: (n: T, path: T[]) => ReactNode;
    /** Row rendered below the map (e.g. a usage-hint footer). */
    renderFooter?: (n: T, path: T[]) => ReactNode;
    /**
     * Right side of the cell size line — e.g. "$1.2/mo". Rendered inline with
     * the size when the cell is big enough to fit a subtitle; `dims` is the
     * cell's box (plus `hasKids`/`fade`) so the consumer can size its content
     * to the room left after the label (the size span never shrinks, so an
     * over-long subtitle would crush the name) and tell branches from leaves —
     * `n`'s own children may be lazily loaded rather than in hand.
     */
    renderCellSubtitle?: (n: T, path: T[], ctx: CellCtx) => ReactNode;
    /**
     * Override the click handler. Return `true` to skip the built-in
     * drill/pin behavior (i.e. the consumer handled it).
     */
    onCellClick?: (n: T, path: T[], event: React.MouseEvent) => boolean | void;
    /**
     * Make leaf-rendered cells real anchors (`<a href>`): native cursor,
     * middle/cmd-click, link hints (Vimium), crawlability. Return `undefined`
     * for cells that shouldn't be links. Cells that render nested tiles stay
     * `<div>`s (anchors can't nest), so this suits shallow maps best. Plain
     * clicks are `preventDefault`ed and flow through `onCellClick`/drill/pin
     * as usual — an SPA router intercepts while the href keeps its native
     * affordances.
     */
    cellHref?: (n: T, path: T[]) => string | undefined;
    /** Called whenever the drill path changes (drill in, drill back). */
    onPathChange?: (path: T[]) => void;
    /**
     * Fired when the pointer enters a cell (with the node + its path from root)
     * and again with `null` when it leaves the map / all cells — the outward
     * mirror of the inward `lens` highlight, so a consumer can reflect the map's
     * hover into another view (e.g. file-tree's split listing lights the row for
     * the hovered tile). Fires once per cell change, not per pixel; `null` on
     * leave. Debounce/grace is the consumer's concern. No behavior change when
     * absent. `path` is the root→cell node array, same shape as `onPathChange`.
     */
    onCellHover?: (n: T | null, path: T[]) => void;
    /**
     * Fold small-area cells (below `minCellArea` in px²) into a synthetic
     * "…" tile. Pass `null` to disable folding.
     */
    minCellArea?: number | null;
    /**
     * Fold cells whose *short side* renders below `minCellSide` px into one
     * synthetic tile — catches the tall, skinny slivers a dominant sibling
     * squeezes out, which have enough area to escape `minCellArea` but are too
     * narrow to hover or label. Pass `null` to disable. Default: 7.
     */
    minCellSide?: number | null;
    /**
     * Build the folded stand-in as a *first-class* `T` (label, aggregated
     * size, and whatever the consumer's tooltip/colors need). When given, the
     * folded tile gets normal label/tooltip/click treatment; when omitted, a
     * synthetic gray `(+n)` tile with no tooltip is used.
     */
    mergeSmall?: (small: T[]) => T;
    /** Show the fullscreen toggle button. Default: true. */
    fullscreen?: boolean;
    /**
     * Add copy-PNG / download-PNG buttons to the bar (and `⌘/Ctrl+Shift+C` to
     * copy) that export the current view as an image. `true` ≡ `{}` (the bare
     * map); `{ title: true }` composites the crumb text above it. Canvas
     * renderer only — the export reads the map's `<canvas>` pixels directly (no
     * DOM-to-image dependency); ignored under the DOM renderer.
     */
    exportable?: boolean | ExportOptions<T>;
    /** Fired after each export, with the PNG blob and how it left (for a
     *  "copied ✓" toast / analytics; the copy/download itself is already done). */
    onExport?: (blob: Blob, ctx: {
        kind: ExportKind;
    }) => void;
    /**
     * Wrap a control-bar button (export copy/download, fullscreen) with your own
     * tooltip lib — MUI `<Tooltip>`, `@floating-ui/react`, Radix, … — instead of
     * the browser-native `title`. Given the button's label and element, return
     * the wrapped node. When set, the native `title` is suppressed so tips
     * aren't doubled. Omitted → native `title` (the core stays dependency-free).
     */
    renderTip?: (label: string, button: ReactNode) => ReactNode;
    /** Render the breadcrumbs/legend bar. Default: true. */
    chrome?: boolean;
    /** Render in-cell labels. Default: true. (`false` + `chrome={false}` ≈ a redacted/og render.) */
    showLabels?: boolean;
    /**
     * Where the inline size sits on a cell's first line (branch title bars and
     * short leaves — tall leaves always drop the size to a second line under the
     * name). `'left'` (default) sets it right after the name; `'right'` pushes
     * it to the cell's far edge. Both renderers honor it identically.
     */
    sizeAlign?: 'left' | 'right';
    /**
     * Minimum cell width (CSS px) at which a branch title-bar or short leaf
     * shows its size *inline* beside the name. Below it the name gets the whole
     * line (the size stays in the tooltip, and on a tall leaf's 2nd line).
     * Raise it when names matter more than sizes (long paths). Default: 90.
     */
    inlineSizeMinWidth?: number;
    /** Extra className on the outer wrapper. */
    className?: string;
    /** Style overrides on the map area. */
    mapStyle?: CSSProperties;
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
    depthFade?: number;
    /** Background opacity of the outermost (depth 0) cells. Default: 0.92. */
    rootFade?: number;
    /**
     * Floor on the depth fade, so deep backgrounds don't wash out entirely
     * (depth 4 at the defaults would otherwise paint at ~0.5). Default: 0.75.
     */
    fadeFloor?: number;
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
    tiling?: Tiling | ((n: T, path: T[], depth: number, ctx: TilingCtx) => Tiling);
    /**
     * Shared-mode stroke width (CSS px) for cells at `depth` (0 = the viewed
     * node's children). Default: `max(1, 2 − depth)` — a 2px stroke at the top
     * level (the same width as the `gaps` gutter, so shared tiling never reads
     * *wider* than gaps) and a hairline everywhere below. Dust cells (<14px)
     * are capped at 1px.
     */
    borderWidth?: (depth: number, ctx: CellDims) => number;
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
    edgeEmphasis?: number;
    /**
     * Shared tiling only: round every stroke to whole pixels and keep each one
     * ≥1px narrower than its parent's, so a parent's seam always reads wider
     * than the seams between its children (down to the 1px floor). Fractional
     * widths from `borderWidth` (e.g. a size cap) otherwise quantize to no
     * visible difference. Pair with a `borderWidth` that steps by level.
     * Default: false.
     */
    borderStep?: boolean;
    /**
     * In shared tiling, default each cell's half-stroke to a luminance-contrast
     * color derived from its own face (dark stroke on light cells, light on
     * dark) instead of the neutral `--dt-treemap-edge` gutter — so borders read
     * on any palette, including grey-on-grey fields. Default: true. A per-cell
     * `CellStyle.edge` from `colorForCell` still wins; a container cell whose
     * face is an unparseable `var()` falls back to the gutter. Set `false` to
     * keep the flat neutral gutter for every cell.
     */
    edgeContrast?: boolean;
    /**
     * Render the default synthetic fold tile (the `(+n)` "(other)" stand-in) as
     * a canvas cross-hatch whose rules tighten toward the lower-right and whose
     * density scales with the folded count — reading as "dust" distinct from a
     * real cell — instead of a flat grey block. Hovering the tile maps the
     * cursor to the specific folded child under it (a squarify of the folded
     * items), so the tail stays interrogable without one DOM node per item.
     * Only applies to the built-in fold (no `mergeSmall`). Default: true.
     */
    dustTexture?: boolean;
    /**
     * Hierarchical default coloring (no effect when `colorForCell` is set): each
     * top-level dir keeps its categorical "macro" hue, its descendants get a
     * per-L2-subtree "micro" variation of it (so sibling subtrees read as
     * related-but-distinct), and container cells are tinted a recessed shade of
     * their hue instead of a flat grey — so directory headers carry color. Off by
     * default (the neutral-container look is unchanged); opt in per consumer.
     */
    nestedHues?: boolean;
    /**
     * Render a "detail" slider in the chrome bar that scales the fold thresholds
     * live (`minCellArea` and `minCellSide`), so a viewer can trade legibility
     * against completeness without a code change — drag toward *fine* to split
     * the dust back into cells, toward *coarse* to fold more away. Seeds at the
     * given thresholds (multiplier 1). Requires `chrome`. Default: false.
     */
    foldControl?: boolean;
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
    remainderTail?: boolean | number;
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
    renderer?: 'dom' | 'canvas';
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
    a11yLinks?: boolean;
    /**
     * Canvas a11y overlay: cap on the number of overlay elements, largest cells
     * first — the knob that keeps a huge map's overlay bounded. Default: 400.
     */
    a11yMaxCells?: number;
    /**
     * Canvas a11y overlay: only cells whose shorter side is ≥ this many px get an
     * overlay element (0 = every labeled/container cell up to the cap). Raise it
     * to mirror only the comfortably-clickable cells. Default: 0.
     */
    a11yMinSide?: number;
    /**
     * Stroke, once, the outer perimeter of the union of rendered cells sharing a
     * consumer-supplied group key — so a run of adjacent same-mark siblings reads
     * as one bordered region instead of a lattice of doubled cell frames (spec
     * `specs/treemap-mark-union-outlines.md`). An overlay above the fills, below
     * tooltips, `pointer-events:none`; redrawn with layout. Off when unset.
     */
    outlineGroups?: OutlineGroups<T>;
}
/** Context for the `tiling` callback: the parent's canvas and its children's layout. */
interface TilingCtx extends CellDims {
    nChildren: number;
    /** median laid-out child area in px² — the density cue */
    medianChildArea: number;
}
interface CellDims {
    w: number;
    h: number;
}
/** Render-time context passed to `colorForCell`. */
interface CellCtx extends CellDims {
    /** Whether this cell renders nested child tiles at the current size. */
    hasKids: boolean;
    /** Background opacity applied at this depth (the depth fade), so consumers
     * can compute what their color actually composites to on screen. */
    fade: number;
    /** With `collapseChains`, how many single-child levels this cell collapsed
     * beyond its first (0 = not a chain): `path` ends at the deepest node, so
     * the cell's own top node is `path[path.length - 1 - chain]`. Passed to
     * `renderCellExtra` only. */
    chain?: number;
}
declare function Treemap<T>({ root, initialPath, path: pathProp, getSize, getChildren, hasChildren, loadChildren, onChildrenLoaded, renderLoading, renderLoadError, getLabel, getId, formatSize, formatCell, collapseChains, reclaimTitleSpace, branchOutline, colorForCell, lens, renderCellExtra, renderTooltip, tipMode, renderTipDefault, renderRollup, renderLegend, renderCrumbSuffix, renderFooter, renderCellSubtitle, onCellClick, cellHref, onPathChange, onCellHover, minCellArea, minCellSide, mergeSmall, fullscreen, exportable, onExport, renderTip, chrome, showLabels, sizeAlign, inlineSizeMinWidth, className, mapStyle, depthFade, rootFade, fadeFloor, tiling, borderWidth, edgeEmphasis, borderStep, edgeContrast, dustTexture, nestedHues, foldControl, remainderTail, renderer, a11yLinks, a11yMaxCells, a11yMinSide, outlineGroups, }: TreemapProps<T>): react.JSX.Element;

/** Controlled layout switch; use the same value for `<Treemap reclaimTitleSpace>`. */
declare function TitleSpaceToggle({ checked, onChange }: {
    checked: boolean;
    onChange: (checked: boolean) => void;
}): react.JSX.Element;

/** Controlled stroke switch; use the same value for `<Treemap borderStep>`. */
declare function BorderStepToggle({ checked, onChange }: {
    checked: boolean;
    onChange: (checked: boolean) => void;
}): react.JSX.Element;

/**
 * A canvas cross-hatch that stands in for a folded "(other)" tile — the long
 * tail of cells too small to draw as their own rects. Instead of one flat grey
 * blob, it draws horizontal + vertical rules whose spacing *tightens toward the
 * lower-right*, evoking the continued shrink of the dust it represents, and
 * whose count scales with how many real cells were folded in (denser hatch =
 * more hidden items). Styled to read as distinct from real cells: thin rules on
 * a faded ground, not a solid fill with a label.
 *
 * Pure paint — hit-detection over the region lives in the consumer (the
 * treemap maps a cursor position back to a specific folded child).
 */
interface DustHatchProps {
    /** CSS px width of the region. */
    w: number;
    /** CSS px height of the region. */
    h: number;
    /** How many real cells were folded in — drives hatch density. */
    count: number;
    /** Rule color. Default: a translucent neutral that reads on either ground. */
    color?: string;
    /**
     * Rules per axis at `count = 1`, before the count scaling. The axis with
     * more room gets proportionally more. Default: 3.
     */
    baseLines?: number;
    /** Geometric growth of the gap moving away from the dense (lower-right)
     * corner. >1. Default: 1.32. */
    ratio?: number;
}
/** Line offsets from the dense end (0 = the tightest gap), growing
 * geometrically, that sum to `len`. Returns `n` interior positions measured as
 * distance from the dense end. */
declare function dustOffsets(len: number, n: number, ratio: number): number[];
/** Target rule count on an axis of length `len`, given how many cells folded
 * in — grows with `log2(count)`, clamped so a rule is never under ~4px apart. */
declare function dustLineCount(len: number, count: number, base: number): number;
/**
 * Stroke the dust cross-hatch into an already-configured 2D context, at CSS-px
 * origin `(ox, oy)`. Self-contained (sets stroke style + width, does its own
 * `beginPath`/`stroke`), so both the standalone `<DustHatch>` canvas and the
 * whole-map canvas renderer draw an identical tile. `ctx` is assumed already
 * dpr-scaled by the caller; coordinates are CSS px.
 */
declare function drawDust(ctx: CanvasRenderingContext2D, ox: number, oy: number, w: number, h: number, count: number, color?: string, baseLines?: number, ratio?: number): void;
declare function DustHatch({ w, h, count, color, baseLines, ratio }: DustHatchProps): react.JSX.Element;

/**
 * Whole-map canvas renderer for `<Treemap renderer="canvas">`.
 *
 * Paints the entire placed-cell tree to one `<canvas>` (parent-first, so
 * children draw over their container) and hit-tests the same retained tree —
 * one code path, one paint loop, no DOM node per cell. Interaction is not
 * re-implemented here: pointer hits resolve to `(node, path)` and are handed
 * straight back to `<Treemap>`'s drill/tooltip handlers, the identical path a
 * DOM cell's event takes.
 *
 * Parity is staged. This first cut draws base + faded bg + shared edge stroke +
 * dust tail + labels, and hit-tests cells (folded dust resolves to its specific
 * child, like the DOM tile). Segments (makeup stripes), chain-label rendering,
 * `cellHref` anchors, and the lazy-load overlay are still DOM-only.
 */
/** A resolved pointer hit: a real cell, or a specific child under a dust tile. */
interface CanvasHit<T> {
    /** Always a real node — a folded tile resolves to the child under the cursor. */
    node: T;
    path: T[];
    key: string;
    drillable: boolean;
    /** Whether this cell renders nested tiles (branch title-bar vs. leaf body). */
    branch: boolean;
    /** This hit is a child resolved out of a dust tile (pins, never drills, and
     * bypasses `onCellClick` — matching the DOM dust tile). */
    foldChild: boolean;
}
interface TreemapCanvasProps<T> {
    cells: PlacedCell<T>[];
    width: number;
    height: number;
    styleOpts: StyleOpts<T>;
    getSize: (n: T) => number;
    getLabel: (n: T) => string;
    formatSize: (n: number) => string;
    /** A cell's size text, overriding `formatSize(getSize(n))` (e.g. a signed change, while the area stays `getSize`). */
    formatCell?: (n: T) => CellText;
    /** Inline-size placement on a cell's first line (see `TreemapProps.sizeAlign`). */
    sizeAlign: 'left' | 'right';
    /** Min cell width for the inline size (see `TreemapProps.inlineSizeMinWidth`). */
    inlineSizeMinWidth: number;
    idFor: (n: T, p: T[]) => string;
    expandable: (n: T, p: T[]) => boolean;
    dustTexture: boolean;
    /** Per-cell link href for the a11y overlay (mirrors the DOM `cellHref`). */
    cellHref?: (n: T, path: T[]) => string | undefined;
    /** Build the focusable a11y overlay (see `TreemapProps.a11yLinks`). */
    a11yLinks: boolean;
    /** Cap on overlay elements, largest cells first. */
    a11yMaxCells: number;
    /** Minimum short-side px for a cell to get an overlay element. */
    a11yMinSide: number;
    /** Key of the currently pinned cell, ringed on the canvas so the pin reads. */
    pinnedKey: string | null;
    /** Mirror of the internal `<canvas>` element, for image export (`exportable`). */
    canvasRef?: React.MutableRefObject<HTMLCanvasElement | null>;
    onHover: (hit: CanvasHit<T>, clientX: number, clientY: number) => void;
    onClick: (hit: CanvasHit<T>, e: React.MouseEvent) => void;
    onLeave: () => void;
}
declare function TreemapCanvas<T>({ cells, width, height, styleOpts, getSize, getLabel, formatSize, formatCell, sizeAlign, inlineSizeMinWidth, idFor, expandable, dustTexture, cellHref, a11yLinks, a11yMaxCells, a11yMinSide, pinnedKey, canvasRef, onHover, onClick, onLeave, }: TreemapCanvasProps<T>): react.JSX.Element;

/**
 * Headless hover+pin state machine — the common core of the legend/map/cell
 * "pin" UX. Ported verbatim from marin-gcs-usage's `hoverpin/useHoverPin.ts`
 * (itself distilled from prior implementations in ctbk's StationMap, hbt's
 * GeoSankey, pltly's useTraceHighlight):
 *
 * - `active = pinned ?? hovered`; single pin; click toggles (re-click unpins,
 *   click elsewhere switches the pin).
 * - Touch devices (`(hover: none)`) ignore hover entirely — tap-toggle is the
 *   only interaction, sidestepping the hover→click→unhover race.
 * - Re-click-unpin suppresses that key's hover until the pointer truly leaves
 *   it, so unpinning doesn't instantly re-highlight.
 * - Outside-click clears the pin, scoped by `excludeRefs`/`excludeSelectors`
 *   (clicks inside those never count as "outside"). Escape also clears.
 *
 * No dependencies beyond React; keys are opaque (compared by Object.is).
 */
interface HoverPin<K> {
    hovered: K | null;
    pinned: K | null;
    /** pinned ?? hovered — what the UI should treat as focused */
    active: K | null;
    /** pointerenter/move with a key; pointerleave with null */
    hover: (k: K | null) => void;
    /** click on the keyed element: pin / unpin-if-same / switch */
    togglePin: (k: K) => void;
    clearPin: () => void;
}
interface HoverPinOpts {
    /** clicks inside these elements never clear the pin */
    excludeRefs?: React.RefObject<Element | null>[];
    /** clicks inside elements matching any selector never clear the pin */
    excludeSelectors?: string[];
}
declare function useHoverPin<K>({ excludeRefs, excludeSelectors }?: HoverPinOpts): HoverPin<K>;

/**
 * Color primitives for the treemap (and disk-tree's widgets). All CSS-var-
 * friendly so consumers can theme without touching component code.
 */

/**
 * The 8-slot categorical palette every widget defaults to. Shared so a node
 * colored slot-3 in the treemap is the same hue as its marker in the
 * scatter — cross-widget hue identity is the whole point of a fixed palette.
 */
declare const DEFAULT_PALETTE: string[];
/**
 * The categorical color for top-level slot `i`. The first
 * {@link DEFAULT_PALETTE}`.length` are the fixed shared palette (so slot-3 is
 * the same hue across widgets); past that, golden-angle-spaced hues keep many
 * top-level dirs distinct instead of wrapping the 8 slots — a bucket root can
 * have dozens of children (ctbk: 31).
 */
declare function slotColor(i: number, palette?: string[]): string;
/** The treemap map's opaque base — every cell's paint composites over this
 * (matches `--dt-treemap-container-bg`'s default). Consumers that theme the
 * base to a light ground pass their own to {@link contrastEdge}. */
declare const CONTAINER_BG: [number, number, number];
/**
 * A high-contrast half-stroke for a shared-tiling cell whose face is `bg`:
 * dark on light faces, light on dark ones. Each cell paints its own half of
 * every boundary it shares, so a bright cell beside a dark one gets a dark
 * half-stroke against the neighbour's light one — one fixed color can't serve
 * both, which is why grey-on-grey borders vanish.
 *
 * The face that actually lands on screen is `bg` (at its own alpha) composited
 * over the opaque `base`, then faded toward `base` by the depth `fade` the
 * widget applies to the paint layer — this reproduces that. Returns `null`
 * when `bg` can't be parsed (a container `var()`, a gradient), so the caller
 * keeps its own neutral fallback for those.
 */
declare function contrastEdge(bg: string | undefined, fade?: number, base?: [number, number, number]): string | null;
interface AgeFadeOpts {
    /**
     * Max fraction of the panel color mixed in at `age01 = 1` — the fade
     * *floor* clamp, so the oldest cells stay legible. Default 0.72.
     */
    floor?: number;
    /** Color faded toward. Default: the themable panel background var. */
    panel?: string;
}
/**
 * Age lens: fade a resolved cell style toward the panel background —
 * "fading from memory". Older (`age01` → 1) ⇒ more faded.
 *
 * Uses `color-mix(in oklch, …)` so the fade is *perceptual lightness*, even
 * across hues (equal age reads as equal fade — an RGB-alpha ramp doesn't),
 * and works with CSS-var colors the JS can't parse. Ink fades at half the
 * background's rate, preserving label contrast on faded cells.
 *
 * Composable: takes and returns a `CellStyle`, so it stacks on the default
 * categorical palette or any `colorForCell` output — pass via the treemap's
 * `lens` slot rather than replacing the color logic.
 */
declare function ageFade(style: CellStyle, age01: number, opts?: AgeFadeOpts): CellStyle;
/**
 * [oldest, newest] age extent over `nodes` (ignoring null/undefined ages) —
 * feed to `age01`. Returns `null` when no node has an age (lens should no-op).
 */
declare function ageDomain<T>(nodes: T[], getAge: (n: T) => number | null | undefined): [number, number] | null;
/**
 * Normalize an age (epoch seconds, days — any monotonic "older = smaller"
 * unit like mtime) into the `[0, 1]` fade domain: the *newest* value in the
 * domain maps to 0 (no fade), the oldest to 1. Degenerate domains map to 0.
 */
declare function age01(age: number, [oldest, newest]: [number, number]): number;

/**
 * Path matching for the filter plane (spec: viz-widgets.md §5, v0).
 *
 * v0 semantics are deliberately narrow and must be labeled as such in the UI:
 * this filters *what is displayed at the current level*. It does not
 * re-aggregate — a directory's size still counts children that the filter
 * hides. (True re-aggregation is v1, and needs either a server round-trip or
 * the segment index.) Saying "highlight + re-layout" is honest; saying
 * "filtered totals" would not be.
 */

interface QueryOpts {
    /** Match case-sensitively. Default: false. */
    caseSensitive?: boolean;
}
/**
 * Compile a query string into a path predicate.
 *
 * `/…/` (optionally with trailing flags) is a regex; anything else is a plain
 * substring. **An in-progress or invalid regex never throws** — it degrades to
 * a literal substring match, because this runs on every keystroke and a
 * half-typed `(` must not blank the view. An empty query matches everything.
 */
declare function parseQuery(query: string, opts?: QueryOpts): (path: string) => boolean;
/** Keep the nodes whose extracted path matches `query`. */
declare function filterNodes<T>(nodes: T[], getPath: (n: T) => string, query: string, opts?: QueryOpts): T[];
interface DimOpts {
    /** Opacity applied to non-matching cells. Default 0.15. */
    opacity?: number;
}
/**
 * Dim a cell that doesn't match — pass through the treemap's `lens` slot so it
 * *stacks* on the palette (and on the age lens) instead of replacing colors.
 * Returns `null` for matching cells, i.e. "leave this one alone".
 */
declare function dimUnmatched(style: CellStyle, matched: boolean, opts?: DimOpts): CellStyle | null;

interface OutlineOverlayProps<T> {
    cells: PlacedCell<T>[];
    width: number;
    height: number;
    groups: OutlineGroups<T>;
}
declare function OutlineOverlay<T>({ cells, width, height, groups }: OutlineOverlayProps<T>): react.JSX.Element;

/**
 * Canvas `fillStyle` / `strokeStyle` silently ignore a CSS custom-property
 * reference (`var(--x)` paints black), yet a themed consumer names its colors
 * exactly that way. Resolve `var(--name[, fallback])` against an element's
 * computed style; any other color string passes through untouched.
 */
declare function cssColor(el: Element, color: string): string;
/**
 * A memoizing resolver for one paint pass: thousands of cells reuse a handful
 * of `var()` strings, and `getComputedStyle` per cell is the kind of cost the
 * canvas renderer exists to avoid. Make a fresh one per pass so a theme
 * change is picked up on the next paint.
 */
declare function colorResolver(el: Element): (color: string) => string;

/** A row's diff verdict. `touched`: same size & count, mtime differs (rename /
 *  net-zero churn / `touch`). */
type DiffStatus = 'added' | 'removed' | 'changed' | 'touched' | 'unchanged';
/** Node statuses add two synthetic kinds the tree builder introduces:
 *  `filler` (unenumerated unchanged bytes) and `fold` (aggregated dust). */
type DiffNodeStatus = DiffStatus | 'filler' | 'fold';
type DiffKind = 'file' | 'dir' | 'filler' | 'fold';
/** Area mode: `max` = one cell per row sized by `max(old, new)`, |Δ| painted as
 *  a band; `delta` = churn only, sized by `|Δ|`. */
type DiffAreaMode = 'max' | 'delta';
/** A node in the diff treemap. Field names are metric-agnostic (`oldSize`/
 *  `newSize`/`countDelta`) so any part-of-whole diff maps onto it. */
interface DiffNode {
    key: string;
    label: string;
    /** what the widget sizes by — `max(old, new)` or `|Δ|` per mode; parents take
     * `max(own, Σ children)` so children can never overflow. */
    weight: number;
    /** signed primary-metric delta, for coloring */
    delta: number;
    /** Σ of positive / negative deltas across descendants (frontier-leaf
     * granularity): `grew ≥ 0`, `shrank ≤ 0`, `grew + shrank ≈ delta`. */
    grew: number;
    shrank: number;
    /** same, for the secondary count metric (`countDelta`): `nGrew ≥ 0`, `nShrank ≤ 0` */
    nGrew: number;
    nShrank: number;
    status: DiffNodeStatus;
    oldSize: number;
    newSize: number;
    countDelta: number;
    kind: DiffKind;
    uri: string;
    /** fold cells: how many too-small-to-draw children were merged */
    nFolded?: number;
    /** filler cells: the unenumerated unchanged children it stands for
     * (direct-child count and their descendants), when the server told us */
    nRest?: number;
    nDescRest?: number;
    /** Frontier dir with unexplored change below (budget/depth cut the walk). */
    pruned?: boolean;
    children?: DiffNode[];
}
/** One recursive-frontier row (a change at some depth). */
interface DiffRecRow {
    path: string;
    uri: string;
    depth: number;
    kind: 'file' | 'dir';
    status: DiffStatus;
    oldSize: number;
    newSize: number;
    delta: number;
    countDelta: number;
    /** Stats differ below but the walk stopped here (budget/depth). */
    pruned: boolean;
}
/** One depth-1 flat row, for labeled grey context at the top level. */
interface DiffFlatRow {
    path: string;
    uri: string;
    kind: 'file' | 'dir';
    status: DiffStatus;
    oldSize: number;
    newSize: number;
}
/** Persisted full-diff index state for the pair. */
interface DiffIndexInfo {
    status: 'none' | 'building' | 'done' | 'failed';
    error?: string;
}
/** Everything the tree builder + treemap need about one compared subtree. The
 *  consumer maps its own compare payload onto this. */
interface DiffInput {
    uri: string;
    /** depth-1 rows (grey context; only `unchanged` ones are used) */
    flatRows: DiffFlatRow[];
    /** recursive change frontier across depths */
    recRows: DiffRecRow[];
    /** each expanded dir's biggest unchanged children (labeled grey cells) */
    unchangedTop: DiffRecRow[];
    /** aggregate of the rest, keyed by parent path (`''` = the compared uri) */
    unchangedRest: Record<string, {
        count: number;
        size: number;
        n_desc: number;
    }>;
    /** net primary-metric delta for the whole compared subtree */
    totalDelta: number;
    oldRootSize: number;
    newRootSize: number;
    oldRootCount: number;
    newRootCount: number;
    index?: DiffIndexInfo;
}
interface DiffTreemapProps {
    input: DiffInput;
    recState: 'loading' | 'error' | 'ready';
    onRecRetry: () => void;
    onDrill: (uri: string) => void;
    /** middle-click / open-in-new-tab href for a drillable dir */
    cellHref?: (uri: string) => string | undefined;
    /** Fetch a node's own diff subtree (the widget rebuilds cells at its current
     *  area/unchanged mode). One request per drill, cached by the widget. */
    fetchSubtree: (node: DiffNode) => Promise<DiffSubtree>;
    formatSize: (n: number | null | undefined) => string;
    formatCount: (n: number | null | undefined) => string;
    tiling: Tiling;
    setTiling: (t: Tiling) => void;
    /** The map box, measured for the drawable floor (see `mapMinFrac`). */
    mapRef: RefObject<HTMLDivElement | null>;
    /** Chrome slots (plain fallbacks when omitted); a disk/MUI consumer supplies
     *  its own so the surface matches the rest of the app. */
    renderContainer?: (children: ReactNode) => ReactNode;
    renderOverlay?: (recState: 'loading' | 'error' | 'ready', onRetry: () => void) => ReactNode;
    renderEmpty?: (opts: {
        areaMode: DiffAreaMode;
        showUnchanged: boolean;
        onShowUnchanged: () => void;
    }) => ReactNode;
}
/** The raw sub-diff a drill fetches (no root aggregation). */
interface DiffSubtree {
    recRows: DiffRecRow[];
    unchangedTop: DiffRecRow[];
    unchangedRest: Record<string, {
        count: number;
        size: number;
        n_desc: number;
    }>;
}
/** One metric column-group (old / new / Δ / bar) in the diff table. */
interface DiffMetric {
    id: string;
    /** group header ('Size', 'Descendants') */
    label: string;
    /** only meaningful for directories (files render `-`) */
    dirOnly?: boolean;
    fmt: (v: number | null) => string;
    fmtDelta: (v: number) => string;
}
/** One table row's per-metric old/new/Δ values. */
interface DiffRowValues {
    [metricId: string]: {
        old: number | null;
        new: number | null;
        delta: number;
    };
}
interface DiffTableRow {
    key: string;
    path: string;
    uri: string;
    kind: 'file' | 'dir';
    status: DiffStatus;
    values: DiffRowValues;
}
interface DiffTableProps {
    rows: DiffTableRow[];
    /** the compared dir itself (the highlighted `.` summary row) */
    parent: {
        uri: string;
        values: DiffRowValues;
    };
    metrics: DiffMetric[];
    /** count of unchanged entries omitted (for the footer note) */
    unchangedCount: number;
    pageSize?: number;
    /** leading icon for a row (app: folder/file glyph) */
    renderIcon?: (row: {
        kind: 'file' | 'dir';
    }) => ReactNode;
    /** wrap a dir's path label in a link (app: SPA router Link) */
    renderPathLink?: (row: DiffTableRow, children: ReactNode) => ReactNode;
    /** trailing per-row action cell (app: rescan button) */
    rowAction?: (row: {
        uri: string;
        kind: 'file' | 'dir';
        path: string;
    }) => ReactNode;
}

type Fmt = (n: number | null | undefined) => string;
/**
 * `▲ 32.4 ▼ 6.9 Δ +25.5` — churn among a node's descendants, for title
 * strips / crumbs / tooltips. Units are omitted when every value shares the
 * unit of the total it follows (`totalBytes`), and shown per value otherwise
 * (`▲ 100M ▼ 200K Δ +100M`) — never a mixed line the reader has to parse.
 * Both directions when there's churn both ways, else just the Δ.
 */
declare function churn(n: {
    grew: number;
    shrank: number;
    delta: number;
}, 
/** bytes: the total the line follows (drives unit elision); counts: `null` */
totalBytes: number | null, formatSize: Fmt, formatCount: Fmt, 
/** omit the `Δ` part (the caller already shows the net elsewhere) */
net?: boolean): {
    text: string;
    node: ReactNode;
} | null;
/**
 * Δ-recolor treemap with two area modes:
 *
 * - `max` (default): one cell per row, sized by `max(old, new)` — deleted
 *   subtrees keep their old area, added ones their new area, and unchanged
 *   structure stays visible as neutral context. Color encodes Δ/max per cell:
 *   pure-add is fully red, pure-delete fully green, unchanged neutral.
 *   Caveat (labeled): cell areas sum to more than either side's true total.
 * - `Δ`: the churn view — only changed rows, sized by `|Δbytes|`, colored by
 *   Δ relative to the largest |Δ|.
 *
 * Clicking a directory drills via `onDrill(uri)` so the exploration matches
 * the consumer's deep-link scheme.
 */
declare function DiffTreemap({ input, recState, onRecRetry, onDrill, cellHref, fetchSubtree, formatSize, formatCount, tiling, setTiling, mapRef, renderContainer, renderOverlay, renderEmpty, }: DiffTreemapProps): react.JSX.Element;

declare function DiffTable({ rows, parent, metrics, unchangedCount, pageSize, renderIcon, renderPathLink, rowAction, }: DiffTableProps): react.JSX.Element;

/**
 * Recursive-diff frontier rows + the depth-1 unchanged rows (from the plain
 * compare, for labeled grey context at the top level) → a nested tree.
 *
 * Weights are bottom-up: a leaf is `max(old, new)` (or `|Δ|` in Δ mode); a
 * parent is `max(its own max, Σ children)` — churn (delete X + add Y) makes
 * children sum past either side's bytes, and the parent honestly grows to
 * hold them. Where children under-fill a parent (unchanged bytes the walk
 * never enumerated), a grey filler cell absorbs the gap, so areas stay
 * truthful without shipping every unchanged row. The server ships each
 * expanded dir's biggest unchanged children (`unchangedTop`, named grey
 * cells) and an aggregate of the rest (`unchangedRest`) — the filler's
 * tooltip counts what it stands for.
 *
 * Children order is signed: biggest adds first, unchanged middle, biggest
 * shrinks last (sort by -Δ).
 */
declare function buildDiffTree(input: DiffInput, areaMode: DiffAreaMode, showUnchanged: boolean): {
    cells: DiffNode[];
    maxAbsDelta: number;
};
/** The smallest cell worth drawing, in px² — the widget's own fold floor. */
declare const MIN_CELL_PX = 16;
/**
 * Has diff detail we could fetch for it: a dir whose children the response
 * trimmed but whose own row says something changed *inside* it. Everything
 * else has no slice to fetch — an unchanged dir has no index rows at all, and
 * an added/removed one is stored as a single row by design (its whole subtree
 * is the change). Those navigate to their own page rather than drilling into
 * a blank map.
 */
declare const fetchable: (n: DiffNode) => boolean;
/**
 * The map's drawable floor as a byte fraction: a node's screen area is ≈ its
 * share of the compared subtree × the canvas, so anything under
 * `MIN_CELL_PX / canvas_px` can't be drawn and needn't be sent.
 *
 * The first request fires before the map mounts, so fall back to the size the
 * layout will give it (page `maxWidth` 1400 minus padding; the responsive
 * height below) — an estimate beats the server's fixed default.
 */
declare function mapMinFrac(ref: RefObject<HTMLDivElement | null>): number;

/**
 * Diff polarity (git convention): green = added/grew, red = removed/shrank —
 * matching the added/removed row tints and the summary chips. This is a diff
 * view, not a cost alarm; a "growth = red" cost lens can be a toggle later.
 * `divergingColor` is red-positive, so negate on the way in.
 */
declare const GREW_GREEN = "#3fb950";
declare const SHRANK_RED = "#f85149";
declare const NEUTRAL = "#8b949e";
/** Unchanged-bytes fill: translucent dark grey (not `divergingColor(0)`'s mid
 * grey, which left the light ink low-contrast) — the colored bands pop and
 * labels read. */
declare const UNCHANGED_GREY = "rgba(110, 118, 129, 0.28)";
/** touched (same bytes, mtime moved): the unchanged grey, hatched */
declare const TOUCHED_HATCH = "repeating-linear-gradient(45deg, rgba(255, 255, 255, 0.11) 0 3px, transparent 3px 9px)";
declare const deltaColor: (t: number) => string;
/** Legend swatch color: the translucent grey as it actually paints (over the
 * map's base), so it isn't invisible on the bar's own dark background. */
declare const UNCHANGED_SWATCH: string;
declare const deltaTextColor: (d: number) => "#3fb950" | "#f85149" | "#8b949e";
/** Row background tint by status (added/removed get a faint wash). */
declare const statusColors: {
    added: {
        bg: string;
    };
    removed: {
        bg: string;
    };
    changed: {
        bg: string;
    };
    touched: {
        bg: string;
    };
    unchanged: {
        bg: string;
    };
};

export { type AgeFadeOpts, BorderStepToggle, CONTAINER_BG, type CanvasHit, type CellCtx, type CellDims, CellStyle, CellText, type ComposeOptions, DEFAULT_PALETTE, type DiffAreaMode, type DiffFlatRow, type DiffIndexInfo, type DiffInput, type DiffKind, type DiffMetric, type DiffNode, type DiffNodeStatus, type DiffRecRow, type DiffRowValues, type DiffStatus, type DiffSubtree, DiffTable, type DiffTableProps, type DiffTableRow, DiffTreemap, type DiffTreemapProps, type DimOpts, DustHatch, type DustHatchProps, type ExportContext, type ExportKind, type ExportOptions, GREW_GREEN, type HoverPin, type HoverPinOpts, MIN_CELL_PX, NEUTRAL, type OutlineGroups, OutlineOverlay, type OutlineOverlayProps, type OutlinePath, type OutlineSeg, PlacedCell, type QueryOpts, SHRANK_RED, StyleOpts, TOUCHED_HATCH, Tiling, type TilingCtx, TitleSpaceToggle, Treemap, TreemapCanvas, type TreemapCanvasProps, type TreemapProps, UNCHANGED_GREY, UNCHANGED_SWATCH, age01, ageDomain, ageFade, buildDiffTree, canvasToPngBlob, churn, colorResolver, composeExport, contrastEdge, copyPng, cssColor, defaultExportFilename, deltaColor, deltaTextColor, dimUnmatched, downloadPng, drawDust, dustLineCount, dustOffsets, fetchable, filterNodes, groupOutlines, groupRects, mapMinFrac, parseQuery, slotColor, statusColors, unionOutline, useHoverPin };
