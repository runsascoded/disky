# Fleet query-index research

Status: active, 2026-11-09. Cross-deployment owner: the root `disky` session (`cloud`). Experimental source lives on `w/fleet-index-research` in `wt/index-research`; GCS owns its deployment inputs, real-data acceptance and rollout. This work is separate from the `#disky-admin` fleet-status dashboard and from portable-executor deployment.

## Goal and current evidence

Find an economical exact serving design for dated maps and diffs with subtree, case-insensitive name search, ownership, storage class and liveness selectors, under declared CPU, memory and physical-read budgets. Preserve exact totals, own-object counts including zero-byte objects, folded remainders and explicit completeness. A bounded useful pixel/share partition does not inherently require the globally largest M rows. Stock, endpoint difference, physical churn and membership churn are distinct contracts.

The latest private research is GCS's `tmp/query-architecture-research.md` and `tmp/query-research/decision.md`, updated November 9. Copies and their hashes are retained in this worktree's ignored `tmp/prior-research/`; the originals remain in place. The selected candidate shares segment text/topology and separates historical numeric measures. Its selected-presence witness is a tiny scanning oracle, not an implemented joint successor index or a fleet performance result. Existing packed scans and the spatial reference remain comparison baselines. The production interval/static-name indexes remain in service.

The portable executor's Phase 1 is complete: 143 exact synthetic Node/workerd comparisons, with separate sampling profiles. Its 16 MiB fixture adapter is not a production-data capture mechanism. See [executor results]. Avoid repeating that acceptance or older full-fleet censuses as a substitute for the missing joint-index measurement.

## Experiment 1: joint eligibility census

Implement a reusable offline `dt-cloud eligibility-census` command over an explicitly pinned complete historical scope. Start with fixed dated owner × class stock, then validate against the selected-presence oracle and adversarial fixtures. An input manifest must identify schema, root/scope, ordered scan stamps, hashes, input counts and the producer's coverage assertion. Validating those assertions against the supplied files does not independently prove that a producer omitted no source objects; a real-data capture needs its own coverage check.

Measure and report separately:

- Full recursive joint byte/count value versions and coalesced positive-count validity intervals.
- Exact/prefix point counts and direct substring character × interval join amplification; vocabulary enumeration is an alternative with query-time work, not free compression.
- Own-object changes and recursive ancestor/facet updates, including owner/class transfers and simultaneous replacement folded atomically.
- Fixed-width packed payload terms, metadata/dictionary terms and construction resource use. A structural byte estimate is not a measured encoded index or an I/O result.

Initial real-data admission limits: one complete subtree, at most 500,000 own paths and 16 scans, four threads, 8 GiB working RAM, 20 GiB work disk and a 30-minute deadline. These are experiment bounds rather than predictions or a proposed production refusal policy. Refuse an oversized or incompletely qualified scope explicitly; do not truncate it into a complete-looking answer. Choose a smaller complete subtree if admission fails. All heavy processing runs on a coordinated dev node; do not copy fleet data or dependency caches onto the laptop.

Correctness checks include zero-byte objects, byte-only changes without count changes, same-stamp replacement, owner/class transfers, empty-to-positive and positive-to-empty transitions, nested/repeated matching labels and historical scan gaps. Preserve a brute-force result for every tested query. Explicitly refuse unsupported physical churn, live ownership/deletion masks and object-version-only selection in this stock-only prototype.

## Next gates

1. Only after the census, compare an implemented hybrid discovery/measure reader against a packed scan on selective, broad, fragmented and minority-owner predicates, deeper trees and distinct timelines. Use equal resident-memory budgets and charge all dictionary/preload/discovery, construction/update, read and serialization work. An index needs a measured crossover with an engineering margin.
2. Add bounded real-data capture to the portable executor and compare complete answers on identical immutable generation, scan, ownership and deletion revisions. Preserve current refusals until a candidate actually implements them.
3. Decide deployment transport and compute placement from those measurements; do not wait for a universal index to finish dev-only executor transport. No production pointer move, hosted infrastructure apply or permission change follows from this research spec.

Known open costs: selected nonempty match roots remain unbounded by M; endpoint eligibility misses born-and-deleted-in-range churn; Boolean combinations, live overrides and generation-local hierarchy coordinates need additional machinery. Marginal indexes do not by themselves implement a joint selected-coverage oracle.

## Progress ledger

| Experiment | State | Execution / evidence | Next decision |
| --- | --- | --- | --- |
| Portable executor Phase 1 | Verified | `site/executor/results.md`; 143 synthetic response pairs | Real-data adapter and explicit dev transport |
| Joint eligibility census | Source implementation active | Bounded subagent `index_research`; no data job started | Exact fixture verification, then GCS-qualified complete subtree |
| Fleet-scale candidate reader | Not started | No joint successor/index built | Census must justify construction first |

Update this table with the exact source revision, input identity, host/job ID, start/end times and artifact paths when work runs. Agent delivery or a suggested command is not a running experiment. Keep at most two research experiments active at once; each must have a bounded question and a result that changes the next action.

[executor results]: ../site/executor/results.md
