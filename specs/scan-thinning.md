# Optional scan thinning

Status: initial implementation, opt-in per deployment. Readers change only with `HISTORY_CURRENT_KEY` set (unset: no history read, ordinary live-reader behavior); preview and build jobs dispatch only with every `HISTORY_*` job setting plus `GCP_SA_KEY`, `JOB_SA` and `GCP_PROJECT` (a GCS data store and an R2 serving store); otherwise the admin controls stay explorable with dispatch disabled. No policy is scheduled and no default historical dates are removed.

## Goal and terminology

CS3 means storage proportional to observed changes rather than a full copy per scan. Keeping every observed change is compatible with CS3. Thinning is an additional, explicitly chosen tradeoff between historical resolution and storage. It does not replace deduplication or obsolete-generation GC. Geometric compaction (merging runs) and geometric scan retention (discarding observations) are separate policies.

## Selection policies

The low-change policy removes N interior observations greedily. For consecutive retained scans A, B, C, B's score is `max(changes(A,B), changes(B,C))`. Choose the smallest score, breaking ties chronologically. Compute the replacement A–C delta from actual endpoint states, then update the neighboring candidates. Adding adjacent counts is incorrect when changes reverse. A changed object counts once for addition, removal or an edited canonical state; directory rollups and read-day movement do not amplify that count. Known object identity tokens participate without filling historical NULLs from future observations. Display byte differences and the two component deltas alongside the score. “Lowest change” describes this heuristic more precisely than Shannon entropy.

The geometric policy preserves a configurable dense recent window, then samples exponential age bands anchored at the newest observation. Each band is divided into a configurable number of equal-duration bins; keep the newest scan per occupied bin. Explicit pins add representatives rather than replacing policy-selected dates. All selections are deterministic for a fixed roster, policy and pin set.

Both policies protect the first and latest observation, explicit pins, schema/identity/churn baseline transitions, unresolved staging review dates (`plan_items.as_of`) and scans required by active deletion/recovery work. Preview identifies the reason for each protection and refuses impossible targets. Policies are opt-in; no default historical dates are removed.

## Preview and jobs

The primary `/scans#history` page provides admin policy controls, explicit pins, preview dispatch, an auditable removal sequence, source-generation/manifest fingerprints and candidate-build dispatch. Heavy planning and generation work runs on Batch beside the metadata bucket. Requests and progress records are immutable/auditable under `history/jobs/<id>/`; status updates identify planning, folding, verification, upload, completion and failures. Repeated dispatch uses explicit job IDs; a candidate never changes live pointers by being built.

Each environment may CAS-select any verified immutable history version, including rollback; that version’s retained roster defines availability, while adoption must preserve all currently required operational pins. The initial adoption mechanism pins an immutable reader snapshot; subsequent scan-pipeline appends remain separate and its writer CURRENT objects never change.

A build uses the exact reviewed plan. Source rosters and manifests must still match; a new append requires a new preview. It constructs fresh self-contained interval and static generations. Source generations and CURRENT remain untouched. Candidate metadata pins the sources for GC, records policy, removed/retained dates, provenance and independent verification results. Promotion is a separate reviewed operation, with append-race and live staging-pin rechecks under builder leases.

## Interval projection

For ordered retained stamps K, map structural bounds to `nextKept(t)` (the first retained stamp at or after t, or OPEN). Discard empty projected intervals and coalesce contiguous equivalent rows using each canonical table's full key, including nullable owner slices. Project `pv`, `pvl`, `rd`, `sv`, `svt` and attribution. Observed timestamps (`mtime`, `wts`, `last_read`) and identity tokens stay exact; they are not structural bounds. Regenerate served sorts and sidecars. Every retained snapshot must agree with the source for all canonical columns and attribution coverage.

Thinning can remove an object's entire transient lifetime (A→B→A). Snapshot parity does not imply parity of observed churn. The product choice is explicit: recompute churn over retained observations, or preserve full observed churn using an independent change ledger, including zero-net transient children. Blindly retaining old cumulative fields is invalid because child sums and level indexes can disagree. The admin preview must describe the selected semantics. The initial implementation recomputes churn from retained observations and records `churn_basis=retained-observations`; the preview calls out lost transient changes. A full observed-churn ledger remains an alternate future treatment.

## Static search

Fold the source's base and runs to canonical coalesced intervals, project their structural bounds and regenerate histograms, suffix shards, membership/census thresholds, catalogs, drill/root measurements, names and anchors. Pure-merging old derived artifacts keeps dropped history and is insufficient. Retained dates' plain, substring, heavy/short, masked, anchored and path-term answers must agree independently with the source.

## Publication and readers

Candidate builds are safe without reader changes. An immutable `history/versions/<id>.json` descriptor holds the retained roster and interval/static generation pair. Each environment independently CAS-selects it through `history/environments/<environment>/CURRENT.json`, resolving the pair once per request. A pinned environment intentionally stays on that snapshot until another explicit adoption; writer appends remain separate. Dropped dates must be excluded from the D1/interval scan union and must return an informative unavailable response instead of falling back to old per-scan artifacts. Deep links do not silently select nearby dates. Existing staging review scans remain addressable.

Promotion must not create an inconsistent interval/static pair. Old generations remain available during grace and are collected through the existing audited GC protocol after all pins/dependencies permit it. Per-scan extras and archived over-time data need explicit retention accounting; they must not silently resurrect dropped dates.

## Verification

Remote tests cover exact selection/recomputation/ties/pins/bands; projection boundary cases and all canonical tables; transient lifetimes/reversions; churn/level consistency; static membership threshold crossings and search modes; append-after-thinning; source changes and interrupted jobs; immutable candidate publication; staging pins, reader fallback and unavailable deep links. Tests run remotely, not on the laptop. Validate visible controls in Chrome before reporting UI verification.

## Scope of the first implementation

The first UI adoption is deliberately a fixed reader snapshot. It does not switch the scan pipeline's writer base or automatically publish later appends. A later operational cutover that makes thinning the continuing writer history, together with source-generation retirement, needs an explicit lineage/publication design; this implementation does not claim that reader adoption alone frees the source history. Full observed-churn preservation and its independent event ledger are also outside this first retained-observation implementation.

## Review controls

The controls live beside the existing scan-run duration chart and table at `/scans#history`; `/admin#history` links there. They start Off (keep all), and no scheduled thinning exists. Off is a proposal-draft choice, not a command to cancel a running candidate job or replace an already adopted reader snapshot. Missing cloud credentials disable dispatch, while policy selection, geometric parameters, writer-roster inspection and manual selection remain available.

Manual proposals name an explicit removal set. The chart, row checkboxes, retries and reviewed proposal hover together. The existing dragged time window can be applied to manual selection explicitly. Writer CURRENT metadata supplies the eligible roster independently of any reader pin, so old successful runs outside source history are excluded and a valid scan whose pipeline later failed remains selectable. First/latest, known operational pins and reviewed plan protections remain visible; the backend revalidates the complete policy and source fingerprint. Closing the controls clears the proposal and manual selections; reopening starts Off.

For a reviewed selection the backend computes exact projected canonical row counts and a rough compressed-byte estimate for folded interval canonical tables only, holding source footer overhead fixed. It excludes static/search artifacts, served sorts, mirrors, churn reconstruction, other retained generations and compression/layout effects. Estimates are for the selected set and cannot be added per scan. Source object payload byte changes are a separate metric, not index storage savings. Completed builds compare measured inventories for each generation copy, including temporary additional storage and a signed potential reduction conditional on eventual source GC. Reader adoption alone neither changes the writer base nor frees those source bytes. Human review exposes change score, timing and storage separately; no automatic weighted tradeoff is assumed.
