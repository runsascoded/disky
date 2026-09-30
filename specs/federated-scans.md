# Federated scans — portable per-location scans, a union reader, an embeddable viz

Status: **vision / north-star** (2026-09-20). Captures the direction behind r2.rbw.sh; the near-term single-index build (below) is a deliberate stepping stone toward it, not a divergence.

## The idea

A scan is a **portable artifact** (`<uuid>.parquet` + `.scan.json`, as `index --to`/`reduce --to` already produce). Today disk-tree treats blob storage as a **search path** (multiple scan dirs unioned locally, `config.scan_read_dirs()`). Extend that to the web:

1. **Scans live beside the data they describe.** Each source bucket houses its own scans — inside it under a reserved prefix (`_disk-tree/` / `usage/`) or in a dedicated **sidecar bucket** (`<bucket>-usage`). Whoever owns/scans the bucket publishes there.
2. **The www reader unions multiple distinct scan locations — at read time.** A deploy is configured with a *list* of scan locations `{store, prefix, creds}`; the Map unions their roots (the union-of-roots concept, now over independent, separately-owned locations rather than one pre-merged index).
3. **The viz is embeddable per project.** The same widgets (`@rdub/treemap` + `@disk-tree/react`: treemap, sizes-over-time, age, staleness) render a *single* location embedded in that project's own site. e.g. ctbk / crashes / jct each surface their own bucket's usage (sizes over time, treemap) at their own `/files` page, reading their own sidecar — while r2.rbw.sh unions all three.

The through-line: **one scan artifact, many readers** — a project's own site (single location) and an aggregator (union of locations), both anonymous over public buckets.

## Near-term build (the stepping stone) — DT-owned daily r2 scans

- **disk-tree owns the r2 scans**: a daily DT ingestion job scans ctbk + crashes + jc-taxes and publishes to **one r2 index bucket** (union-at-ingest, cw-multi-bucket style). r2.rbw.sh reads that one index → the union Map (`specs/union-of-roots.md`).
- This is chosen for speed to a live deploy; it is a **special case** of the north-star where the "union of locations" happens at ingest into a single location, and DT owns all three scans.

## Design guardrails (so the stepping stone generalizes cleanly)

- **Keep ingestion output per-source**, not irreversibly merged — so a source's scans can later move to its own sidecar without re-deriving. (The union at ingest should be a *view*/manifest over per-source outputs, not a lossy merge.)
- **Reader points at any store** — done: `makeStore`/`storeCreds`/`storeReady` are env-generalized (`0862f37`). North-star step: generalize the single `Env` store to a **list of locations**, and union their roots in the reader.
- **Viz stays deploy-agnostic** — already true (the widgets are workspace packages; the static CFN reader is store-driven).
- **Anonymous over public buckets** — the r2 buckets are public, so the union reader and per-project `/files` embeds need the **public/no-gate auth mode** (a DT-lane item in `union-of-roots.md`; mgu's deploys all require a scope). Private locations later carry per-location creds.

## Phases

1. **(near-term)** Single-union-index r2.rbw.sh — DT daily ingestion → one r2 index bucket → union Map. (In progress; store seam done.)
2. **Multi-location union reader** — reader config = N `{store, prefix, creds}` locations; union their roots at read time. r2.rbw.sh reads the three buckets' sidecars instead of a pre-merged index.
3. **Per-project embed** — publish the viz as an embeddable route/widget; ctbk/crashes/jct host it at `/files`, reading their own sidecar (single location).
4. **Sidecar ownership** — each bucket's own scan job writes its sidecar (or a dedicated sidecar bucket); DT orchestrates, or each project self-scans on the shared CLI.

## Open questions

- **Sidecar location convention**: reserved prefix inside the bucket vs a dedicated `<bucket>-usage` bucket. (Sidecar bucket keeps the data bucket clean and lets scan-write creds differ from data creds; in-bucket prefix is zero extra infra.)
- **Union reader auth**: public locations are anonymous; a private location needs its own creds threaded per-location (the `{store, prefix, creds}` shape already anticipates this).
- **Freshness across locations**: locations are scanned by independent jobs (never byte-synchronous). The Map aggregates child sizes, so per-super-root scan time is a non-goal (matches the `union-of-roots.md` deferred-multi-account note); surface each location's own scan time in its crumb/tooltip.
- **Relationship to the `ui/` superset**: the local scan-manager (`ui/`) already unions local scan dirs; the federated web reader is the same union concept over remote locations. They may converge on one union-reader abstraction.
