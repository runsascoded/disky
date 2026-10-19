import * as react from 'react';
import { ReactNode, CSSProperties } from 'react';

/**
 * Clip polygons and area math for Voronoi treemaps (spec: viz-widgets.md §6).
 *
 * Kept separate from the component so the geometry — the part with actual
 * invariants (area ∝ value, cells tile the clip) — is unit-testable without a
 * DOM.
 */
type Point = [number, number];
type Polygon = Point[];
/** Regular `sides`-gon approximating a circle — the clip for glyph mode. */
declare function circlePolygon(cx: number, cy: number, r: number, sides?: number): Polygon;
/** Axis-aligned rectangle as a clip polygon. */
declare function rectPolygon(x: number, y: number, w: number, h: number): Polygon;
/** Shoelace area, always positive (winding-order independent). */
declare function polygonArea(poly: Polygon): number;
/** Area-weighted centroid — where a cell's label wants to sit. */
declare function polygonCentroid(poly: Polygon): Point;
/** SVG `points` attribute for a polygon. */
declare function toPointsAttr(poly: Polygon, digits?: number): string;
/**
 * Largest relative area error across cells: `max |actual/total − value/Σvalue|
 * / (value/Σvalue)`. The Voronoi solver is iterative, so this is how you check
 * a layout actually converged rather than assuming it did.
 */
declare function maxAreaError(cells: {
    polygon: Polygon;
    value: number;
}[]): number;

/**
 * Thin, deterministic wrapper over `d3-voronoi-treemap` (spec: viz-widgets.md §6).
 *
 * The solver is iterative: it nudges site weights until each cell's area
 * matches its value share, stopping at `convergenceRatio` or
 * `maxIterationCount`. Two consequences the callers must not paper over:
 *
 * 1. **It can stop short of converging.** We return the achieved error so the
 *    UI can label an unconverged layout instead of implying exact areas.
 * 2. **It is random-seeded.** Left alone it uses `Math.random`, so the same
 *    data lays out differently every render (cells visibly jump). We always
 *    seed it, making the layout a pure function of (values, clip, seed).
 */

interface VoronoiCell<T> {
    node: T;
    value: number;
    polygon: Polygon;
    centroid: Point;
    area: number;
}
interface VoronoiLayout<T> {
    cells: VoronoiCell<T>[];
    /** Items dropped for being too small to tessellate (see `minShare`). */
    excluded: number;
    /** Their combined value — so a caller can say what fraction is missing. */
    excludedValue: number;
    /**
     * Worst *per-cell relative* area error — `|actual − target| / target`, the
     * error a reader of the chart would perceive ("this cell is 5% too big").
     */
    error: number;
    /** Whether `error` came in under `tolerance`. Label the chart when false. */
    converged: boolean;
}
interface VoronoiLayoutOpts {
    /**
     * The solver's own stop criterion, in units of **clip area**: it stops once
     * cell-area error falls under this fraction of the whole clipping polygon.
     * That is not a per-cell relative bound — at d3's default (0.01) a cell
     * holding 20% of the area can still land ~5% off its target. We default to
     * `0.001`, which measured ~0.01% relative error on a 3-cell square versus
     * ~1.3% at 0.01, for a handful of extra iterations.
     */
    convergenceRatio?: number;
    /** Hard iteration cap. Default 200. */
    maxIterationCount?: number;
    /**
     * Drop items below this fraction of the total before laying out. Default
     * 0.005.
     *
     * This is not tidiness, it's a hard limit of the technique: the solver
     * cannot shrink a cell arbitrarily (see `minWeightRatio`), so on a real
     * directory listing — where the biggest child outweighs the smallest by
     * six orders of magnitude — micro-cells come out *thousands of percent*
     * too large and drag the whole tessellation off target. Measured on a
     * 13-child listing: 421,710% worst-case error unfiltered, 0.14% at 0.005.
     * Rect treemaps have no such floor; this is one reason they stay the
     * default view.
     */
    minShare?: number;
    /**
     * Smallest site weight the solver will keep, as a fraction of the largest.
     * Anything under it is clamped *up*, so a cell far below the floor renders
     * much bigger than its value — the library's 0.01 default makes a child
     * holding 0.1% of the bytes come out ~200% too large. Default 1e-4.
     */
    minWeightRatio?: number;
    /** Layout seed — same seed + data ⇒ same picture. Default: 1. */
    seed?: number | string;
    /**
     * Max acceptable per-cell relative area error before `converged` goes
     * false (and the component labels the chart as approximate). Default 0.02.
     */
    tolerance?: number;
}
/**
 * Tessellate `clip` so each item's cell area is proportional to its value.
 * Non-positive values are dropped (a zero-area cell has no meaning here).
 */
declare function voronoiLayout<T>(items: T[], getValue: (n: T) => number, clip: Polygon, opts?: VoronoiLayoutOpts): VoronoiLayout<T>;

/**
 * Voronoi treemap for one level of children (spec: viz-widgets.md §6).
 *
 * Deliberately **not** a replacement for `<Treemap>`: rectangles give labels
 * far better real estate, and the rect layout is exact where this one is
 * iterative. Where this wins is a *circular* clip — a glyph inside a scatter
 * marker, or an aesthetic full-panel alt view — which rectangles can't fill.
 *
 * Ships from the `@open-athena/treemap/voronoi` subpath so the core package stays
 * dependency-free; `d3-voronoi-treemap` and `d3-hierarchy` are optional peers
 * that only consumers of this subpath need to install.
 */
interface VoronoiTreemapProps<T> extends VoronoiLayoutOpts {
    items: T[];
    getValue: (n: T) => number;
    getLabel?: (n: T) => string;
    getColor?: (n: T, i: number) => string | null | undefined;
    /** `circle` (default) fills the panel's inscribed circle; `rect` fills it. */
    shape?: 'circle' | 'rect';
    /** Explicit clip polygon; overrides `shape`. */
    clip?: Polygon;
    /** Draw labels in cells with room for them. Default: true. */
    showLabels?: boolean;
    /** Format the excluded-items total in the footer note (e.g. bytes). */
    formatValue?: (v: number) => string;
    renderTooltip?: (n: T) => ReactNode;
    onCellClick?: (n: T, event: React.MouseEvent) => void;
    className?: string;
    style?: CSSProperties;
    height?: number | string;
}
declare function VoronoiTreemap<T>({ items, getValue, getLabel, getColor, shape, clip, showLabels, formatValue, renderTooltip, onCellClick, convergenceRatio, maxIterationCount, minShare, minWeightRatio, seed, tolerance, className, style, height, }: VoronoiTreemapProps<T>): react.JSX.Element;

/**
 * Deterministic PRNG for the Voronoi layout.
 *
 * `d3-voronoi-treemap` seeds its initial site positions from `Math.random` by
 * default, so the same data lays out differently on every render — cells would
 * visibly jump on any state change, and tests couldn't assert geometry. A
 * seeded generator makes the layout a pure function of (data, clip, seed).
 *
 * mulberry32: 32-bit state, uniform enough for site seeding, and short enough
 * to keep the subpath free of another dependency.
 */
declare function mulberry32(seed: number): () => number;
/** Stable 32-bit hash of a string — turns a node path into a layout seed. */
declare function hashSeed(s: string): number;

export { type Point, type Polygon, type VoronoiCell, type VoronoiLayout, type VoronoiLayoutOpts, VoronoiTreemap, type VoronoiTreemapProps, circlePolygon, hashSeed, maxAreaError, mulberry32, polygonArea, polygonCentroid, rectPolygon, toPointsAttr, voronoiLayout };
