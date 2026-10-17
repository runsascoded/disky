import type { DateRange } from './Treemap'
import type { Meta } from './types'

/** Whether the access logs cover a top-level cell (a source bucket: a store unioned at ingest names its
 *  buckets as its first path segment). `meta.access.buckets` lists the covered ones; a scan without it
 *  (published before the field) is covered everywhere. Any other first segment, a folded `(+N)` cell
 *  included, is unlogged when the list exists: its reads, if any, are still shown, but a missing read
 *  is "not logged", never "no observed reads". */
export function readLogged(access: Meta['access'] | undefined): (bucket: string | undefined) => boolean {
  const buckets = access?.buckets
  if (!buckets) return () => true
  const set = new Set(buckets)
  return bucket => bucket != null && set.has(bucket)
}

/** The read lens's state of one cell: `read` at `t` ∈ [0, 1] on the access window (old → new),
 *  `never` (logged, no read observed since logging began) or `unlogged` (no logs cover it). */
export type ReadState = { kind: 'read', t: number } | { kind: 'never' } | { kind: 'unlogged' }

/** A cell's read state. A read on a one-day window (the window opens on a collector's first day) sits at
 *  the new end (`t` 1): there is no span to place it on, and it is a read, not a missing one. */
export function readState(
  a: number | null | undefined,
  logged: boolean,
  range: (DateRange & { t?: (d: number) => number }) | null | undefined,
): ReadState {
  if (a != null && range) return { kind: 'read', t: range.max > range.min ? (range.t ? range.t(a) : (a - range.min) / (range.max - range.min)) : 1 }
  return logged ? { kind: 'never' } : { kind: 'unlogged' }
}

/** The view's top-level cells the logs don't cover (names, in tree order): the "not logged" legend's list. */
export function unloggedOf(names: readonly string[], logged: (bucket: string) => boolean): string[] {
  return names.filter(n => !n.startsWith('(') && !logged(n))
}
