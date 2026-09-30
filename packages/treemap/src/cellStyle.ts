/**
 * Cell style resolution, shared by the DOM (`Treemap.cell`) and canvas
 * (`TreemapCanvas`) renderers so both paint a cell the same color.
 *
 * The fall-through — consumer `colorForCell` → folded default → container /
 * categorical default, then the `lens` transform, then the adaptive
 * `edgeContrast` half-stroke — is identical to what `cell()` did inline; it
 * just reads its inputs off a `PlacedCell` now.
 */
import { contrastEdge, hierColor } from './colors'
import type { CellStyle } from './Treemap'
import type { FoldedNode, PlacedCell } from './layout'

export interface StyleOpts<T> {
  colorForCell?: (n: T, path: T[], depth: number, ctx: { w: number; h: number; hasKids: boolean; fade: number }) => CellStyle | null | undefined
  lens?: (n: T, path: T[], depth: number, ctx: { w: number; h: number; hasKids: boolean; fade: number }, style: CellStyle) => CellStyle | null | undefined
  getLabel: (n: T) => string
  /** Top-level label → categorical slot color. */
  topLevelSlot: Map<string, string>
  defaultSlots: string[]
  dustTexture: boolean
  edgeContrast: boolean
  /** Hierarchical L1-macro / L2-micro hues + tinted container headers. */
  nestedHues?: boolean
  /** Background opacity at a nesting depth (the depth fade). */
  fadeAt: (d: number) => number
}

/**
 * The default (no `colorForCell`) fill for a placed cell: its top-level macro
 * hue, and — when `nested` — varied per L2 subtree with containers tinted so
 * directory headers carry their group color. Shared by both renderers so they
 * paint identically. The non-nested branch is byte-identical to the historical
 * inline logic (leaf = slot, container = neutral var).
 */
export function categoricalStyle<T>(
  path: T[],
  hasKids: boolean,
  getLabel: (n: T) => string,
  topLevelSlot: Map<string, string>,
  defaultSlots: string[],
  nested: boolean,
): CellStyle {
  const top = path[1] as T | undefined
  const base = (top ? topLevelSlot.get(getLabel(top)) : undefined) ?? defaultSlots[0]
  if (!nested) {
    return hasKids
      ? { bg: 'var(--dt-treemap-container-bg, #202024)', ink: 'var(--dt-treemap-ink, #d0d0d8)' }
      : { bg: base, ink: '#fff' }
  }
  const l2Key = path.length >= 3 ? getLabel(path[2]) : null
  const bg = hierColor(base, l2Key, hasKids)
  return hasKids ? { bg, ink: 'var(--dt-treemap-ink, #d0d0d8)' } : { bg, ink: '#fff' }
}

/** Resolved paint for one placed cell: its style plus the adaptive edge (if any). */
export interface ResolvedStyle {
  style: CellStyle
  /** Adaptive contrast half-stroke, when shared-tiling + `edgeContrast` and the
   * consumer pinned no `edge`; else null (falls back to the neutral gutter). */
  builtinEdge: string | null
}

/** Default width (px) of a `CellStyle.ring` emphasis ring when unspecified. */
export const DEFAULT_RING_WIDTH = 2

/** A normalized emphasis ring: color + width (px) + inset flag. */
export interface ResolvedRing {
  color: string
  width: number
  inset: boolean
}

/**
 * Normalize `CellStyle.ring` (a bare color string or `{ color, width?, inset? }`)
 * into a `ResolvedRing`, or `null` when there's no ring. Shared by both renderers
 * so the DOM box-shadow and the canvas stroke agree on width/inset.
 */
export function resolveRing(ring: CellStyle['ring']): ResolvedRing | null {
  if (!ring) return null
  if (typeof ring === 'string') return { color: ring, width: DEFAULT_RING_WIDTH, inset: true }
  if (!ring.color) return null
  return { color: ring.color, width: ring.width ?? DEFAULT_RING_WIDTH, inset: ring.inset ?? true }
}

export function resolveCellStyle<T>(cell: PlacedCell<T>, o: StyleOpts<T>): ResolvedStyle {
  const { folded, node, path, depth, w, h, hasKids, mode } = cell
  const fade = o.fadeAt(depth)
  const ctx = { w, h, hasKids, fade }

  const explicit = folded ? null : o.colorForCell?.(node as T, path, depth, ctx)
  let style: CellStyle
  if (explicit) {
    style = explicit
  } else if (folded) {
    // Dust wants a faint ground for the hatch; the flat fallback is a solid block.
    style = o.dustTexture
      ? { bg: 'var(--dt-treemap-folded-ground, rgba(120, 120, 135, 0.12))', ink: 'var(--dt-treemap-folded-ink, #d0d0d8)' }
      : { bg: 'var(--dt-treemap-folded, #4a4a52)', ink: 'var(--dt-treemap-folded-ink, #d0d0d8)' }
  } else {
    style = categoricalStyle(path, hasKids, o.getLabel, o.topLevelSlot, o.defaultSlots, o.nestedHues ?? false)
  }
  if (o.lens && !folded) {
    style = o.lens(node as T, path, depth, ctx, style) ?? style
  }

  let builtinEdge: string | null = null
  if (mode === 'shared' && o.edgeContrast && !style.edge) {
    builtinEdge = contrastEdge(style.bg, fade)
  }
  return { style, builtinEdge }
}

/** The `FoldedNode` behind a folded placed cell (its `.count`/`.children`). */
export function foldedOf<T>(cell: PlacedCell<T>): FoldedNode<T> | null {
  return cell.folded ? (cell.node as FoldedNode<T>) : null
}
