import { Rect } from './squarify.js';

/** A cell's size text (`TreemapProps.formatCell`): one string, or candidates longest first — the renderer draws the
 *  first that fits the room its label leaves (e.g. a diff's `+37.2 T −20.4 T = +16.8 T`, else `+16.8 T`). */
type CellText = string | readonly string[];
/** The fewest px a cell's name keeps beside an inline size before a longer size candidate gives way. */
declare const NAME_MIN_PX = 48;
declare const candidates: (t: CellText) => readonly string[];
/** The first of `cs` whose width (`measure`) is at most `room` px; past every one, the last (the shortest: the
 *  renderer clips it as it clips any size). */
declare function fitCandidate(cs: readonly string[], room: number, measure: (s: string) => number): string;
/** The px an inline size may take in a title bar `boxW` px wide (padding `pad` per side, `gap` before the size): all
 *  of it but the name's, kept down to `NAME_MIN_PX` (or the whole name when shorter). */
declare const inlineRoom: (boxW: number, pad: number, gap: number, nameW: number) => number;

/** The treemap's renderer-agnostic types: a cell's paint and the tiling
 * mode. A plain module (no JSX), so DOM-free consumers (`layout`, `svg`, a
 * Worker) can import them without `Treemap.tsx`. */
type Tiling = 'gaps' | 'shared';
interface CellStyle {
    /** background CSS color/gradient */
    bg?: string;
    /** ink color for the label */
    ink?: string;
    /** repeating-gradient overlay (e.g. the class-lens hatch marin uses) */
    hatch?: string;
    /**
     * Shared-tiling stroke for THIS cell, overriding `--dt-treemap-edge`. Each
     * cell paints its own half of every boundary it shares, so neighbours may
     * choose different colors — each half then contrasts with the face it
     * borders (one stroke color can't serve a bright cell and a dark one).
     * `CellCtx.fade` gives the background opacity applied at this depth, so a
     * consumer can compute the cell's true composited luminance.
     */
    edge?: string;
    /** opacity multiplier (0–1). Combined with the built-in depth fade; applies
     * to the cell's background layer, never its label ink. */
    opacity?: number;
    /**
     * Proportional makeup stripes for a leaf-rendered cell (no nested tiles at
     * the current depth — folds and max-depth leaves): instead of one dominant
     * color, the cell renders these as inset slices along its longer axis,
     * proportional to `frac`. The inset frame (painted `bg`) plus the shared
     * outer border distinguish a composition breakdown from real child tiles.
     * Ignored when fewer than 2 segments, when the cell renders children, or
     * when the cell is too small to read.
     */
    segments?: {
        color: string;
        frac: number;
    }[];
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
    ring?: string | {
        color: string;
        width?: number;
        inset?: boolean;
    };
}

/**
 * Renderer-agnostic treemap layout.
 *
 * `layoutCells` turns a tree into a *placed-cell tree* — geometry plus node
 * identity, no style, no paint. It exists so the DOM renderer (`<Treemap>`'s
 * `cell()`) and the canvas renderer (`<TreemapCanvas>`) share one layout, and
 * a hit-test can walk the same retained tree a paint pass drew from.
 *
 * The formulas here mirror `Treemap.cell()` exactly (box insets, the
 * `w>90 && h>44` recursion gate, the shared/gaps inner offsets, chain
 * collapse), so the two renderers place cells identically and the DOM path can
 * later adopt this as its single source of truth.
 */

/** Synthetic node the default fold-small merger returns. */
interface FoldedNode<T> {
    __folded: true;
    count: number;
    size: number;
    children: T[];
}
declare function isFolded<T>(n: T | FoldedNode<T>): n is FoldedNode<T>;
/** A laid-out cell: absolute map coordinates + node identity + nesting. */
interface PlacedCell<T> {
    /** The node this cell represents (a `FoldedNode` for the folded tail). */
    node: T | FoldedNode<T>;
    /** Full ancestry path ending at `node` (folded tiles reuse the parent path). */
    path: T[];
    folded: boolean;
    /** Collapsed single-child spine labels, when `collapseChains` merged one. */
    chainLabels: string[] | null;
    x: number;
    y: number;
    w: number;
    h: number;
    depth: number;
    mode: Tiling;
    /** Shared-mode half-stroke width in px (0 in gaps mode). */
    edge: number;
    /** Rendered small enough (< 14px short side) to be a "dust" cell. */
    dust: boolean;
    /** Cell box after the gaps gutter / shared inset — the region children fill. */
    boxW: number;
    boxH: number;
    showLbl: boolean;
    hasKids: boolean;
    /** Width of the parent's title hover region; null keeps the full-width bar. */
    titleWidth: number | null;
    children: PlacedCell<T>[];
}
type Node<T> = T | FoldedNode<T>;
/** Everything `layoutCells` needs from the host component, threaded as one bundle. */
interface LayoutConfig<T> {
    getSize: (n: T) => number;
    getLabel: (n: T) => string;
    /** Resolve a node's children (already merged with any lazily-fetched set). */
    childrenOf: (n: T, path: T[]) => T[] | undefined;
    showLabels: boolean;
    collapseChains: boolean;
    borderWidth: (depth: number, ctx: {
        w: number;
        h: number;
    }) => number;
    /** Shallow-edge emphasis (see {@link edgeEmphFactor}); 0 = uniform. */
    edgeEmphasis: number;
    /** Measured title content width. Absent keeps a full-width title strip. */
    getTitleWidth?: (n: T, chainLabels: string[] | null, w: number, boxW: number) => number;
    /** Fold small/thin items before layout (the component's `fold`). */
    fold: (raw: Node<T>[], w: number, h: number) => Node<T>[];
    /** Lay already-folded items into a box (plain squarify or `squarifyRemainder`). */
    layTiles: (items: Node<T>[], x: number, y: number, w: number, h: number) => Rect<Node<T>>[];
    /** Decide a node's children's tiling mode from their laid-out density. */
    tilingFor: (n: T, path: T[], depth: number, w: number, h: number, rs: {
        w: number;
        h: number;
    }[]) => Tiling;
}
/**
 * Lay out a whole (sub)tree into an absolute-positioned placed-cell tree.
 *
 * `topRects` are the already-laid top-level rects (the component's `rects`,
 * which include its `foldThin` refold) so the top level isn't re-decided here;
 * this only recurses the nested levels. `basePath` ends at the viewed node.
 */
declare function layoutCells<T>(topRects: Rect<Node<T>>[], basePath: T[], rootMode: Tiling, cfg: LayoutConfig<T>): PlacedCell<T>[];
/** Flatten a placed tree parent-first (paint order: parents before children). */
declare function flattenPlaced<T>(cells: PlacedCell<T>[], out?: PlacedCell<T>[]): PlacedCell<T>[];
/** Deepest cell whose rect contains (px, py), or null — the hit-test. */
declare function hitTest<T>(cells: PlacedCell<T>[], px: number, py: number): PlacedCell<T> | null;

interface StyleOpts<T> {
    colorForCell?: (n: T, path: T[], depth: number, ctx: {
        w: number;
        h: number;
        hasKids: boolean;
        fade: number;
    }) => CellStyle | null | undefined;
    lens?: (n: T, path: T[], depth: number, ctx: {
        w: number;
        h: number;
        hasKids: boolean;
        fade: number;
    }, style: CellStyle) => CellStyle | null | undefined;
    getLabel: (n: T) => string;
    /** Top-level label → categorical slot color. */
    topLevelSlot: Map<string, string>;
    defaultSlots: string[];
    dustTexture: boolean;
    edgeContrast: boolean;
    /** Hierarchical L1-macro / L2-micro hues + tinted container headers. */
    nestedHues?: boolean;
    /** Background opacity at a nesting depth (the depth fade). */
    fadeAt: (d: number) => number;
}
/** Resolved paint for one placed cell: its style plus the adaptive edge (if any). */
interface ResolvedStyle {
    style: CellStyle;
    /** Adaptive contrast half-stroke, when shared-tiling + `edgeContrast` and the
     * consumer pinned no `edge`; else null (falls back to the neutral gutter). */
    builtinEdge: string | null;
}
declare function resolveCellStyle<T>(cell: PlacedCell<T>, o: StyleOpts<T>): ResolvedStyle;
/** The `FoldedNode` behind a folded placed cell (its `.count`/`.children`). */
declare function foldedOf<T>(cell: PlacedCell<T>): FoldedNode<T> | null;

export { type CellText as C, type FoldedNode as F, type LayoutConfig as L, NAME_MIN_PX as N, type PlacedCell as P, type ResolvedStyle as R, type StyleOpts as S, type Tiling as T, type CellStyle as a, flattenPlaced as b, candidates as c, foldedOf as d, isFolded as e, fitCandidate as f, hitTest as h, inlineRoom as i, layoutCells as l, resolveCellStyle as r };
