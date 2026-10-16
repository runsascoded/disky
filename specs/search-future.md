# Search: the baseline, and what's parked (after Monday)

Status: 2026-10-15. Parked until after the Monday presentation (Ryan). The baseline below is what ships everywhere; nothing here is in progress.

## Principles

- **A search is fast and exact, or it's unsupported.** No "approximate", no "partial", no "timed out" in the baseline. A form we can't answer that way is refused up front with a plain "isn't supported" message, never walked approximately.
- **Indexes grow with changes, not with scans.** The static name index (base + runs + carries) and the interval store are O(changes). Per-scan search sidecars (`rows-search`, trigrams) are O(scans), which is why every deployment is now `FILTER_INDEXED_ONLY=1` (`specs/lean-scans.md`).
- **One search everywhere.** Every deployment runs the same subset; a deployment never carries a richer or different one.

## The baseline (shipped)

- A term is plain text, matched as a substring of a file or folder **name** (a path segment). `*`, `?` and regex characters are literal.
- Anchors: `^q` (name starts with), `q$` (ends with), `^q$` (whole name), from the anchored index every deployment now builds (`static_local` too).
- `is:file`, and AND / OR / NOT of terms.
- Within an owner pool (`o=`: owned, unowned, all but some people).
- Refused as unsupported: a term containing `/` (path terms); `^`/`$` terms below the minimum length; a person lens or a storage-class scope; a common term under a large folder in an owner pool (`owner-too-common`).

## Parked candidates

Each needs, before it's un-parked: brute-force parity on a real deployment's data (not just fixtures), measured latency at gcs scale (cold and warm), the refusal rate it introduces, and no new O(scans) structure.

| Candidate | State | Cost / open questions |
|---|---|---|
| **Path terms** (`a/b`, `^a/b`) | Built, behind `SEARCH_PATH_TERMS=1` (off). Reads the name index's range for the term's tail, keeps rows whose path holds the term. Exact vs brute force on fixtures; answered on r2.rbw.sh in ~1 s. | No new index. Adds two refusals of its own (a tail under 2 characters; a tail too common to read under a large folder), which is what kept it out of the baseline. |
| **`x/`** (everything inside folders named `x`) | Refused. | Equals the folder roots of `x$`, but static hits carry no file/folder kind, and an S3 file `x` beside a folder `x/` is one hit. Needs a kind bit in the name index (`specs/search-box-no-refusals.md` §3.3.1). |
| **Short anchors** (`^a`, `a$`, `ab$`) | Refused. | `^a` needs a reader spanning shards plus 1-character catalog entries; `a$`/`ab$` a new index of names' last 1–2 characters (§3.2). |
| **Person lens / class scope** | Refused. | Owner-split rollups for heavy literals; class columns in the hits (§3.4–3.5). |
| **Regex / glob** | Non-goal. | Unbounded cost per query; no index makes it exact and fast in general. |

## Order, if un-parked

1. Path terms (built; needs the real-data parity and latency measurements, and a decision on its two refusals).
2. The kind bit, which unlocks `x/` and sharper `is:file`.
3. Short anchors and lens/class scopes, only if usage shows demand.
