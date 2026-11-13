# Qualifying a complete stock-census input

Status: proposed producer handoff, 2026-11-09. There is no Parquet export adapter yet. The existing consumer is [eligibility-census]. Confirm that a real producer can supply the own-object and joint dated facet truth below before implementing another adapter. This handoff is local-file-only; it authorizes no fleet download, source rewrite, job submission or generation move.

The first useful input is one complete historical subtree with repeated labels, fragmented leaves, several represented joint owner/class tuples and observed stock changes. Use up to 16 declared scans and retain every own object/state needed for them. A second small complete scope may cover a missing contrasting distribution. Sampling rows, hash-sampling objects or stopping at a row limit cannot produce a qualified complete scope. A constant one-tuple scope checks export plumbing but does not decide the temporal/substring index design.

## What the producer must identify

Supply the immutable store/generation and exact base/append manifest revision; all source file identifiers and hashes; selected scope and ordered scan IDs/stamps; object-era coverage; attribution and class policies/revisions; the exact source column mapping; and independent per-scan bytes/counts plus joint U/C totals for that scope. Record how the source file/partition set was proven complete for the scope, including required runs and source errors. An unresolved source read is not an empty partition.

A content hash pins provided bytes. It does not prove no upstream file, own object or transition was omitted. The producer's completeness assertion and independent qualification evidence remain separate from the exporter's file/schema/stock checks. Where independent owner/class totals are unavailable, label joint-slice qualification incomplete; do not call agreement with a second projection of the same mistaken rows independent truth.

Existing recursive path rows cannot be treated as own-object events. A deployment must establish which rows represent own objects, whether a key that is also a directory retained its own row, whether attribution is a disjoint tuple or aggregated owner map, and where every zero-byte object's class comes from. Nullable or missing generation tokens are not this gate's blocker unless they are required to distinguish its own identities. Missing own rows or unknown zero-byte class are blockers for the declared stock/class contract.

## Proposed normalized own-version Parquet

The producer supplies local Parquet files containing this normalized relation, with explicit schema and immutable input manifest. Additional source fields may be retained for evidence but have no implicit meaning. A future adapter may accept an explicit mapping of these six logical columns; it must not guess aliases or select a class from nonzero recursive class byte totals.

| Logical column | Required type | Meaning |
| --- | --- | --- |
| `path` | nonnull UTF-8 string | Original case-sensitive canonical full own-object key, at/below declared scope |
| `owner` | nonnull UTF-8 string | One disjoint dated owner; empty string is explicitly unowned |
| `class` | nonnull nonempty UTF-8 string | Exact dated storage class, including zero-byte objects |
| `vf` | nonnull signed Int64 | Inclusive validity start in the manifest's scan-stamp coordinate |
| `vt` | nullable signed Int64 | Exclusive validity end; null alone means open |
| `bytes` | nonnull nonnegative Int64 | Own object's complete byte size, not a delta or recursive sum |

Each row's own-object count is one. Rows for a given original path cannot overlap at any represented source validity time, including across owner/class tuples. Adjacent rows at the same boundary are legal replacement/transfer; identical adjacent states may be coalesced without changing stock. Require `vf < vt` for closed rows. A class/owner move closes the old state and opens the full new one. No opaque JSON owner map, fractional ownership or multiple live slices is accepted by this initial format.

Paths use the same canonical slash-separated coordinates as the consumer. `scope = ""` denotes the fleet root; an object exactly at a nonempty scope is included alongside descendants. Object/folder twins must retain their own object rows. If a source fused them irreversibly, this producer cannot reconstruct the missing own contribution by subtracting guessed descendants.

All row values are explicit. The producer must normalize any source-specific open-end sentinel to null, resolve any null owner according to a documented attribution rule, and supply exact class strings before handoff. These conversions have their own evidence and checks; the consumer does not silently fill them. A mixed or aggregate class row is not a single-class object.

The input manifest should contain contract `normalized-own-versions-v1`, source identity, scope, scan IDs and Int64 stamps, column mapping and normalization policies, the ordered local file list with byte sizes/SHA-256/row counts, total unique own paths, total version rows, and the trusted coverage assertion with references/hashes of its independent check artifacts. It pins both hierarchy identity and numeric/facet history. Source generation and append-run dictionaries cannot be mixed by equal-looking numeric IDs.

## Proposed export operation

Once a real producer is qualified, a bounded `dt-cloud eligibility-export` can read only those explicitly named local files and a qualification manifest. It reconstructs complete own stock at each declared scan, then emits canonical stock deltas and the existing `joint-uc-stock-events-v1` consumer manifest. The initial baseline emits every object live at the first declared scan, including versions whose `vf` precedes it. Earlier unknown history is not inferred.

Export events in scan order, then original path/owner/class code-point order. If an own path keeps its tuple, emit the exact byte/count delta when stock changes. If its tuple changes, remove the full old stock and add the full new stock at the same stamp. Skip identical consecutive selected states. Fold each boundary atomically, so replacement order cannot create a false eligibility gap. Require a deterministic serialization/newline policy and byte-identical repeated output; hash the exact event bytes and record exact counts before publishing the consumer manifest last into a fresh output directory.

A version entirely between two chosen scans contributes no endpoint stock and therefore no stock-export event. The exporter is not a physical-event or churn exporter. A same-size rewrite that does not change declared stock is likewise invisible here. The source may retain more history, but stock export alone does not accept any churn claim.

The export report must retain the normalized input hashes and qualification references, selected scan coordinates, exact rows/paths inspected, per-scan total and joint tuple bytes/counts, emitted event count/hash and resource usage. Check those per-scan measures against the producer's independently qualified references. Hash/count consistency without those references is only a validated supplied capsule, not accepted real-source completeness.

Reject overlapping own validity, missing baseline coverage, malformed/out-of-scope paths, ambiguous class/owner state, unknown scan coordinates, count/hash mismatch, unread input, resource excess or qualification disagreement. Do not output a successful consumer manifest after such a failure. Output staging/cleanup and retries must preserve preexisting user artifacts; never replace a published capsule in place.

## Bounded execution and decision gate

Use the research spec's outer envelope: at most 500K own paths, 16 scans, four threads, 8 GiB RAM, 20 GiB work disk and 30 minutes. Start with a smaller complete scope fitting the current census defaults (50K paths, 100K emitted events, 64 MiB input, 16 scans and its recursive-work caps), or predeclare explicit overrides. Declare caps for Parquet compressed bytes, decoded row count, unique paths, emitted rows and event×ancestor work; check source metadata first and charge actual work before allocating the next batch or constructing rollups. Compressed file size alone does not bound decoded memory. An exporter needs measured RSS/work-disk guards and a cooperative deadline, with OS enforcement where the dev host provides it. None of these bounds is a measured runtime forecast.

The first real run must answer whether positive eligibility I is materially smaller than recursive value versions J, and whether direct substring points `sum I_node × label_length` or vocabulary enumeration is the cheaper candidate under declared storage/query budgets. Report tuple/depth distributions and fragmented/minority cases; global ratios can conceal the expensive slices. Preserve source/export/census artifacts and exact commands/hashes on the coordinated dev host. This gate selects a physical layout or rejects the candidate; another synthetic-only adapter acceptance does not answer it.

[eligibility-census]: eligibility-census.md
