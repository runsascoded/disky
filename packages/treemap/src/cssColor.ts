/**
 * Canvas `fillStyle` / `strokeStyle` silently ignore a CSS custom-property
 * reference (`var(--x)` paints black), yet a themed consumer names its colors
 * exactly that way. Resolve `var(--name[, fallback])` against an element's
 * computed style; any other color string passes through untouched.
 */
export function cssColor(el: Element, color: string): string {
  const m = /^\s*var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)\s*$/.exec(color)
  if (!m) return color
  const v = getComputedStyle(el).getPropertyValue(m[1]).trim()
  return v || (m[2] ?? '').trim() || color
}

/**
 * A memoizing resolver for one paint pass: thousands of cells reuse a handful
 * of `var()` strings, and `getComputedStyle` per cell is the kind of cost the
 * canvas renderer exists to avoid. Make a fresh one per pass so a theme
 * change is picked up on the next paint.
 */
export function colorResolver(el: Element): (color: string) => string {
  const cache = new Map<string, string>()
  return (color: string) => {
    if (!color.includes('var(')) return color
    const cached = cache.get(color)
    if (cached !== undefined) return cached
    let resolved = cssColor(el, color)
    for (let i = 0; i < 8 && resolved.includes('var('); i++) {
      const next: string = resolved.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)/g, (_m, name: string, fallback?: string) => getComputedStyle(el).getPropertyValue(name).trim() || fallback?.trim() || `var(${name})`)
      if (next === resolved) break
      resolved = next
    }
    cache.set(color, resolved)
    return resolved
  }
}
