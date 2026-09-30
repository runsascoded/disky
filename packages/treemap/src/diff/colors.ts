import { CONTAINER_BG, divergingColor, parseColor } from '../colors'

/**
 * Diff polarity (git convention): green = added/grew, red = removed/shrank —
 * matching the added/removed row tints and the summary chips. This is a diff
 * view, not a cost alarm; a "growth = red" cost lens can be a toggle later.
 * `divergingColor` is red-positive, so negate on the way in.
 */
export const GREW_GREEN = '#3fb950'
export const SHRANK_RED = '#f85149'
export const NEUTRAL = '#8b949e'
/** Unchanged-bytes fill: translucent dark grey (not `divergingColor(0)`'s mid
 * grey, which left the light ink low-contrast) — the colored bands pop and
 * labels read. */
export const UNCHANGED_GREY = 'rgba(110, 118, 129, 0.28)'
/** touched (same bytes, mtime moved): the unchanged grey, hatched */
export const TOUCHED_HATCH = 'repeating-linear-gradient(45deg, rgba(255, 255, 255, 0.11) 0 3px, transparent 3px 9px)'
export const deltaColor = (t: number) => divergingColor(-t)

/** Legend swatch color: the translucent grey as it actually paints (over the
 * map's base), so it isn't invisible on the bar's own dark background. */
export const UNCHANGED_SWATCH = (() => {
  const [r, g, b, a] = parseColor(UNCHANGED_GREY) ?? [110, 118, 129, 0.28]
  const mix = (c: number, base: number) => Math.round(c * a + base * (1 - a))
  return `rgb(${mix(r, CONTAINER_BG[0])}, ${mix(g, CONTAINER_BG[1])}, ${mix(b, CONTAINER_BG[2])})`
})()
export const deltaTextColor = (d: number) => (d > 0 ? GREW_GREEN : d < 0 ? SHRANK_RED : NEUTRAL)

/** Row background tint by status (added/removed get a faint wash). */
export const statusColors = {
  added: { bg: 'rgba(46, 160, 67, 0.15)' },
  removed: { bg: 'rgba(248, 81, 73, 0.15)' },
  changed: { bg: 'transparent' },
  touched: { bg: 'transparent' },
  unchanged: { bg: 'transparent' },
}
