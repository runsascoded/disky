export { Treemap } from './Treemap'
export type { CellCtx, CellDims, CellStyle, Tiling, TilingCtx, TreemapProps } from './Treemap'
export { foldSmall, foldThin, squarifyRemainder, squarify } from './squarify'
export type { Rect } from './squarify'
export { drawDust, DustHatch, dustLineCount, dustOffsets } from './DustHatch'
export type { DustHatchProps } from './DustHatch'
export { flattenPlaced, hitTest, isFolded, layoutCells } from './layout'
export type { FoldedNode, LayoutConfig, PlacedCell } from './layout'
export { foldedOf, resolveCellStyle } from './cellStyle'
export type { ResolvedStyle, StyleOpts } from './cellStyle'
export { TreemapCanvas } from './TreemapCanvas'
export type { CanvasHit, TreemapCanvasProps } from './TreemapCanvas'
export { useHoverPin } from './useHoverPin'
export type { HoverPin, HoverPinOpts } from './useHoverPin'
export { age01, ageDomain, ageFade, CONTAINER_BG, contrastEdge, DEFAULT_PALETTE, divergingColor, divergingInk, parseColor, slotColor } from './colors'
export type { AgeFadeOpts } from './colors'
export { dimUnmatched, filterNodes, parseQuery } from './filter'
export type { DimOpts, QueryOpts } from './filter'
// Grouped-outline overlay: stroke the union perimeter of same-key cells once
// (adjacent same-mark siblings → one region, not a doubled-line lattice).
export { OutlineOverlay } from './OutlineOverlay'
export type { OutlineOverlayProps } from './OutlineOverlay'
export { groupOutlines, groupRects, unionOutline } from './outlines'
export type { OutlineGroups, OutlinePath, OutlineSeg } from './outlines'
export { colorResolver, cssColor } from './cssColor'
// PNG export of the current treemap view (copy / download); `<Treemap exportable>`.
export { canvasToPngBlob, composeExport, copyPng, defaultExportFilename, downloadPng } from './exportImage'
export type { ComposeOptions, ExportContext, ExportKind, ExportOptions } from './exportImage'
// Diff widgets: a treemap + table pair over a compared tree (green grew / red
// shrank, Δ-by-area). Accessor/slot-based, disk-agnostic.
export { DiffTreemap, DiffTable, buildDiffTree, churn, fetchable, mapMinFrac, MIN_CELL_PX } from './diff'
export {
  GREW_GREEN,
  SHRANK_RED,
  NEUTRAL,
  UNCHANGED_GREY,
  UNCHANGED_SWATCH,
  TOUCHED_HATCH,
  deltaColor,
  deltaTextColor,
  statusColors,
} from './diff'
export type {
  DiffStatus,
  DiffNodeStatus,
  DiffKind,
  DiffAreaMode,
  DiffNode,
  DiffRecRow,
  DiffFlatRow,
  DiffIndexInfo,
  DiffInput,
  DiffSubtree,
  DiffTreemapProps,
  DiffMetric,
  DiffRowValues,
  DiffTableRow,
  DiffTableProps,
} from './diff'
