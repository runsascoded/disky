import { encodeScan, resolveScan } from './scanSlug'

/** The scan a `?d=` slug picks among `scans`: the one it spells in full (`2611040703`), else the newest it is a prefix
 *  of (`261104`: that day's latest); null for no slug, or one naming no scan (the page then follows the newest). */
export const pickedScan = (slug: string | undefined, scans: readonly string[]): string | null =>
  slug ? scans.find(s => encodeScan(s) === slug) ?? resolveScan(slug, scans) : null

/** What `?d=` holds for the scan picked in the menu: its compact id; nothing for the newest, which the page follows. */
export const pickedSlug = (id: string, scans: readonly string[]): string | undefined => (id === scans[0] ? undefined : encodeScan(id))

/** The scan `/staged` sizes at: the one picked, while the list still has it, else the newest (`scans` newest first). */
export const sizedScan = (scans: readonly string[], picked: string | null): string =>
  picked && scans.includes(picked) ? picked : scans[0] ?? ''

/** Why `/staged` dispatches nothing, dry or real, while it is sized at a scan (`sized`) other than the one a run reads
 *  (`run`, the newest): what its run controls say in place of acting, and what a dispatch is refused with (`plans.ts`
 *  `dispatchOptions`); null when they are the same scan. Everything a dispatch sends is read off the page's sizes: which
 *  rows are still staged, so can be checked at all; the buckets a run can be cut to; the machine it gets; the total its
 *  confirm names; its row's expected size. At another scan none of those is what the run reads, and the pick can arrive
 *  in a link (`?d=`). */
export const sizedAtOldScan = (sized: string, run: string): string | null =>
  sized === run ? null : `Sizes are shown at scan ${sized}. A run reads the newest scan, ${run}. Switch "sized at scan" back to ${run} to dispatch.`

/** What a real run on one scan recorded for a staged item (`deletion_bands`, not undone). Its amounts, if the run ended with a result;
 *  all zero if it didn't (killed, stopped, failed, still going, or by hand): that it deleted from the prefix, and not how much. */
export interface ScanDeletion { prefix: string; bytes: number; objects: number; gone: number }

/** `stats` (sizes at one scan) less what `bands` (real runs on that same scan: `scanDeletions`) deleted since, so an item re-staged
 *  after its run doesn't read as live until the next scan. Only that scan: a run read its listing, so what it deleted was there; a
 *  later scan holding the prefix is what's really there now, written back or not, and takes no discount.
 *  An item this empties leaves `/staged`'s real-delete selection, so every doubt keeps the item, whole.
 *  Only a run that ended with a result counts (`scanDeletions`: its job succeeded, so no delete went unanswered, no bucket
 *  errored and nothing stopped it). Any other run's band arrives with no amounts: it takes nothing off, and since what it
 *  deleted isn't known, no remainder is stated beside it.
 *  Exact or not at all. Every object at the scan deleted or already gone, by one counted run: the item is empty at the scan. One
 *  band, no objects gone (their bytes aren't recorded): the remainder, with its owners and classes dropped, since a band doesn't
 *  split by them. Anything else (partial runs may overlap, as a resumed run counts the first's deletions `gone`) stays whole.
 *  A band's `objects` are what the run attempted: on an XML run GCS answers `Deleted` for a generation that was overwritten since
 *  the scan, so such an object is live though counted here. The item still leaves `/staged`'s selection at this scan, which
 *  loses nothing: a run on this scan is held to its generations and can't delete that object either. The next scan lists it. */
export function withoutDeleted<S extends { bytes: number; objects: number; owners?: unknown; class_bytes?: unknown }>(stats: Record<string, S>, bands: readonly ScanDeletion[]): Record<string, S> {
  if (!bands.length) return stats
  const byPrefix = new Map<string, ScanDeletion[]>()
  for (const b of bands) byPrefix.set(b.prefix, [...(byPrefix.get(b.prefix) ?? []), b])
  const out: Record<string, S> = {}
  for (const [prefix, stat] of Object.entries(stats)) {
    const on = byPrefix.get(prefix)
    if (!on) { out[prefix] = stat; continue }
    if (on.some(b => (b.objects > 0 || b.gone > 0) && b.objects + b.gone >= stat.objects)) continue
    const [only] = on
    if (on.length > 1 || only.gone || !only.objects) { out[prefix] = stat; continue }
    const { owners: _owners, class_bytes: _cb, ...rest } = stat
    out[prefix] = { ...rest, bytes: stat.bytes - only.bytes, objects: stat.objects - only.objects } as S
  }
  return out
}
