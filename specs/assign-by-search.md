# Assign by search: one ledger rule for every match of a filter

Status: **parked** (2026-10-09). Ryan chose a muted over-cap bar instead (disabled assign/stage with a tooltip; agents can bulk-assign via the API), and asked for this design to stay specced but unbuilt. A working implementation sits unmerged on branch `assign-search` (merge with `cloud` resolved at `6dd930df`, unverified); no migration applied. Revive it only if large bulk assignments become a real need.

## Problem

"Assign all of nemotron": on gcs prod `?f=nemotron` has 70,450 match roots, and their cover (`/api/filter-cover`, the fewest exact prefixes) is still 70,390 folders and files, mostly `nemotron_cc*` folders under `tokenized/`, `normalized/` and `raw/` whose parents also hold non-matches. The bulk bar refuses past 50,000 items, and even under the cap a gesture would write 70k ledger rows that cover only the matches that exist *today*.

## Design: one rule, not 70k rows

A **search action** is one ledger row whose scope is a filter query: "every path matching `nemotron` (under `<scope>`) belongs to X". It is stored once and resolved per path by a predicate, so:

- it is one action in the audit log, one row on `/assignments`, one thing to retract;
- it covers matches that appear in later scans (a new `nemotron_cc_v3/` is X's the day it is scanned) — the reason it is a rule and not a job that writes 70k prefix rows. A job would be a snapshot that silently stops covering new matches, and would bury the audit log.

The expansion into match roots happens only where bytes are counted (server totals, the lens), per scan, in the cached computation, never in the ledger.

### The query

Same matching as `?f=`, the `simple` syntax: each term is a lowercase substring (or a `*` in-segment glob) of the full index path (`bucket/dir/…`); `a b` = AND, `a|b` = OR. Accepted for a rule:

- positive terms only: no exclusions (`-x`) and no regex. Both are refused because they are not *monotone* along a path (`nemotron -tmp` matches `a/nemotron` but not `a/nemotron/tmp`), and a non-monotone rule has holes inside its match roots: it would not be "a set of subtrees", which every fold below relies on.
- every term at least `MIN_TERM` (3) literal characters in a row, with no lone-literal exemption: a rule is fleet-wide and permanent, so `ab` is never a rule.
- the stored text is canonical: the parsed AST printed back (`canonQuery`), so `Nemotron`, `  nemotron ` and `nemotron` are one rule; `parse(canonQuery(ast))` equals `ast` (tested).

Monotone means: once a path matches, every path below it does. So a rule's coverage is exactly the subtrees of its **match roots** — the outermost matching paths under its scope (`filter.ts` `matchRoots`, the first-hit roots `?f=` shows). For a path `X`: `X` is covered by rule `S` ⇔ `X` is at or under `S.scope` and `S.query` matches `X`'s full path.

### The scope: what a rule binds

A rule binds the view it was made in, with the view's semantics — as far as a scope can be bound exactly:

- **the path** — `pattern`: the store root (`gs://`, from the root view) or a folder `gs://b/dir/` (a drilled view). Only matches inside it. If the scope's own path matches, the whole scope is the one match root (the filter view's rule).
- **the query** — `query`, as above.
- **an owner lens / pool (`?u=`, `o=owned|unowned|!a,b`)** — *excluded, refused*. Those scopes are ledger-applied: "bytes owned by U" already folds the ledger, so an owner rule scoped by ownership is circular (the rule changes the set it is defined over; "assign every unowned nemotron path to bob" stops covering the paths it just assigned). Binding the *scan's* attribution instead (the raw `usr` column, no ledger) is not circular, but see the next point.
- **storage class (`cls=`) and scan attribution (`usr`)** — *not bound yet*. Both are per-object properties, not path properties: such a rule covers part of a folder's bytes, so it is no longer a set of subtrees. The table's owner column, the map overlay and the audit log (which answer per path) couldn't say who owns a folder, and the totals would need per-root class × user splits the index doesn't keep (it has per-class bytes and per-user bytes, not per-user-per-class, and no per-class objects). It is possible (the bands become class/user slices) but is a different fold; deferred until wanted.

The UI follows: the filtered bulk bar only appears for an unscoped filter (`/api/filter-cover` refuses a lens, a pool or a class: a scoped match isn't a prefix), so the rule is only offered where its binding is exact. The server refuses a regex, an exclusion, a short term or a non-`simple` syntax.

## Ownership semantics

Unchanged rule (`ownership-ledger-nesting`): **for each path, the newest live action that covers it wins** — ties on `ts` broken by action id. There is no specificity tie-break: recency (ts, id) is a total order, which is the only rule `computeOwners`, `assignmentOf` and the lens have ever used.

The candidates for a path `X` are now:

1. folder (`prefix`) actions on `X`'s ancestors-or-self (for an object `X`: its strict ancestors),
2. an `object` action on `X` itself (when `X` is an object),
3. `search` actions that cover `X` (at or under the scope, the query matches `X`'s full path).

Equivalently: a search action behaves, on every scan, exactly like a set of folder/object actions at its match roots, all carrying the search action's `(ts, id)`. Every fold implements it that way.

Examples (bucket `b`; `tokenized/` holds `nemotron_cc/` and `fineweb/`):

| ledger (oldest first) | `b/tokenized/nemotron_cc/x` | `b/tokenized/fineweb/y` |
| --- | --- | --- |
| folder `b/tokenized/` → ann; search `nemotron` → bob | bob | ann |
| search `nemotron` → bob; folder `b/tokenized/` → ann | ann (a newer broad folder is a bulk override, as today) | ann |
| search `nemotron` → bob; folder `b/tokenized/nemotron_cc/` → kim | kim (a later folder carves an exception) | — |
| search `nemotron` → bob; object `b/tokenized/nemotron_cc/x` → kim | kim (a later object carves one file) | — |
| object `…/nemotron_cc/x` → kim; search `nemotron` → bob | bob (the newer rule repaints the older object) | — |
| search `nemotron` → bob; search `nemotron_cc` → lee | lee (a narrower newer rule) | — |
| search `nemotron_cc` → lee; search `nemotron` → bob | bob (newer broad rule overrides) | — |
| search `nemotron` (scope `b/raw/`) → bob | — (outside the scope) | — |
| search `nemotron` → bob; search `nemotron` → null | unowned (a newer release) | — |

Retraction is a tombstone on the rule's `owner_prefixes` row, as for any action: the paths fall back to whatever is the newest remaining candidate.

Ties: the handler stamps whole seconds, so actions posted in one second share a `ts` and the action id orders them (the later POST wins), as it always has.

A search row on its own key supersedes an older search row with the same scope and canonical query (`superseded`). In the audit log a folder or object row is `overridden` by a newer search rule that covers it whole (the rule's query matches the row's own path, inside its scope); a search rule is `overridden` by a newer folder at or above its scope. A rule partially repainted by later actions stays `live` (it still decides the rest). A rule wholly inside a newer, broader rule (`nemotron_cc` under a newer `nemotron`) also reads `live`: deciding query implication isn't worth it for a status label; the folds resolve it exactly.

## Storage (D1, additive)

The gcs lineage only (cw's D1 has no ledger tables; nothing to add there):

```sql
-- 0042_search_actions.sql (gcs): a search action's filter query (specs/assign-by-search.md).
ALTER TABLE actions ADD COLUMN query TEXT;
ALTER TABLE owner_prefixes ADD COLUMN query TEXT;
```

A search action is `query IS NOT NULL` (and `kind` NULL). `actions.pattern` is the scope; `owner_prefixes.prefix` is the rule's **key** `<scope>?q=<query>` (e.g. `gs://?q=nemotron`, `gs://b/raw/?q=nemotron`). The key is inert for any reader that predates this: it is no path's ancestor or self (no index path holds `?q=` after a scope, and the store-root key has no bucket), so rolling the code back leaves the rule dormant instead of turning it into a whole-bucket assignment (the 2 PB incident in `ownership-ledger-nesting`). This is also why the rule doesn't reuse `kind`: `0041_items_kind.sql`'s `CHECK (kind IS NULL OR kind = 'object')` can't be widened once applied without rebuilding `actions` (referenced by `owner_prefixes`, `d1-enforces-foreign-keys`), and the `query` column needs no change to it. The wire and every reader say `kind: 'search'` (`CASE WHEN query IS NOT NULL`).

No table rebuild, no new table. The code reads the column unconditionally, so the migration lands before the deploy.

## Server totals (`computeOwners`, `owner_totals`)

Totals stay a per-(scan, ledger head) cached computation. For a scan, each live search rule is expanded into its match roots on that scan:

1. **Match roots**: `allMatchRoots(env, {date, path: scope, query})` — the same reader `/api/filter-cover` uses: the static name index (cheap for light terms, the drilldown for heavy ones), else the search. Its `coverage` / `rollup` say whether the list is complete.
2. **Aggregates**: each root's row on the `path` tier (the same `readAsks` the totals already make for every folder action), per-user and per-class, plus its kind (file or folder) — as point lookups, or one range per folder holding 16+ roots. Memoized per (scan, rule): see Read cost.
3. **Fold**: the roots become virtual folder / object rows carrying the rule's `(ts, id)`, deduplicated against real rows on the same key by recency, and the existing band fold runs over the union — so the per-user numbers are exact by the same argument as before (bands partition the estate).
4. **Output**: the totals' `assignments` (what the user lens folds, stored in `owner_totals.claims`) can't carry 70k rows (D1 values cap at 2 MB). A virtual root with nothing else of the ledger at or under it (*clean*) is merged with the rule's other clean roots under the same parent folder into one **group** row (`kind: 'search'`, `prefix` = the parent folder, `query`, `scope`, `roots`, summed bytes / per-user splits). A clean root's ancestors are its parent's, so every root in a group shares its trie parent and its effective owner, and a path inside a group is covered iff the rule's query matches it: the lens resolves it from the group with the predicate. Roots that hold other actions (an exception carved inside) stay individual folder / object rows with the rule's `(ts, id)` and `via` = the rule's key. For nemotron, one group per parent folder holding matches (the cover's per-bucket items suggest low thousands, mostly one root each, not measured) instead of 70k rows.

Each body also carries `searches: [{key, scope, query, owner, roots, bytes, objects, complete, reason?}]` — one line per rule. `MANIFEST_VERSION` 4. `/api/assignments` counts a rule's groups and expanded roots under the rule's key.

**A scan the static index doesn't cover (yet)**: `allMatchRoots` falls back to the search, which may be thresholded (`approximate`) or budget-cut (`partial`), or a heavy literal may only roll up (`rollup`). The rule then counts the roots it found (a lower bound), its `searches[]` entry says `complete: false` with the reason, and the body is reused for at most `SEARCH_RETRY_S` (1 h) before a recompute — so the day's static index landing makes the next read exact. Ownership *resolution* (the table's owner column, the map overlay, the audit log) never depends on this: it is the predicate, exact on every scan.

## Read cost (the trade-off)

A rule is cheap to store and exact to resolve; its cost is at read time, where it has to be expanded per scan. Where each cost lands:

**Per view (the map, the table, the audit log): no expansion.** The client resolver (`assignmentOf`) and the overlay test each live rule's predicate against a path: O(rules × path length) per drawn node, CPU only, no I/O. The overlay walks the whole drawn tree while any rule is live (a rule can cover any path); the drawn tree is pixel-bounded, so that is the same order as drawing it. The audit log compiles the live rules once per page. With tens of rules this is noise; with thousands it would want an index (one trigram pass over the path against all rules' literals).

**Per scan × ledger head (owner totals, `/users`, `/assignments`, the user lens): the expansion.** For each live rule on a scan: (1) list its match roots — `allMatchRoots`, the same reader `/api/filter-cover` uses (static index; drilldown for heavy literals); (2) one aggregate per root, per user and class, from the `path` tier. The expansion depends on (scan, rule) only, never on the ledger head, so it is memoized per (scan, rule) (`ruleMemo`, 64 entries per isolate) and a new assignment anywhere re-folds without re-expanding. Roots that share a parent folder (16 or more) are read as one `under` range at their depth, not as point lookups; a rule whose lookups need more than `RULE_GROUPS` (4,000) row groups counts nothing on that scan and says so.

**Measured on gcs prod, scan 2026-10-09T1236, `nemotron` store-wide (read-only GETs, `/api/filter-cover`):**

| | roots | bytes | listing the roots | the cover's lookups |
| --- | --- | --- | --- | --- |
| whole store | 70,450 | 718 TB (6.58M objects) | 32 ms | 22.5 s (and then discarded: 70,390 items > 50K) |
| marin-us-central1 | 58,994 | 199 TB | 22 ms | 6.4 s |
| marin-us-east5 | 5,580 | 136 TB | 53 ms | 4.2 s |
| marin-eu-west4 | 2,449 | 174 TB | 238 ms | 3.6 s |
| marin-us-central2 | 2,419 | 121 TB | 191 ms | 6.1 s |
| marin-us-west4 | 742 | 72 TB | 28 ms | 1.1 s |
| marin-us-east1 | 266 | 17 TB | 152 ms | 1.2 s |

- Listing match roots is ~free (the static index). The lookups are the cost: the cover's are a fair proxy for the expansion's (one lookup round per depth over the roots' parents, plus kind lookups for one-object roots), ~0.3–2.5 ms per root.
- 54,965 of the 70,450 roots (78%) sit in one folder, `marin-us-central1/ot-agent`, and hold 3.1 GB (0.0004% of the matched bytes). Those are exactly what the sibling range read is for: one `under` rectangle instead of 55K point asks. Not measured on prod (no deploy); the fixture test pins that a range read sizes the roots exactly.
- Expected order of the expansion with the range reads: a few seconds per (scan, rule), once per isolate, then nothing per ledger change. Measure with `computed.ms` / `computed.groups` in the `owner_totals` body after the first deploy.

**A scan the static index doesn't cover yet**: the roots come from the thresholded search (`approximate`), budget-cut (`partial`) or a heavy literal's roll-up (`rollup`). The rule counts what was found (a lower bound), its `searches[]` entry says `complete: false` with the reason, and a body holding such a rule is reused for at most `SEARCH_RETRY_S` (1 h) — so the day's static index landing makes the next read exact. Resolution never depends on this.

**If rules multiply (the plan, not built):**
1. Persist expansions per (scan, rule) beside the scan (an R2 object, or a chunked D1 table `rule_roots(scan, rule, part, body)` — one D1 value caps at 2 MB, nemotron's roots alone are ~5 MB as JSON), so a cold isolate doesn't redo them.
2. Expand in the daily job instead of the Worker: after the static index is built, the job expands every live rule against the new scan (DuckDB over the `path` tier — a scan, not lookups) and writes (1). The Worker's on-demand expansion stays the fallback for a rule made today.
3. A soft cap: past ~50 live rules, `/api/actions` refuses a new rule with a pointer to `/assignments` (retract or merge rules first). Not built: one rule today.
4. Many tiny roots under one folder (`ot-agent`) could be one "partial folder" node in the fold (its matching children summed, read as a range) — the groups already do this at fold time; doing it at expansion time shrinks (1) too.

## Staging: not by search (yet)

Recommendation: don't add "stage all matches" as a rule. Deletion is a one-time act on a reviewed set of objects, not a standing policy: a staged search would delete next week's new `nemotron_*` too, which nobody reviewed. If wanted later, the safe shape is a *snapshot*: a plan item `kind: 'search'` that the sweep's manifest expands into its match roots on the plan's pinned `as_of` scan (generation-pinned like every item), whose review shows that expansion, and that never re-expands on a later scan. That needs the manifest, plan review, digest and executor to learn a third item kind; it isn't trivially safe, so it is deferred. Assign is built.

## UI

- The filtered bulk bar, for an assignable query (positive `simple` terms), offers **"assign all N matches as a rule"** (to the user in the assign box, else the viewer) — always, beside the listed-matches assign. It posts one action `{kind: 'search', query, pattern: <scope>, owner}` after a confirm that states the rule in words, today's count (roots, bytes), "and every new match in later scans", and that newer folder / file / rule actions still carve exceptions.
- When the cover is over its cap (or otherwise can't be listed), the bar shows the reason in plain words beside a **disabled** assign and stage, with the rule action next to them.
- `/assignments` audit log: a rule row reads "every path matching `nemotron`" (under the scope, when not the root), links to the map with `?f=` set.
- `/api/assignments` (the assigner × assignee matrix) counts a rule's groups and individual roots under its key.

## Also fixed: the over-cap cover

`/api/filter-cover` for `nemotron` at the root ran its full cover (folder rounds, then up to 24 kind lookups of one-object roots) and then dropped the result because it held 70,390 items. `coverSet` now takes `itemsMax`: once the folder rounds fix the item count (one per outermost full folder, one per root under none), it returns `{items: [], over: count}` without the kind lookups, which never change the count. Same answer and reason text (no `COVER_V` bump); the saving is the kind phase only — the folder rounds are still paid (22.5 s total on 10-09; the split between the two phases isn't instrumented). The other prod findings are in the hand-back (not built here).

## Tests

- `functions/_lib/assignSearch.test.ts`: the canonical query (round trip, refusals); the spec table through every fold — the client resolver, `computeOwners` per-user totals and the user lens at every folder — each against a brute-force per-object resolver; groups vs individual roots and the `searches` summary; 300 seeded random ledgers of folders, objects and rules (ties on `ts` included), every fold equal to brute force; the audit log's statuses with rules (over a local D1, `sqliteD1('cw')` + `GCS_LEDGER`); `POST /api/actions` with a rule (stored rows, GET, refusals); the bulk bar's payload posted through the real handler.
- `functions/_lib/pathStore.test.ts`: `ownerTotals` over the fixture generations with a rule in the ledger — a one-root rule on a scan without a search index (flagged `complete: false` with the reason), and a 372-root rule read as one sibling range, sized equal to a direct read of those rows.
- `functions/_lib/cover.test.ts`: past `itemsMax` the kind lookups never run.
- `src/BulkBar.test.ts`, `src/searchRules.test.ts`: the rule action's copy, its absence for an exclusion, the over-cap bar (reason, disabled assign / stage, the rule), `ruleOffer` / `rulePost` / `ruleText` / `ruleLink`.

Mutation checks (each applied alone, the tests above run, reverted; `tmp/mutate.py`): all caught —
- the client resolver ignoring rules;
- a rule's virtual root beating a newer real row on the same key;
- groups merged across parent folders;
- the lens ignoring groups;
- the audit log never marking a row overridden by a rule;
- a rule accepting exclusions;
- a rule ignoring its scope;
- the bar offering a rule for an exclusion;
- roots holding other actions merged into groups;
- the cover ignoring `itemsMax`.

Not caught, by design: pointing the sibling range read at the wrong folder — `readAsks` keeps rows by path, so an off rectangle only reads more (or, on a real store, would lose roots and flag the rule incomplete); the fixture's 2048-row groups mix depths and hide it.

Not verified in a browser: no dev deploy (dev is shared), and a local stack would need the ledger D1; the bar's markup is pinned by the render tests.

## Mode 2: persisted expansions on R2 (parked, not built)

Rules stay one D1 row each (source of truth); a per-scan job materializes each live rule's match roots so no request ever expands one.

- **When**: a stage after the scan's static name index (light + drill) is built (`dt-cloud static-names runs add`, or its own command `run.sh` calls next) — expansion reads only that scan's index.
- **Layout**: one Parquet per (scan, rule), `rules/<gen>/<scan-id>/<rule-id>.parquet`, rows `(root, usr, b, o)` sorted by `root` (the per-root per-user aggregates owner totals need), plus `rules/<gen>/<scan-id>/manifest.json` written last (rule ids, row counts, md5s). New keys only; a rule edit is a new rule id.
- **Readers**: owner totals and the user lens read the persisted file when the scan's manifest lists the rule; otherwise they expand live and report `complete:false` until the job catches up. Views never need it (rule predicates test drawn nodes directly).
- **Frozen variant**: a rule flagged `frozen` covers exactly the roots of the scan it was made on — its expansion is written once at creation (`rules/<gen>/frozen/<rule-id>.parquet`) and resolved from that file on every scan, for reviewable one-shot bulk assignments.
- **Cost**: nemotron's ~70k roots × a few users ≈ a few MB per scan per rule (≈ $0.0005 egress at $0.12/GB); negligible until rules number in the hundreds.
- **Alternative**: a chunked D1 table `rule_roots(scan, rule, part, body)` (one D1 value caps at 2 MB); R2 keeps D1 small and matches the static index's storage, so it's preferred.
