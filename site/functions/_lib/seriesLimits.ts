/** The most match roots one `/api/series` request charts (one root read per
 * scan each); a filter with more shows no chart rather than a partial one.
 * Import-free: the client (`SizeOverTime.tsx`) imports it too. */
export const SERIES_MAX_PATHS = 24

/** The scans one scoped `/api/series` request reads. A path's closed versions live in the interval store's dyadic time
 *  blocks (`specs/interval-store.md` §2.4), so a read of all S scans decodes about a group per block, O(S) groups
 *  however few rows the path has. An aligned window of K scans intersects at most 2K blocks of its own, the
 *  log2(S/K) above them and the open segment, each in every tier: ~2K + log2(S/K) + 1 + 2·tiers blocks, at most two
 *  groups each (a path straddling a boundary). K = 16 at S = 1024 and 5 tiers: 49 blocks, ≤ 98 groups, a few seconds
 *  of Worker CPU at ~0.1 s a group against the ~30 s a request may use, independent of S. */
export const SERIES_CHUNK = 16
