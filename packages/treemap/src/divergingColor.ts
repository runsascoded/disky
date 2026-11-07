/** Diverging delta colors. Positive is red; negate for green-positive diffs. */
export function divergingColor(t: number): string {
  const c = Math.max(-1, Math.min(1, t))
  if (c === 0) return 'rgb(139, 148, 158)'
  const a = 0.15 + 0.85 * Math.abs(c)
  return c > 0 ? `rgba(248, 81, 73, ${a.toFixed(3)})` : `rgba(63, 185, 80, ${a.toFixed(3)})`
}

/** Ink contrasting with a delta fill. */
export function divergingInk(t: number): string {
  return Math.abs(t) > 0.35 ? '#fff' : 'var(--dt-treemap-ink, #d0d0d8)'
}
