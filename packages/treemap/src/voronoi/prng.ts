/**
 * Deterministic PRNG for the Voronoi layout.
 *
 * `d3-voronoi-treemap` seeds its initial site positions from `Math.random` by
 * default, so the same data lays out differently on every render — cells would
 * visibly jump on any state change, and tests couldn't assert geometry. A
 * seeded generator makes the layout a pure function of (data, clip, seed).
 *
 * mulberry32: 32-bit state, uniform enough for site seeding, and short enough
 * to keep the subpath free of another dependency.
 */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** Stable 32-bit hash of a string — turns a node path into a layout seed. */
export function hashSeed(s: string): number {
  let h = 2166136261 >>> 0
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619) >>> 0
  }
  return h >>> 0
}
