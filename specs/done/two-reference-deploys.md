# Two reference deploys: Flask × local scans vs Vite+CFN × cloud-store scans

> **Superseded** by `cfn-demo-and-flask-localhost-peer.md` (2026-09-05). The
> "two archs, compare the branch diff" framing was dropped in discussion: Ryan
> has no story for a public Flask deploy, so it becomes one public **CFW demo**
> (a fleet union over public R2: nj-crashes + ctbk) with **Flask kept as the
> localhost peer** for laptop-disk cleaning — not a co-equal deploy. Original
> ask preserved below for provenance.

Written from the marin-gcs-usage session (2026-08-28). Context: marin runs the
**Vite + Cloudflare Pages Functions** www arch over cloud-store scans
(gcs.oa.dev, cw-s3.oa.dev — one long-lived branch per deployment, see marin's
`specs/branch-parity-discipline.md`); disk-tree ships the **Flask** arch
(`ui/` + `server.py`) over local/cloud scans. Nobody publishes the bare
CFN-arch reference — marin's is buried under auth, attribution, and mark & sweep.

## Ask

disk-tree publishes **two deploys from two branches**, each the minimal
reference impl of one www arch over the same scan/index core, so the extent to
which the archs can share code — and where they can't — is visible in the repo
itself rather than argued about:

1. **`flask`** (today's `main`): `disk-tree serve` — Python serves scans,
   diff, filter, histograms on the fly (`server.py`, diff index, vocab
   sidecar). Deploy: whatever hosts a Python process (Fly/Render/a VM) with a
   demo dataset (a local scan + one public-bucket scan).
2. **`cfn`**: Vite SPA + Pages Functions over a cloud store. Job publishes
   `snapshots/<date>/{tree,age,meta}.json` + `path-index.parquet` to a bucket;
   Functions serve `scans.json`, `/api/subtree` (pixel-budget drill over the
   path index via range reads), `/data/*` proxy, and diff as
   align-two-subtrees. No auth, no attribution, no marks — the "vanilla"
   marin. Deploy: a `*.pages.dev` over a public demo bucket (R2 is natural —
   free egress, same S3-compat lister).

Both consume `@disk-tree/react` and the Python core (`bulk-list`, `import`,
`tree_build`, access plane). The interesting artifact is the diff between the
branches: what is arch-intrinsic (serving layer, diff strategy, where
aggregation runs) vs. what leaked across by habit.

## Notes for the impl

- `cfn` can be seeded from marin's `site/functions/{data,api/subtree,api/
  path-index}` + `job/cw-run.sh` (the lightest job) with the marin-specific
  layers deleted — marin will keep CP'ing fixes from `cfn` back, so keep file
  shapes recognizable.
- Diff in the CFN arch is **not** a port of the Flask diff index: either
  align two `/api/subtree` responses (interactive), or a job-precomputed
  `diff.json` (marin's `job/cw-diff.py` runs `recursive_diff` offline).
- git-didi (`scripts/branch-audit` in marin) is the tool for keeping the two
  branches' shared surfaces at parity and their intended deltas explicit.
