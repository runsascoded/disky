# Portable query executors

Status: implementation handoff, 2026-11-08. Shared-code owner: the `disky` session on `cloud`; GCS owns its deployment validation and rollout. No backend implementation is claimed by this spec. Index research runs independently and is not a prerequisite.

Selected by Ryan for implementation on 2026-11-08: Phase 1 first, followed by `auth-session-log-adoption.md`. Phases 2–3 remain subsequent deployment work; this implementation does not provision a backend or change production routing.

## First deliverable

Run the existing map/diff reader against identical immutable bytes and pinned ownership/deletion state in both a Worker runtime and an ordinary Node process. Establish exact answer parity before adding cloud infrastructure or automatic routing. This extracts the existing query engine; it does not replace its index, weaken refusal semantics, or revive the retired ClickHouse service.

Start with a dependency inventory around `site/functions/_lib/view.ts` (`buildView`, `buildDiff`) and their reader dependencies. Identify platform-specific storage, footer/manifest lookup, cache, clock, scheduling, decode initialization and telemetry seams. Introduce the smallest interfaces needed for those actual dependencies, preserving the current Worker adapter and its production behavior.

## Request and result identity

The normalized request pins contract/semantics version, store, immutable generation/manifest, scan IDs, path, query AST, owner/class/liveness selectors, ownership/deletion revisions, metric/churn semantics, canvas/refinement/output limits and execution budgets. Preserve current result-selection semantics; distinguish exact top-M from pixel/share thresholds rather than substituting one for the other.

Results carry the generation and ledger revisions actually read, exact totals/counts/remainders and selection/coverage/completeness state. Distinguish unsupported queries, unavailable generations, budget exhaustion and transient failures. Partial answers and continuations must identify their coverage and cannot overwrite complete cached answers. Existing explicit refusals remain refusals unless separately implemented and verified.

## Phase 1: portable kernel and local executor

1. Capture small deterministic immutable fixture capsules, including required manifest/footer/index bytes and synthetic ownership/deletion snapshots. Use no production credentials in fixtures or tracked artifacts.
2. Isolate the reader kernel from HTTP authentication and Worker bindings through explicit adapters. Keep platform globals, `waitUntil`, D1/R2 handles and colo cache access out of the portable boundary. Keep request-scoped budgets/cancellation explicit; process caches must not mix store/generation/ledger identities.
3. Add an ordinary-process CLI or local HTTP executor that uses the same kernel and fixture bytes. Reuse existing CLIs/packages where they fit. It needs no deployed service, permission change, new cloud credential or production switch.
4. Run exact complete-body parity against the Worker adapter for root and drilled maps/diffs, plain and name-filtered reads, supported owner/class combinations, zero-byte object counts, multiple scans, remainder/tie behavior, partial/refusal cases and budget exhaustion. Normalize only documented transport/telemetry fields, not semantic output.
5. Record complete response/serialization time, decode and aggregation CPU, bytes/GETs/decoded rows, working memory and cold/warm behavior at bounded concurrency. This is portability acceptance, not an assumed speedup. Keep fixture correctness separate from later real-data measurements.

Acceptance: one source kernel, two working adapters, deterministic fixtures and a parity runner that exits nonzero on semantic disagreement. Production continues using the Worker adapter. Update the architecture overview with the implemented boundary and remaining gaps.

## Phase 2: transport and development routing

After Phase 1, add a deployment-configured executor interface and a dev-only routing selector. The edge remains the authentication/scope boundary. A remote executor accepts only a narrowly scoped, short-lived capability bound to the request, store/scope, generation/revisions and deadline. Do not forward browser cookies, job credentials, admin grants or spoofable identity headers. Select endpoints from deployment configuration, never arbitrary caller URLs.

Initially use explicit routing and exact shadow comparisons. Preserve the same bytes and revisions across executors; refuse drift rather than composing mixed answers. Enforce output-byte, memory, origin-read and deadline limits, bounded retries and concurrency. Measure streaming/paging and cancellation before claiming interchangeable complete-body behavior.

## Phase 3: hosted adapters and selection

Choose the first hosted container/VM or function adapter from measured working sets, cold loading, data placement and complete-body cost. Lambda and VM wrappers can share the executor protocol without sharing platform-specific lifecycle assumptions. Provisioning resources, credentials/permissions and production routing remain concrete deployment-owner actions, with the applicable approval gates. No such apply or cutover is authorized by this handoff alone.

An automatic planner may route directly to a capable executor using measured work bounds. Do not intentionally exhaust the Worker and then retry the whole query elsewhere. Capability gaps, resumable work and exactness must remain visible. Retain an explicit rollback path and keep old executors available for pinned requests in flight.

## Coordination and scope

The `disky`/`cloud` session should commit the initial spec before implementation under the usual spec workflow, then own shared package/API changes and CI. GCS will adopt shared commits and perform GCS dev/prod verification; other deployment owners adopt the same shared interfaces. No deployment-branch merge into `cloud` is required.

Phase 1 can start now. It is not blocked by the research index, a final cloud-provider choice, fleet compaction or a production generation move. Before reporting work as active, the owning session must actually accept/start the handoff; delivery to an inactive session is not execution.
