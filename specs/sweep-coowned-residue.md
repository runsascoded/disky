# Sweep gate: stop deleting co-located non-sweeper data on a majority heuristic

A sweep approval in *slice* mode is supposed to delete "the sweeper's own slice
of the band." The gate approximates that per **directory**: a directory is
`eligible` when its nearest-indexed-ancestor's top-attributed user is a sweeper
holding ≥ `MIN_SHARE` (0.5) of the subtree's bytes. Once eligible, **every
object under it is deleted** — including files attributed to other users, and
unattributed files, that happen to sit inside a sweeper-majority directory.

This spec fixes the terminology, states the authority principle, and makes the
default conservative: **never delete non-sweeper data on the strength of a
byte-majority; surface it and require an explicit, per-owner decision.**

## The problem, precisely

- `classify_dir` (`gcs-usage/src/gcs_usage/sweep_plan.py:278`) resolves the gate
  at directory granularity. `eligible` ⇒ all keys in the dir enter the manifest.
- The gate lookup (`attr(band, bucket, dirname)`) resolves to the nearest
  **indexed** ancestor. Non-sweeper data *below* index granularity — a few of
  another user's files inside a sweeper's run dir — inherits the sweeper's
  classification and is deleted.
- Measured on the current approved set (dry run `2026-09-06-h7686`, attribution
  split at run-dir depth): the deletable set is ~100% sweeper-attributed, but
  carries **624 KB attributed to other users + 5.8 GB unattributed** across
  199 TB. Tiny — but it is exactly the data a human should see before a real run,
  not silently sweep.

### The inconsistency

We already **defer** an entire directory when someone other than the sweeper is
its majority holder (`deferred_owner` / `deferred_attr`). But we **delete** that
same "someone else's" data when it is a minority inside a sweeper-majority dir.
Same bytes, opposite treatment, decided only by how the data happens to cluster.
The conservative, consistent rule is: *delete only bytes attributed to a
sweeper; defer everyone else's, wherever they sit.*

### The authority principle

A byte-majority is **not** an ownership claim and **not** deletion authority. A
sweep vote authorizes deleting the **voter's own** data. It does not let the
voter delete a third party's files merely because they are co-located. We cannot
today infer "Kaiyue created this directory, so its contents are hers to sweep" —
that would need provenance (an Iris/W&B signal that the dir was created by the
sweeper's job; see [[assignment-provenance]]), which is not built. Until such a
signal exists and is shown, co-located non-sweeper data stays deferred.

## Terminology (fix now — shipped)

Never say a user "owns" a directory to mean "holds ≥50% of its bytes." That
conflates a heuristic with authority. The tooltips now say **"majority
byte-holder (≥50%)"** and call it *"a majority heuristic, not an ownership
claim."* Keep that language everywhere the gate is described.

## Proposed change

### 1. Default: exclude the residue

Split an eligible directory's bytes by attribution and delete only the
sweeper-attributed slice:

- **Other users' attributed bytes** in an eligible dir → deferred (a nomination
  to *those* users, exactly as a whole other-owned dir is). New category
  `deferred_residue` (or fold into `deferred_attr`) so the manifest counts it.
- **Unattributed bytes** in an eligible dir → deferred by default (most
  conservative: unattributed ≠ sweeper's). This is the bigger number (5.8 GB)
  and the softer call — see Open questions.

Feasibility: the manifest is object-level and the path index resolves owners at
run-dir depth, so most residue is already separable. Residue *below* index
granularity (the 624 KB) needs either deeper indexing of eligible dirs or a
manifest-time re-attribution pass over their objects; scope that as a follow-up
if the surfaced amount ever warrants it.

`full`-mode approval is unchanged — it remains the explicit "delete the entire
band regardless of ownership (verified out of band)" escape hatch.

### 2. Surface the residue (the "at minimum" ask)

Wherever an approved band is shown, and in the dry-run output:

- **Who**: the other users whose data sits in the eligible dirs (UserChips) and
  the unattributed total, with bytes each.
- **Inspect**: a link per residue owner to the data — the dry-run `would-delete`
  parquet filtered to their objects, and/or a homepage subtree filtered to that
  owner under the band (`?o=<user>` on the band path).
- **Decide**: include / exclude control. Excluding is the default; including a
  specific owner's residue requires that it be defensible (ideally that owner's
  own sweep vote on it, not the majority-holder's) — so "include" is a narrow,
  audited action, not a convenience.

Add a `decision` value (`residue_deferred`) to the executor logs so the residue
is browsable in `/files` alongside `delete` / `skipped_*`.

## Status — shipped 2026-09-10 (planner side)

`sweep manifest` now applies the rule: a dir inside an approved band that passes the majority gate must also be **ruled** to one of its sweepers by the deepest attribution rule covering it (`prefixes.load_prefix_map` — the same prefix map the index is built from, so the manifest and the site agree on whose data a dir is). Otherwise it defers as one of two new categories: `deferred_residue` (ruled to another user) or `deferred_unattr` (no rule, or an explicit nobody). Both are counted in `plan-summary.json` and listed per dir in `residue/<bucket>.parquet` (dir, category, user, sweepers, bytes, objects — largest first), browsable in `/files` beside the manifest. `-R/--no-residue-check` restores the old behavior; full-mode bands skip the check as they skip the gate. Because attribution is per directory (deepest prefix rule), the "below index granularity" residue needs no re-attribution pass: a deeper rule for another user is exactly what the lookup finds, index row or not.

Decisions taken: (1) unattributed defers by default; (2) nothing re-includes residue yet — an owner's own sweep vote on it is the only path we'd accept, and that is not built; (3) covered by the rule lookup, see above. Still open: the console's per-band residue include/exclude control (§2 "Decide") — today the residue is surfaced and excluded, not decidable.

## Open questions (as posed)

1. **Unattributed bytes**: defer by default (conservative, proposed) or treat as
   the sweeper's within their majority dir? Deferring 5.8 GB costs almost
   nothing; treating-as-sweeper's is the current behavior. Recommend defer.
2. **Who may include residue?** A majority-holder's vote should not re-include a
   third party's residue. Only that owner's own vote (or verified provenance)
   should. Confirm this is the rule.
3. **Below-index-granularity residue** (the 624 KB): accept as surfaced-only for
   now, or invest in a manifest-time re-attribution pass to exclude it precisely?

## Non-goals

- Changing `full` mode, the vote model, or precedence.
- Group ownership (excised 2026-09-06; stays out).
