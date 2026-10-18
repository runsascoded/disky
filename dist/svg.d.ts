import { T as Tiling, C as CellText, S as StyleOpts, P as PlacedCell } from './cellStyle-DkAN5dVj.js';
import './squarify.js';

interface StaticLayoutOpts<T> {
    width: number;
    height: number;
    getSize: (n: T) => number;
    getLabel: (n: T) => string;
    getChildren: (n: T) => T[] | undefined;
    /** `<Treemap>`'s defaults unless given. */
    collapseChains?: boolean;
    showLabels?: boolean;
    minCellArea?: number | null;
    minCellSide?: number | null;
    tiling?: Tiling;
    borderWidth?: (depth: number, ctx: {
        w: number;
        h: number;
    }) => number;
    edgeEmphasis?: number;
}
/** The placed-cell tree for `root`'s children at `width`×`height`, as `<Treemap>` lays it. */
declare function staticLayout<T>(root: T, o: StaticLayoutOpts<T>): PlacedCell<T>[];
interface SvgPaintOpts<T> {
    width: number;
    height: number;
    getSize: (n: T) => number;
    getLabel: (n: T) => string;
    formatSize: (n: number) => string;
    /** A cell's size text, overriding `formatSize(getSize(n))` (e.g. a signed change; candidates longest first: `CellText`). */
    formatCell?: (n: T) => CellText;
    colorForCell?: StyleOpts<T>['colorForCell'];
    /** Text width in px of `text` at `px` font size (the SVG's font). */
    measure: (text: string, px: number) => number;
    /** `var(--name)` values for colors the styles reference (a consumer's theme);
     *  an unlisted var falls back to its `var(--name, fallback)` fallback. */
    vars?: Record<string, string>;
    fontFamily?: string;
    /** Label sizes, px (`--dt-treemap-lbl-fs` / `-sm`). */
    labelPx?: number;
    labelPxSm?: number;
    depthFade?: number;
    rootFade?: number;
    fadeFloor?: number;
    edgeContrast?: boolean;
    dustTexture?: boolean;
    sizeAlign?: 'left' | 'right';
    inlineSizeMinWidth?: number;
}
declare function escXml(s: string): string;
/** Substitute `var(--x[, fallback])` from `vars` (recursively), so the color
 * parser sees a literal. */
declare function resolveVars(c: string, vars: Record<string, string>): string;
/** The map as SVG elements (no `<svg>` wrapper), origin at (0, 0): a `<g>`
 * a caller can translate into a larger image. */
declare function treemapSvg<T>(cells: PlacedCell<T>[], o: SvgPaintOpts<T>): string;

export { type StaticLayoutOpts, type SvgPaintOpts, escXml, resolveVars, staticLayout, treemapSvg };
