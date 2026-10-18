/**
 * Squarified treemap layout (Bruls et al., "Squarified Treemaps", 2000).
 *
 * Ported from marin-gcs-usage's `site/src/squarify.ts`, generified to work on
 * any `{ size, ... }` node instead of hardcoded `{ b }`. The algorithm is:
 *
 *   walk items in decreasing-weight order; keep adding to the current row
 *   while it improves the worst-case aspect ratio; when it doesn't, lay
 *   out the row along the shorter side of the remaining rectangle and
 *   start a new row on what's left.
 *
 * This is the shape everyone expects when they say "treemap"; the classic
 * slice-and-dice alternative gives ugly stripes for skewed distributions.
 */
interface Rect<T> {
    /** The input node this rect represents. */
    it: T;
    x: number;
    y: number;
    w: number;
    h: number;
}
/**
 * Lay out `items` inside `[x, y, w, h]`, returning one Rect per item.
 *
 * @param items    non-negative-weighted items; zero-weight items are dropped
 * @param getSize  extracts the weight (bytes, count, whatever) from an item
 */
declare function squarify<T>(items: T[], x: number, y: number, w: number, h: number, getSize: (it: T) => number): Rect<T>[];
/**
 * Fold items too small to render at this scale into one synthetic node, so
 * their combined area shows as a single tile instead of dropped rows of
 * sub-6px cells (which read as dead space).
 *
 * The synthetic node is built by `mergeSmall`, which receives the folded
 * items and returns a stand-in of the same shape `T` (label like "(+3)",
 * summed size, and whatever else the consumer needs to preserve).
 *
 * @param items       candidate items
 * @param w, h        available viewport
 * @param getSize     size accessor
 * @param mergeSmall  builds the synthetic node from the folded items
 * @param minArea     min area in px² below which an item is "too small"
 *                    (default 16 = a 4×4 tile — below what a user can hit)
 */
declare function foldSmall<T>(items: T[], w: number, h: number, getSize: (it: T) => number, mergeSmall: (small: T[]) => T, minArea?: number): T[];
/**
 * Fold layout rects whose **short side** is below `minSide` px into one
 * synthetic node, and return the new item list to re-`squarify` (or `null` when
 * fewer than two qualify, i.e. nothing to do).
 *
 * Complements {@link foldSmall}, which folds by *area*: when one item dominates,
 * the remainder is squeezed into a thin strip and its cells come out as tall,
 * skinny slivers — enough area to escape the area fold, but too narrow to hover,
 * label, or read. This catches those by their rendered geometry. Runs *after*
 * squarify, since a cell's short side isn't known until it's laid out.
 */
declare function foldThin<T>(rects: Rect<T>[], minSide: number, mergeSmall: (small: T[]) => T): T[] | null;
/**
 * Squarify, but give a long tail of small items a legible 2D region instead of
 * unhoverable slivers — the "side-by-side remainder" layout.
 *
 * When a dominant sibling squeezes the rest into sub-`minSide` slivers, split
 * the container along its longer axis and lay the tail in its own block, over-
 * allocated to at least `remainderFrac` of that axis so its cells stay wide
 * enough to read. This trades exact area-proportionality (the tail is drawn
 * larger than its share, the dominant items smaller) for legibility — the
 * alternative to {@link foldThin}, which preserves area but hides the tail
 * behind one `(+n)` tile. Returns a plain squarify when no tail qualifies.
 */
declare function squarifyRemainder<T>(items: T[], x: number, y: number, w: number, h: number, getSize: (it: T) => number, minSide?: number, remainderFrac?: number): Rect<T>[];

export { type Rect, foldSmall, foldThin, squarify, squarifyRemainder };
