# Scans are not dates: sub-daily scan ids everywhere, a short `?d=` on every page

From the gcs session (2026-10-09), on Ryan's direction: "we should not be treating dates as PKs! we should have option to trigger (or switch cron to) multiple scans per day. cw already does this so if you added code … to bake in 'date == scan', that's wrong and should be removed. I'm fine to leave the params BC (e.g. date maps to latest scan for that date) and we can generalize that to where we always pick that latest scan matching a provided strptime-ish slug. param `date` should be shortened (have use-prms canonicalize it to a new shorter key+val str), and canonical repr should drop leading '20' from years."

He also said "some of the above should go US too": this spec covers the `cloud` (upstream) part. gcs's own job (`job/run.sh` keyed by `DATE`, `listing/$DATE/`) is the gcs session's, done there in parallel (§4).

## 1. A scan's identity is its scan id, never its date

A scan id is `YYYY-MM-DD` or `YYYY-MM-DDTHHMM` (cw's `SNAP_ID`, `job/cw-run.sh`: `$(date -u +%Y-%m-%dT%H%M)`). The path store already keys on it (`index_schema.date` is "snapshot date / scan id"; `api/diff.ts` `SCAN_RE`; `og/data.ts` `scanOfSel`). Audit `cloud` for code that assumes one scan per date, and fix it:

- **Static name search** (yours): `job/static-daily.sh DATE`, `deltas/<D>/`, `manifests/<D>.json`, `state/<D>/`, the "next scan" check (exit 3), `dt-cloud static-names daily …`, R2 keys, the catalog's per-date rows, `api/name-summary*` (`date`, `from`), `src/nameModel.ts` (`date` keys, `DAILY_SOURCES`, registry rows `{date, …}`), `_lib/nameSummaryStatic.ts`, `_lib/staticFilter.ts` ("every scan of its generation"). Each should take a scan id; two scans on one day are two deltas/manifests.
- Anything else that parses a scan with a date-only regex, or derives "the date's scan" by string equality, should use one shared resolver (§2).

## 2. A selection resolves to the latest scan matching its slug

One resolver, shared by Functions and the client: given a slug and the indexed scan ids, return the **latest scan whose id starts with the slug's expansion**:

| slug | matches |
|---|---|
| `261009` / `2026-10-09` | the latest `2026-10-09…` scan (the bare date if that's all there is) |
| `261009-12` / `2026-10-09T12` | the latest `2026-10-09T12…` scan |
| `261009-1200` / `2026-10-09T1200` | exactly that scan |

BC: existing links (`?d=2026-10-09`, `?date=2026-10-09`) keep working and mean "the latest scan of that day".

## 3. One short param, canonicalized by use-prms

The map's `?d=` already canonicalizes to the compact form (`261002`, `261002-1200`; `src/scan.ts` `encodeSel`/`decodeSel`). Every page that selects a scan uses the same key and codec:

- `/names?date=2026-10-09&name=gof` → `/names?d=261009&name=gof` (`from` → the same compact form, or the map's `d` look-back syntax if that fits). Decode accepts the old `date`/ISO forms and rewrites the URL to the canonical one.
- The canonical form drops the leading `20` of the year, everywhere a scan appears in a URL.
- The APIs can keep accepting ISO ids; the client sends whatever the resolver resolved.

## 4. gcs's side (FYI, done in the gcs session)

`job/run.sh` adopts cw's pattern: `SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}`, keyed through `listing/$SNAP_ID/`, `snapshots/$SNAP_ID/`, `index-sync … $SNAP_ID`, healthcheck / warm-cache / ch-ingest / index-gc. `static-daily.sh` is then called with the scan id. If any `dt-cloud` command it calls (`job submit-listing -d`, `path-index -d`, `healthcheck -d`, `ch-ingest -d`, `index-dir`) refuses a `T`-suffixed id, gcs will fix it as a `[cloud]` commit for you to pick, or tell you.

## Done when

- Two gcs scans on one day are both indexed, browsable on the map, and searchable on `/names`, each by its own id. The static daily append builds a delta per scan.
- `?d=261009` resolves to the day's latest scan on every page; old `date=` links redirect to `d=`.
- Tests cover the resolver table (§2) and the codec round-trip (§3).

## Remaining (handoff, 2026-10-09)

### Done
- **§2–3 (merged to `cloud`):** `site/src/scanSlug.ts` is the one resolver:
  - `resolveScan`, `latestScan`, `scanMatches`, `scanNeighbors` and `isScanId`;
  - the `?d=` codec: `encodeSel`/`decodeSel`, `selOf`, `canonicalSel`, `useScanSel`.
  - Slugs are dashless `YYMMDD[HH[MM]]`. Legacy `-HH`/`-HHMM`, `T` and ISO spellings still decode; the grammar is in `scanSlug.ts`.
  - A slug that matches nothing is a miss: the pages show "No scan matches X" with links to the nearest scans, and the APIs return 404 `{error}` (`functions/_lib/scanArg.ts`).
  - Python: `dt_cloud/scan_id.py` (`SCAN_ID`, `slug_prefix`, `resolve_slug`, `scan_slug`, `latest_scan`). `healthcheck -d` fails when its slug matches nothing.
- **Branch `scan-slug-3` (local, not merged):**
  - `85883570` / `fdfe3df2`: a date-only scan's exact slug is its **midnight**, `YYMMDD0000`.
    - Matching reads `YYYY-MM-DD` as `T0000` (`scanKey` / `scanUnder`; Python `scan_key`).
    - Every picker writes `exactPrefix(id)`, and `scanArg`'s D1 query matches the same way.
    - `check_order` refuses a date together with its `T0000`, so the key can't collide with a real scan.
    - This already fixes the `?f=tomat&d=26100900` repro: `26100900` matches the date-only scan, and the "← 10/9" link writes `2610090000`.
    - The gcs digest links to a date-only scan as `…0000`; the goldens differ only by that.
  - `fa977ed2`: a new refusal code, `term-too-common`. A heavy literal without `FILTER_STATIC_HEAVY` is `term-too-common`, not `scan-not-indexed` (`declined()` in `staticFilter.ts`).
  - `c1a59162`: filter refusals render inline, with no "view failed", no retry and no status, on the map, the Diff and the size chart (`apiError` / `refusalOf` in `filterCaps.ts`).
  - `be53b663`: a filtered view with no `d` on an indexed-only deployment opens on the newest scan the static index covers.
    - The covered list comes from `/api/filter-scans`; the pieces are `floatingScan`, `selectScan` and `pendingNote`.
    - Checked on dev only with every scan indexed: T1236 was already appended, so no note showed. The "newest scan unindexed" case is covered by tests only.

### Done on `scan-times` (2026-10-09)
Items 1–3 below are implemented; 4 isn't reproduced; 5 is verified.
- **A date-only scan's start (items 1 and 3).**
  - `path-index` writes `meta.started`: the earliest `started` across the listing `_SUCCESS.json` markers beside its `-l` globs (`dt_cloud/scan_started.py` `listing_started`).
  - `dt-cloud stamp-started ROOT [SCANS…]` back-stamps the existing metas. By default it stamps date-only ids; `-a` adds timed ids, and `-n` is a dry run. It skips a meta that already has `started`, or whose markers carry none (before 2026-09-08).
  - The key (`scanKey(id, times)`) is the start minute when it's known (`ScanTimes`, from `startKey`), else midnight. Midnight stays an alias, so `…0000` links keep resolving.
  - The key drives every resolver, `exactSlug`, the ordering (`sortScans`/`scanCmp`), the nearest-scan links and the size chart's x. Only date-only scans that share their day need a start (`timesNeeded`), and a start on another UTC day is ignored.
  - Client: `useScanTimes` reads those metas through the page's own `['meta', store, id]` query. Functions: `scanArg` resolves a slug among its day's scans with `_lib/scanTimes.ts`, as does the OG resolver. Python: `scan_key`, `scan_slug`, `latest_scan` and `resolve_slug` take `times`, and `healthcheck -d` reads the needed metas.
  - The dry run against `gs://oa-gcs-usage-dvx` covered 71 date-only scans: 32 to stamp (2026-09-08 through 10-09; 10/9 → `2026-10-09T04:31:04.542Z`, slug `2610090431`) and 39 with no `started` in their markers. **To apply (gcs session):** `dt-cloud stamp-started gs://oa-gcs-usage-dvx`.
- **The dropdown (item 2).**
  - `useScan` returns the scans sorted by time, plus `times` and a `label` (`scanLabeler`).
  - On a shared day, every row is a time: the date-only one shows its start ("12:31a"), or "(time unknown)" until the backfill runs. A single-scan day keeps "10/8".
  - The label is used by `ScanCombobox`, `ScanPicker`, both disambiguation strips, `NoScanMatch`, the diff labels and the chart tooltip.
  - The chart's pick and brush hand back the exact scan id under the point. A date-only id used to read as its whole day, so it picked the day's latest scan.
- **Checked on dev** (gcs + `scan-times`, `dev.gcs.oa.dev`, prod data without the backfill):
  - Unknown-time path, live: the 10/9 group reads "8:36a ✓ / (time unknown)", and picking the latter pins `?d=2610090000` and shows it. The strip reads "10/9 8:36a · 10/9 (time unknown)"; its buttons pin `2610090000`, and the newest floats.
  - The end pin writes `2610091236`, and the start pin writes `2610090000` (or `2610080000`). A chart pick on the date-only point pins `2610090000`. The 26100905 miss offers "← 10/9 (time unknown) · 10/9 8:36a →".
  - Known-time path: the start was injected into the page's query cache (a client-only fixture; prod metas aren't stamped). The rows read "8:36a / 12:31a", picking 12:31a pins `?d=2610090431`, the strip reads "10/9 8:36a · 10/9 12:31a", and the miss links read "← 10/9 12:31a" (`2610090431`).
  - Not checked live: the server's `d=` resolution by start (`scanArg`, OG). Unit tests cover both, and they work once the metas are stamped.
- **Item 4 (the "date=2026-10-09T0836" leak): not reproduced.** Covered on dev: the 8:36a entry, the end and start pins, both dropdowns, the brush and a chart pick. No request or URL carried `0836`, and there were no 4xx responses. No client path builds an id from local time: `dateOfX` is UTC, and picks now return exact ids.

### Done on `min-slug` (2026-10-09): the canonical slug is the shortest permanent one
Per Ryan: "once the hour, or day, have passed, we can know a permanent, unique, minimal dt-prefix slug for each scan".
- **Rule** (`scanSlug.ts` `minSlug`/`minPrefix`/`namesForGood`; Python `scan_id.min_slug`/`names_for_good`): the shortest of the scan's key's day (`261008`), hour (`26100904`) and minute (`2610091236`) that names only it and always will. A day or an hour qualifies once it has ended (UTC) and holds no other scan (by `scanUnder`, so a date-only scan's midnight alias counts against hour 00); a minute always does. A day holding a date-only scan with no known start has no hour slugs (that start could later land in any hour). Today's scans link by minute (or hour) and shorten once the period ends; the longer forms keep resolving, so shared links never break. Resolution is unchanged.
- **Where:** every picker/pin/brush/chart pick (`useScan`'s setters), the nearest-scan links (`hrefWithScan`, given the scan list), `/names` (`nameUrlParams`), the scan-runs pages' map links (`mapHref` + `useMinSlug`), and the digests (`scan_slugs` from each window's metas; rows carry `slug`, compared-out). The page's own URL canonicalizes in place (`useCanonicalSel`, a replace): a pinned endpoint that already names one scan for good becomes that scan's `minPrefix` (never longer) — `?d=2610080000` → `261008`; a day or hour that is still open or holds several scans stays as written (`?d=261009` today is still "the day's latest"). `canonicalSel` takes the scan list optionally; the OG card's view key (a pure route) stays spelling-only. Labels are unchanged (they keep showing the minute).
- **Known gap:** "ended" is the period's UTC end, but a scan is listed only once indexed. A scan started before the end and indexed after it can join a period whose slug already shortened (e.g. a day's only scan gets `261008` after midnight, then a 23:50Z scan of 10/8 lands at 02:00): `261008` then names the newer one. The cadences in use make this rare (never two scans in an hour; a day only across midnight). If it matters, add a settle margin (period end + the longest scan-to-index lag) in `namesForGood`/`names_for_good`.

### Still open
- `/names` (`NamePage`) keeps the midnight form for a date-only scan. It resolves, but isn't labelled by the start.

### Throwaway worktrees and branches to delete
`wt/scan-slug-gcs` (`scan-slug-gcs`), `wt/scan-slug-2-gcs` (`scan-slug-2-gcs`) and `wt/scan-slug-3-gcs` (`scan-slug-3-gcs`), plus the merged `wt/scan-slug` and `wt/scan-slug-2`. The current dev deploy is `gcs-dev` → `78d13f6a` (gcs + `scan-times`; its throwaway worktree and branch are already deleted).
