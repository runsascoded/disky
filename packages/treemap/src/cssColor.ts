import { parseColor } from './parseColor'

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

const probed = new Map<string, [number, number, number, number]>()
let probe: CanvasRenderingContext2D | null = null

/**
 * A resolved (no `var()`) CSS color as `[r, g, b, a]`: `parseColor`'s literals,
 * else whatever the browser itself understands (CSS Color 4, notably
 * `color-mix()`), read back off a 1px canvas once per distinct string. Null
 * for anything unparseable, or outside a browser.
 */
export function browserRgba(c: string): [number, number, number, number] | null {
  const p = parseColor(c) ?? probed.get(c)
  if (p) return p
  if (!c || c.includes('var(') || typeof CSS === 'undefined' || !CSS.supports('color', c)) return null
  if (!probe) {
    const canvas = document.createElement('canvas')
    canvas.width = canvas.height = 1
    probe = canvas.getContext('2d', { willReadFrequently: true })
  }
  if (!probe) return null
  probe.clearRect(0, 0, 1, 1)
  probe.fillStyle = c
  probe.fillRect(0, 0, 1, 1)
  const [r, g, b, a] = probe.getImageData(0, 0, 1, 1).data
  const out: [number, number, number, number] = [r, g, b, a / 255]
  probed.set(c, out)
  return out
}
