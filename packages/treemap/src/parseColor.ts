/** DOM-free CSS color parsing, shared by browser widgets and edge SVG rendering. */
const parseCache = new Map<string, [number, number, number, number] | null>()

/** h,s,l in [0,360),[0,1],[0,1] → [r,g,b] 0-255. */
function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const c = (1 - Math.abs(2 * l - 1)) * s
  const hp = ((h % 360) + 360) % 360 / 60
  const x = c * (1 - Math.abs((hp % 2) - 1))
  const [r1, g1, b1] =
    hp < 1 ? [c, x, 0] : hp < 2 ? [x, c, 0] : hp < 3 ? [0, c, x]
    : hp < 4 ? [0, x, c] : hp < 5 ? [x, 0, c] : [c, 0, x]
  const m = l - c / 2
  return [Math.round((r1 + m) * 255), Math.round((g1 + m) * 255), Math.round((b1 + m) * 255)]
}

/**
 * Parse a CSS color to `[r, g, b, a]` (channels 0–255, alpha 0–1). Handles
 * `#rgb` / `#rrggbb` / `#rrggbbaa`, `rgb()`/`rgba()`, and `hsl()`/`hsla()`
 * (comma- or space-separated, optional `/ alpha`). Returns `null` for anything
 * else — `var()`, `color-mix()`, gradients, named colors — so callers can fall
 * back rather than mis-render a grey. Memoized: a handful of distinct strings
 * serve thousands of cells.
 */
export function parseColor(c: string): [number, number, number, number] | null {
  if (parseCache.has(c)) return parseCache.get(c)!
  let out: [number, number, number, number] | null = null
  const rgb = c.match(/^rgba?\(([^)]+)\)$/)
  const hsl = c.match(/^hsla?\(([^)]+)\)$/)
  if (rgb) {
    const [r, g, b, a = '1'] = rgb[1].replace(/\//g, ' ').split(/[\s,]+/).filter(Boolean)
    out = [+r, +g, +b, +a]
  } else if (hsl) {
    const [h, s, l, a = '1'] = hsl[1].replace(/\//g, ' ').split(/[\s,]+/).filter(Boolean)
    const [r, g, b] = hslToRgb(parseFloat(h), parseFloat(s) / 100, parseFloat(l) / 100)
    out = [r, g, b, parseFloat(a)]
  } else if (/^#[0-9a-fA-F]{3,8}$/.test(c)) {
    const h = c.slice(1)
    const x = h.length === 3 || h.length === 4 ? h.split('').map(d => d + d).join('') : h
    out = [
      parseInt(x.slice(0, 2), 16), parseInt(x.slice(2, 4), 16), parseInt(x.slice(4, 6), 16),
      x.length >= 8 ? parseInt(x.slice(6, 8), 16) / 255 : 1,
    ]
  }
  if (out && out.some(n => Number.isNaN(n))) out = null
  parseCache.set(c, out)
  return out
}
