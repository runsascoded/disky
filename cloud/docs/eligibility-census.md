# Joint owner/class stock eligibility census

`dt-cloud eligibility-census manifest.json events.jsonl` prints a deterministic JSON report. It reads local files only, writes nothing, installs nothing and performs no source/cloud discovery. Each invocation admits one whole capsule or fails without a report. This is a bounded in-memory census, not a fleet builder, query index or serving benchmark.

The fixed contract is own-object stock at declared scans with dated, disjoint joint owner/class slices. Recursive values include each node's own object and every descendant. Zero-byte objects remain present through their count. Names remain case-sensitive identities; shared label/character measurements use Python `str.lower()` and Unicode code points. These measurements do not establish cross-runtime Unicode query parity.

The checked-in adversarial capsule is `cloud/tests/fixtures/eligibility-census/{manifest.json,events.jsonl}`. On the dev node, from the repository root:

```bash
dt-cloud eligibility-census cloud/tests/fixtures/eligibility-census/manifest.json cloud/tests/fixtures/eligibility-census/events.jsonl > tmp/census-tiny.json 2> tmp/census-tiny-resources.log
```

It must report 21 recursive value versions (15 positive), nine coalesced positive intervals and 27 event×ancestor updates. Direct substring joins decrease from 32 value-version characters to 20 eligibility-interval characters. These are tiny synthetic structural counts. The exact assertions and a separate own-object snapshot oracle live in `cloud/tests/test_eligibility_census.py`.

## Capsule schema

The manifest has exactly these fields:

```json
{
  "contract": "joint-uc-stock-events-v1",
  "complete": true,
  "initial_state": "empty-before-first-scan",
  "source": "immutable generation + export revision identity",
  "scope": "bucket/complete-subtree",
  "scans": [0, 1, 2],
  "events": 2,
  "paths": 1,
  "sha256": "<lowercase SHA-256 of exact events.jsonl bytes>"
}
```

`scope` is a canonical slash-separated path, or `""` for the fleet root. Event paths use those same full coordinates and must be at/below scope; `scope` may itself own an object. No empty, `.` or `..` segments are accepted. `source` identifies the immutable source and export semantics. Its truth and source completeness are the producer's responsibility: hashes/counts pin the supplied export, but cannot prove that the producer did not omit an object or transition. Use a complete subtree, never sampled events or a row-limited query. Scans are strictly increasing signed 64-bit integers (indexes or epoch stamps); every source stock transition between the declared scans must be represented by the stock delta at its declared destination scan. The source must supply full initial stock at the first scan. Unknown earlier object history cannot be reconstructed by this command.

Each JSONL row has exactly these fields, ends with a newline, and is at most 1 MiB:

```json
{"scan":0,"path":"bucket/complete-subtree/Hit/file","owner":"alice","class":"1","bytes_delta":5,"objects_delta":1}
{"scan":1,"path":"bucket/complete-subtree/Hit/file","owner":"alice","class":"1","bytes_delta":7,"objects_delta":0}
```

Rows are ordered by scan; ordering within one scan is immaterial. All same-scan changes are folded atomically before validation or version boundaries. A deletion followed by replacement at the same scan therefore cannot introduce a false empty interval. `owner` is text (`""` is unowned), `class` is nonempty text, bytes are signed 64-bit integers and each row's object delta is −1, 0 or +1. At a scan boundary each own `(path,owner,class)` has count 0 or 1, nonnegative Int64 bytes, and zero bytes when absent. Across joint cells an own path has at most one object. Transfer an object by removing its complete old state and adding its complete new state at the same scan. An object whose key also names a folder remains a separate own contribution to that node's recursive values.

This format is an exported own-object event contract; feeding recursive directory sums as events double-counts contributions. A Parquet/version-row adapter is not implemented; the proposed [producer qualification contract] defines the minimum source truth needed before building one. Liveness overrides, object-version selectors, physical/membership churn and Boolean query plans are outside this stock census.

## Counts and bytes

The report counts event×ancestor updates before same-stamp folding, recursive cells whose value changes, complete changes including terminal zero, positive byte/count value versions and maximal count-positive validity intervals. The final open interval has `end = null` internally. Exact/prefix geometry uses one point per searchable eligibility interval. Direct substring geometry uses one point per lowercase basename character per interval; these are replicated coordinate rows even when vocabulary text is shared. The report also counts character joins for positive value versions, providing the uncoalesced control.

The byte ledger gives explicitly assumed fixed-width payload terms: value rows 48 bytes (node ID, joint tuple ID, start/end, bytes, count); eligibility intervals 32 bytes; exact/prefix or substring points 40 bytes (interval plus label/suffix coordinate). These terms are alternatives, not additive complete index totals. They are computed counts × declared widths, not actual encoded files, compressed sizes, process RSS, range-tree sizes or I/O measurements. Topology, path/tuple dictionaries, coordinate directories, access-index replication and build memory remain excluded and are listed in the output. Shared vocabulary UTF-8 bytes and UInt64 offsets are separate terms.

Default limits: 64 MiB input, 100K event rows, 50K distinct own paths, 16 scans, depth 64, 2M event×ancestor updates, 250K tree nodes/changed recursive cells, 1M value changes, 8 GiB process peak RSS and 300 seconds. `--help` exposes explicit overrides. They are admission/work caps, not calibrated RSS predictions. Input/manifest bytes are bounded before decoding; other work stops when its cap is crossed. The deadline/RSS checks are cooperative, not OS isolation. No partial success report is emitted.

The JSON report on stdout is deterministic for identical input and limits. A separate stderr JSON resource record reports construction wall time, process CPU, process-lifetime high-water RSS, single-thread execution and zero work-disk writes. RSS includes the interpreter/imports and any earlier work in that process; it is not a per-request allocator measurement. Redirect stdout/stderr separately when preserving results.

A producer and consumer must pin hierarchy, attribution and scan semantics together. The census has no stable DFS allocator, joint range-min/successor structure, query refinement or performance crossover claim. Use its complete-scope counts to decide whether a physical-index experiment is justified.

[producer qualification contract]: eligibility-producer.md
