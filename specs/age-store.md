# Age histogram history and independent store locations

The eight age-pyramid tiers contain a histogram for each path. Directory means cannot reconstruct those histograms. Permanent full per-scan pyramid copies are replaced by change intervals over exact cells, using the existing interval-store implementation and range reader.

## Representation

Each `(tier, original path, binstart)` becomes one depth-three file row at `<tier>/p=<URL-encoded path>/<binstart>`. URL encoding uses RFC 3986's unreserved set. The row's `size` is bytes and `n_files` is object count. Unchanged cells coalesce, zero-byte cells retain object counts, removed cells close, and native binary-counter carries and compaction preserve every scan's answers. Owner slices in the input pyramids are summed to the path/bin totals that the current age API serves.

`dt-cloud age-store -g GEN -L 'listing/{id}/index/{gen}/age-pyramid-1h.parquet' -s SOURCE -o HISTORY -w WORK` discovers historical inputs, builds all eight tiers' synthetic rows, and builds or appends the native interval store in a fresh local mirror. Every newly covered scan is exported and compared as a complete multiset of `(path, size, n_files)`. A mismatch publishes no new manifests or current pointer. Initial generations and later native compactions use the same engine.

Publication orders data, liveness metadata, scan catalogs, manifests, successors, then `interval-store/CURRENT.json`. A passing comparison gets `verified/<scan>.json`; coverage requires both that marker and a published native manifest/catalog holding the scan. Temporary canonical input rows are discarded after success. The small scan catalog, manifests and verification records remain as the explicit per-scan ledger.

## Serving and retirement

`AGE_STORE_PREFIX` is the independent age-history root in `INDEX_R2`; the native store is under `<prefix>/interval-store/`. The age API reads live synthetic cells with the existing interval range reader and keeps its response shape. Uncovered scans retain per-scan fallback; a covered empty histogram is empty, and a broken interval run uses the reader's retryable refusal rather than falling back to a retired file.

`age-store -r ROOT [-r COPY] -k 2` prints a retirement plan. `-x` executes it: only histogram files and footers of verified, manifest-covered scans older than the recent keep window. D1's age footers and pointers clear first, with the existing bounded deletion helper and the explicit `-T` site-store key; later copies share that D1 pass. A failed D1 operation preserves the files. `-D` explicitly skips D1 for a deployment without it or a copy already cleared. Ordinary path retirement remains independent and never assumes that a path interval implies age coverage.

The optional `age-store` scan step appends and verifies before ordinary path retirement, and collects superseded native generations after the normal grace. Its profile supplies `store`, initial `gen`, optional `keep` (default two), `copies`, and `execute` (default dry). No deployment enables it implicitly.

## Independent path and static histories

`INTERVAL_STORE_PREFIX` defaults to `interval-store`. Secondary stores opt into `PATH_STORE=intervals` with their own prefix and pointer; secondary scan profiles must explicitly provide their interval-store root. Store overlays clear primary path/age settings. Handles, state, footers, decoded groups, answer tags and health caches are namespaced by physical prefix as well as generation and site-store key.

`STATIC_CURRENT_KEY` optionally names an independent static-lineage pointer. A deliberate `STATIC_GEN` pin still wins. `static-names build|add -u KEY` publishes that lineage's pointer only after its generation is whole; `add` follows compaction successors. The generic scan step passes `options.static-names.current_key` to either command. Ordinary retirement follows static successors too. `store-gc -k ROOT` protects the named static generation and its newest successor, so two lineages in one bucket can be collected safely; `options.store-gc.static_gens` supplies every live lineage root. The fixed original bases stay as explicit rollback anchors, while intermediate generations expire after the existing grace.
