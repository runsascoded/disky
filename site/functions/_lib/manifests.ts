// A run store's manifests (`dt_cloud.append_runner`): the static name index's runs (`staticRuns.ts`) and the interval
// store's (`index.ts`) are each listed by immutable manifests under `<store>/<gen>/manifests/`: a scan's publish writes
// `<scan id>.json`, a merge's revision of it `<scan id>.m<NNN>.json`. Both readers take the newest the same way.

/** A manifest's name (`append_runner.MANIFEST` over `SCAN_ID`): `YYYY-MM-DD[THHMM][.mNNN].json`. */
export const MANIFEST_NAME = /^\d{4}-\d{2}-\d{2}(?:T\d{4})?(?:\.m\d{3})?\.json$/

/** The newest manifest among `names` (file names under `manifests/`; anything else ignored), or null: the greatest in
 *  code-point order, which is by scan, then revision (`<id>.m001.json` sorts after `<id>.json`, `.m` > `.j`, and before
 *  every later scan's, which is greater at a character before `.` or extends `<id>` with `T…`, `T` > `.`). */
export function newestManifest(names: Iterable<string>): string | null {
  let best: string | null = null
  for (const n of names) if (MANIFEST_NAME.test(n) && (best === null || n > best)) best = n
  return best
}
