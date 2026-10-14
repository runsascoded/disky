/** The treemap's renderer-agnostic types: a cell's paint and the tiling
 * mode. A plain module (no JSX), so DOM-free consumers (`layout`, `svg`, a
 * Worker) can import them without `Treemap.tsx`. */

export type Tiling = 'gaps' | 'shared'

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
