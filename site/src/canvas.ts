// The pixel budget the map's views (`/api/subtree`, `/api/diff`) are requested
// at. It's part of their cache keys, and the scan job pre-warms a fixed set of
// widths (`cloud/src/dt_cloud/warm.py` WIDTHS) — so a window snaps UP to the
// nearest warmed width (a little more detail than it needs, already cached)
// rather than to its own 128-px step, which mostly missed. Past the widest
// warmed width it keeps the 128-px step.
export const WARMED_WIDTHS = [512, 1280, 1536, 1792, 1920] as const

export function canvasWidth(innerWidth: number): number {
  return WARMED_WIDTHS.find(w => w >= innerWidth) ?? Math.ceil(innerWidth / 128) * 128
}
