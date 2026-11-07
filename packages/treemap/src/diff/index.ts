export { DiffTreemap, churn } from './DiffTreemap'
export { DiffTable } from './DiffTable'
export { buildDiffTree, fetchable, mapMinFrac, MIN_CELL_PX } from './tree'
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
} from './colors'
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
} from './types'
