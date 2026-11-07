# De novo factor: cw-s3 → gcs as a feature-by-feature parallel construction

**What "factor the diff" means here** (and everywhere in this repo from now on): start from one branch's tip and rebuild the other branch's delta as a sequence of *features*, each a hunk-level logical change assembled across files, where **every intermediate commit is a working checkpoint** — `tsc -b`, `tsc -p functions`, and the Python suite green; the FE served on a throwaway harness at milestones. These are states that have never existed anywhere; building them is real integration work, not bookkeeping. File/hunk-bucketed "factored" branches (`factored/cw-s3-gcs-2026-09-{09,16}`) are *diff partitions* — useful only as a delta inventory and a hunk→owner map — and are not what this exercise produces (see the ledger's 2026-09-16 checkpoint audit: 14 of 15 bucket commits fail to build).

## Why

Two deployments, one intended codebase. The de novo sequence answers the question the convergence plan needs: **which features are separable by configuration, and where is the real entanglement?** A feature that can be added as a clean commit on top of the prior checkpoints is config-shaped; one that forces a rewrite of an earlier checkpoint marks the seam that has to be refactored before gcs and cw-s3 can collapse into one branch + config.

## Branch and base

- Branch `denovo/cw-s3-gcs-<date>` in `wt/factored` (reset), from cw-s3's tip **after** the `cloud/` rename (both branches renamed identically, so paths are stable under the construction).
- Target: the tip either ≡ `gcs` (byte-identical), or — preferred — a third state, **"cw-s3 + everything general"**, with the two deployment deltas (gcs's attribution stack; cw's plan-first sweep) as the final commits on either side. Say which was reached.

## Seed material (use, don't reinvent)

- `tmp/hunk-owners.py` output: every delta hunk → the gcs commit that introduced it. That is the feature attribution.
- gcs's commit history (`git log gcs`) for the natural feature order and commit messages.
- The ledger's intended-divergence rows (`specs/branch-parity-discipline.md`) and the gcs-ward manifest (`specs/cp-from-cw-s3-2026-09-16.md`).

## Feature order (first cut — revise as entanglement shows)

1. Shared engine + attribution modules (py): identity/rules/prefixes/records/signals as pure modules with tests.
2. Attribution data path: `attr-index`, owner sidecars, the `usr` column in tiers (`index-write`), `/api/subtree` owner axis — **behind config**: with no attribution source the branch must behave exactly as cw does today.
3. Owner UI: `UserChip`, `/users`, `/user/:id`, color-by-user, hover top users.
4. `/assignments` + claims (write path) + the D1 tables they need.
5. Auth package (`@open-athena/auth` grants/tokens/SSO, `/admin`, D1 allowlist) **as an alternative** to edge Access — a store/deployment flag selects one; both must build and serve.
6. Marks: mark-state axis, `/marks`, totals/todo/estate.
7. Sweep executors side by side: gcs's owner-slice sweep and cw's plan-first sweep behind one `/sweep` UI — this is the expected entanglement point; document what had to change in the earlier checkpoints to make both fit.
8. Access log / read (atime) axis.
9. Class/pricing axis (a store flag: `prices`).
10. Digest twin (Discord) + weekly report.
11. `/api/bench`, index extras, warm-cache service token.
12. GCS job pipeline (`job/run.sh` etc.) next to the CW scan job — job selection by store.
13. Deploy config, e2e, og assets, docs.

## Checkpoint protocol (every commit)

- `cd site && pnpm build` (tsc -b + tsc -p functions + vite), `cd cloud && PYTHONPATH=src python -m pytest`, `pnpm -C packages/... test` when touched.
- Commit message: the feature, the gcs commits it draws from (`[from gcs: <sha>…]`), and one line on what earlier checkpoints had to change (`[entangled: …]` or `[clean]`).
- Milestones 2, 5, 7, 12: throwaway-harness CIC (local D1 + snapshot overlay), screenshot in `tmp/`.
- Mechanical verification at the end: the `tmp/ckpt-check.txt` loop over every commit — all green, or the pass isn't done.

## Deliverables

- The branch; a table (commit → feature → clean/entangled → files/±lines) in the ledger under a new "De novo factor" section; the list of seams (entangled features) as the input to the convergence spec; the gcs-ward items it discovers.
