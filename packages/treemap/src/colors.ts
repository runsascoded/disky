/**
 * Color primitives for the treemap (and disk-tree's widgets). All CSS-var-
 * friendly so consumers can theme without touching component code.
 */

import type { CellStyle } from './Treemap'

/**
 * The 8-slot categorical palette every widget defaults to. Shared so a node
 * colored slot-3 in the treemap is the same hue as its marker in the
 * scatter — cross-widget hue identity is the whole point of a fixed palette.
 */
export const DEFAULT_PALETTE = [
  'hsl(210 70% 55%)',
  'hsl(30 80% 55%)',
  'hsl(160 55% 45%)',
  'hsl(350 65% 55%)',
  'hsl(280 55% 55%)',
  'hsl(50 75% 55%)',
  'hsl(180 50% 45%)',
  'hsl(120 45% 50%)',
]

/**
 * The categorical color for top-level slot `i`. The first
 * {@link DEFAULT_PALETTE}`.length` are the fixed shared palette (so slot-3 is
 * the same hue across widgets); past that, golden-angle-spaced hues keep many
 * top-level dirs distinct instead of wrapping the 8 slots — a bucket root can
 * have dozens of children (ctbk: 31).
 */
export function slotColor(i: number, palette: string[] = DEFAULT_PALETTE): string {
  if (i < palette.length) return palette[i]
  return `hsl(${Math.round((i * 137.508) % 360)} 60% 52%)`
}

/**
 * Diverging red-negative / green-positive scale for delta coloring
 * (`Δbytes`, `Δcount`). Returns an rgb-triple string, always at max
 * saturation to keep signs unambiguous; the *intensity* comes from
 * background/opacity, not from the hue.
 *
 * `t` should be in `[-1, 1]`. Values outside are clamped.
 */
export function divergingColor(t: number): string {
  const c = Math.max(-1, Math.min(1, t))
  if (c === 0) return 'rgb(139, 148, 158)' // neutral gray
  if (c > 0) {
    // positive → red, brightening with magnitude (polarity is the caller's:
    // negate `t` for a green-positive/git-diff reading)
    const a = 0.15 + 0.85 * c
    return `rgba(248, 81, 73, ${a.toFixed(3)})`
  }
  const a = 0.15 + 0.85 * -c
  return `rgba(63, 185, 80, ${a.toFixed(3)})`
}

/**
 * Ink color that reads well on top of `divergingColor(t)`.
 * Roughly: switches to white once the delta is intense enough that the
 * background is dark enough to need it.
 */
export function divergingInk(t: number): string {
  return Math.abs(t) > 0.35 ? '#fff' : 'var(--dt-treemap-ink, #d0d0d8)'
}

/** The treemap map's opaque base — every cell's paint composites over this
 * (matches `--dt-treemap-container-bg`'s default). Consumers that theme the
 * base to a light ground pass their own to {@link contrastEdge}. */
export const CONTAINER_BG: [number, number, number] = [32, 32, 36]

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

/** Parse an `hsl()/hsla()` string to `[h(0-360), s(0-1), l(0-1)]`, else null. */
function parseHsl(c: string): [number, number, number] | null {
  const m = c.match(/^hsla?\(([^)]+)\)$/)
  if (!m) return null
  const [h, s, l] = m[1].replace(/\//g, ' ').split(/[\s,]+/).filter(Boolean)
  const out: [number, number, number] = [parseFloat(h), parseFloat(s) / 100, parseFloat(l) / 100]
  return out.some(n => Number.isNaN(n)) ? null : out
}

/** Deterministic [0, 1) from a string — a stable per-name jitter seed. */
function hashUnit(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (Math.imul(h, 31) + s.charCodeAt(i)) | 0
  return (((h % 1000) + 1000) % 1000) / 1000
}

export interface HierColorOpts {
  /** Max hue nudge (deg) between L2 sibling subtrees. Default 16. */
  hueJitter?: number
  /** Max lightness nudge between L2 sibling subtrees. Default 0.10. */
  lightJitter?: number
}

/**
 * Hierarchical cell fill: derive a cell's color from its top-level (L1) macro
 * hue `base`, nudged per its L2 ancestor (`l2Key`, `null` for L1 cells) so
 * sibling subtrees read as related-but-distinct "micro-hues". Containers
 * (`hasKids`) get a darker, desaturated tint of that hue so a directory header
 * carries its group color instead of a flat grey. `base` must be an `hsl()`
 * string (the categorical slots are); anything else is returned unchanged.
 */
export function hierColor(base: string, l2Key: string | null, hasKids: boolean, opts?: HierColorOpts): string {
  const p = parseHsl(base)
  if (!p) return base
  let [h, s, l] = p
  if (l2Key) {
    const j = 2 * hashUnit(l2Key) - 1 // [-1, 1)
    h = (((h + j * (opts?.hueJitter ?? 16)) % 360) + 360) % 360
    l = Math.max(0.22, Math.min(0.82, l + j * (opts?.lightJitter ?? 0.10)))
  }
  if (hasKids) { s *= 0.55; l *= 0.5 } // recessed, colored header
  return `hsl(${Math.round(h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`
}

/**
 * A high-contrast half-stroke for a shared-tiling cell whose face is `bg`:
 * dark on light faces, light on dark ones. Each cell paints its own half of
 * every boundary it shares, so a bright cell beside a dark one gets a dark
 * half-stroke against the neighbour's light one — one fixed color can't serve
 * both, which is why grey-on-grey borders vanish.
 *
 * The face that actually lands on screen is `bg` (at its own alpha) composited
 * over the opaque `base`, then faded toward `base` by the depth `fade` the
 * widget applies to the paint layer — this reproduces that. Returns `null`
 * when `bg` can't be parsed (a container `var()`, a gradient), so the caller
 * keeps its own neutral fallback for those.
 */
export function contrastEdge(bg: string | undefined, fade = 1, base = CONTAINER_BG): string | null {
  if (!bg) return null
  const p = parseColor(bg)
  if (!p) return null
  const [r, g, b, a] = p
  const eff = a * fade
  const lum =
    (0.2126 * r + 0.7152 * g + 0.0722 * b) * eff +
    (0.2126 * base[0] + 0.7152 * base[1] + 0.0722 * base[2]) * (1 - eff)
  return lum > 96 ? 'rgba(0, 0, 0, 0.55)' : 'rgba(255, 255, 255, 0.42)'
}

export interface AgeFadeOpts {
  /**
   * Max fraction of the panel color mixed in at `age01 = 1` — the fade
   * *floor* clamp, so the oldest cells stay legible. Default 0.72.
   */
  floor?: number
  /** Color faded toward. Default: the themable panel background var. */
  panel?: string
}

/**
 * Age lens: fade a resolved cell style toward the panel background —
 * "fading from memory". Older (`age01` → 1) ⇒ more faded.
 *
 * Uses `color-mix(in oklch, …)` so the fade is *perceptual lightness*, even
 * across hues (equal age reads as equal fade — an RGB-alpha ramp doesn't),
 * and works with CSS-var colors the JS can't parse. Ink fades at half the
 * background's rate, preserving label contrast on faded cells.
 *
 * Composable: takes and returns a `CellStyle`, so it stacks on the default
 * categorical palette or any `colorForCell` output — pass via the treemap's
 * `lens` slot rather than replacing the color logic.
 */
export function ageFade(style: CellStyle, age01: number, opts?: AgeFadeOpts): CellStyle {
  const t = Math.max(0, Math.min(1, age01))
  const floor = opts?.floor ?? 0.72
  const p = t * floor * 100
  if (p < 0.5 || !style.bg) return style
  const panel = opts?.panel ?? 'var(--dt-treemap-fade-panel, #1a1a1f)'
  const mix = (c: string, pct: number) => `color-mix(in oklch, ${c}, ${panel} ${pct.toFixed(0)}%)`
  return {
    ...style,
    bg: mix(style.bg, p),
    ...(style.ink && { ink: mix(style.ink, p / 2) }),
  }
}

/**
 * [oldest, newest] age extent over `nodes` (ignoring null/undefined ages) —
 * feed to `age01`. Returns `null` when no node has an age (lens should no-op).
 */
export function ageDomain<T>(nodes: T[], getAge: (n: T) => number | null | undefined): [number, number] | null {
  let lo = Infinity
  let hi = -Infinity
  for (const n of nodes) {
    const a = getAge(n)
    if (a == null) continue
    if (a < lo) lo = a
    if (a > hi) hi = a
  }
  return lo <= hi ? [lo, hi] : null
}

/**
 * Normalize an age (epoch seconds, days — any monotonic "older = smaller"
 * unit like mtime) into the `[0, 1]` fade domain: the *newest* value in the
 * domain maps to 0 (no fade), the oldest to 1. Degenerate domains map to 0.
 */
export function age01(age: number, [oldest, newest]: [number, number]): number {
  if (newest <= oldest) return 0
  return Math.max(0, Math.min(1, (newest - age) / (newest - oldest)))
}
