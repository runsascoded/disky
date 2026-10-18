/**
 * Parse a CSS color to `[r, g, b, a]` (channels 0–255, alpha 0–1). Handles
 * `#rgb` / `#rrggbb` / `#rrggbbaa`, `rgb()`/`rgba()`, and `hsl()`/`hsla()`
 * (comma- or space-separated, optional `/ alpha`). Returns `null` for anything
 * else — `var()`, `color-mix()`, gradients, named colors — so callers can fall
 * back rather than mis-render a grey. Memoized: a handful of distinct strings
 * serve thousands of cells.
 */
declare function parseColor(c: string): [number, number, number, number] | null;

export { parseColor };
