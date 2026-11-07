# One tree builder: kill the depth cap, make folding a budget

2026-08-23. Answers "why does `/cw` go arbitrarily deep while GCS stops at 4,
and why is a rendering cutoff baked into ETL?"

Short version: **the depth cap is not a design choice, it's a symptom.** The
fix is already specced — it's `specs/done/aggregation-extensions.md` § "Consumer
follow-up", whose prerequisites have all landed. Folding is a separate issue
and the current shape of it is genuinely wrong.

## 1. Why the two builders differ

They read different things.

`job/cw-webdata.py:62-93` reads a **disk-tree layer-2 parquet** — one row per
path, `kind='dir'`, with `size`/`n_files` already rolled up at every depth. So
it can select every dir above a byte floor and link parent→child. Depth never
enters into it.

`marin/src/gcs_usage/viz.py:285-321` reads the **raw object listing** and does
its own aggregation, with the depth cap welded into the SQL projection:

```sql
coalesce(regexp_extract(dir, '^([^/]+)', 1), '') AS d1,
... '^[^/]+/[^/]+/[^/]+/([^/]+)' ... AS d4,
sum(size_bytes) ... GROUP BY ALL
```

That flattening is load-bearing *given the input*: grouping 595M object rows at
full path grain in Python is not viable, so the SQL has to collapse cardinality
first, and four components is where someone drew the line. `_build(g, ["d1",
"d2", "d3", "d4"])` then just walks those columns.

So GCS isn't capped because anyone wanted a cap. It's capped because it never
got migrated onto layer-2.

## 2. The fix already has a spec

`specs/done/aggregation-extensions.md` § Consumer follow-up:

> marin's `webdata` splits into: per-bucket `disk-tree import` (layer-2 with
> `n_files`, class sums, `mtime_mean`) + an overlay pass (attribution join →
> `tm`/`us`/`sh`, pricing, JSON slices). Then `webdata`'s DuckDB aggregation +
> `listing.py`'s SII shim retire.

Both engine prerequisites are marked landed (`--pivot-sum`, `--mean-mtime`,
3-engine parity + tests). What remains is the marin-side consumer rewrite and
the `[ ] a2a extension vs marin production cb/d` validation.

Once `webdata` reads layer-2, it uses the same parent→child linking CW already
uses, `d1..d4` disappears, and the tree is as deep as the data. **Do not write
a second arbitrary-depth builder — extract CW's into a shared function and have
both call it.**

One real prerequisite to name: attribution (`tm`/`sh`/`us`) is currently
computed by grouping raw object rows against `dir_attr` at the d1..d4 grain
(`viz.py:274-282`). At arbitrary depth it has to become either a layer-2 column
(the `--pivot-sum` pattern) or a path-keyed join against layer-2 dir rows. That
is the actual work in this migration; the tree shape is the easy part.

## 3. Folding: right instinct, wrong current shape

The critique is correct — a rendering cutoff should not be a semantic property
of the stored aggregate. Two things need separating:

**Aggregation is already complete.** disk-tree's layer-2 has every path at
every depth; nothing is lost upstream. Only the *publish* step prunes. So this
was never a backend-processing optimization — it's a payload-size decision
about one static JSON served to a browser, which is a legitimate thing to want
and an illegitimate thing to bake in irreversibly.

**What's wrong today**, concretely:

- Two different cutoffs with no shared rationale: `FOLD_MIN_BYTES = 20e9`
  (`viz.py:29`, absolute — doesn't scale with fleet size) and `--min-frac
  0.0002` (`cw-webdata.py`, relative).
- Fold nodes are a dead end by construction: `{"n": "(other ×N)", "b":…, "o":…}`
  with **no `c` key** and no marker distinguishing "this is a leaf" from "this
  is pruned". The UI therefore cannot offer to expand them even in principle —
  clicking one just pins a tooltip.
- The cutoff isn't recorded anywhere, so the UI can't tell the user what
  threshold they're looking at.

## 4. Proposal

1. **One shared builder**, parameterized by floor, called by both the GCS and
   CW paths.
2. **Floor is relative and recorded.** A fraction (of total, or of the node's
   own bytes), written into `meta` so the UI can say "showing prefixes ≥ X".
   An absolute constant that predates a 10× fleet growth is how you end up
   with a tree that's mostly `(other)`.
3. **Fold nodes carry a marker** — e.g. `f: <count>` as a field rather than
   encoded in the display name — so the client can distinguish pruned from
   leaf and render an expand affordance.
4. **Lazy subtree fetch**, which makes the floor stop being a wall. The widget
   already supports it (`packages/react/src/Treemap.tsx` `hasChildren` /
   `loadChildren`), `ui/` already wires it to a per-prefix endpoint, and
   `site/functions/` already proxies the bucket and already range-GETs parquet
   for the `/files` browser. `site/` just never wired the props.

Note the two cutoffs then have genuinely different jobs and both are fine:
the ETL floor is a **payload budget** (a coarse safety net on one JSON), and
the client's existing `minCellArea = 16px²` handles **visual dust**. What's not
fine is the ETL floor doubling as a permanent boundary on what's knowable.

This also closes `specs/done/mark-sweep-ui.md:56` ("marking needs to reach prefixes
below the current webdata depth cap").

## Status

- [x] **Extract CW's parent→child builder into a shared, floor-parameterized fn**
      — `src/disk_tree/tree_build.py` (`build_tree(rows, total_bytes, min_frac)`
      over `DirRow`s), 7 tests. Arbitrary depth, relative floor, `(other)` fold
      carries an `f: <count>` marker + re-combined class/date/attribution.
- [x] **Marking reaches any depth already** — independent of the tree, via
      `/api/resolve` + `gcs-usage status` (resolution is prefix-matching over the
      ledger). So `mark-sweep-ui.md:56` is unblocked *now*; the tree deepening
      below is about *browsing*, not marking.
- [ ] Rewire `job/cw-webdata.py` onto `build_tree` (validates the extraction on
      the already-working CW path)
- [ ] Rewrite `viz.py`: aggregate to full-dir grain (ancestor-explode over the
      listing) instead of `d1..d4`, attribution join at dir grain, feed
      `build_tree`. **Runs on 595M rows → GCP Batch, on this branch, not the
      live daily job until validated.**
- [ ] Relative floor recorded in `meta` (so the UI can say "showing ≥ X")
- [ ] Site: render `f`-marked `(other)` as expandable; per-prefix endpoint +
      `hasChildren`/`loadChildren` for below-floor lazy fetch
- [ ] Measure published payload size before/after; tune the floor to a budget
