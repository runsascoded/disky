# Branch harmony audit: trend each branch's own delta, classify it, and drive the accidental part to zero

**Status:** measured and classified 2026-11-12; `scripts/branch-harmony` built (one command, JSONL out). The remediation moves below are proposals for each deployment's session and the root session; nothing has been picked or dropped yet. First drafted 2026-10-17 (extending `scripts/branch-audit` and the "Monitoring" section of [`specs/done/one-clone-layout.md`][one-clone]). Uses git-didi's `merge` / `picks` ([`git-didi:specs/delta-invariance-audit.md`][didi spec]).

## Goal

Each long-lived branch differs from its parent only where it must: config, migrations, deployment glue, store-intrinsic code. That difference (its own delta) should be roughly constant, and never grow because a merge was skipped, a pick was adapted, a merge mangled the delta, or shared code was written on one branch. The audit measures the own delta, splits it into intrinsic and accidental parts, names the commits that put the accidental part there, and records it all so the trend is visible.

## Definitions

- **Own delta** of a child: `git diff -M merge-base(child, parent)..child`, in the code scope. Lines are `+` plus `−`.
- **Code scope**: everything except `specs/*`, `screenshots/*`, `*/fixtures/*`, `*/vendor/*`, `*/dist/*`, `site/bench/baselines/*`, `static-names/*` (gcs's DVX outputs), lockfiles and binaries. It is the scope of `~/c/oa/disky-deep-dive/scripts/branch_similarity.py`, so its 10-17 numbers compare a2a (the script reproduces them exactly for gcs, hf, r2, local, m3 and app).
- **Lag**: `merge-base..parent`, the parent's work not yet merged: commits, lines and the oldest commit's age. Lag is not own delta. A file whose blob already equals the parent's counts as `lag` (the next merge clears it), not as delta.
- **Lanes** (child → parent): `o/gcs`, `o/cw`, `o/hf`, `o/r2`, `r/local` → `r/cloud`; `r/m3`, `tauri-native-app` (app) → `r/local`.
- **Parity surfaces** (`one-clone-layout.md`): `packages/*/src`, `src/disk_tree`, `cloud/src`, `site/src`, `site/functions`. An own-delta file there is a **leak** until a rule says why it's intrinsic.

## Numbers

Own delta (lines / files) at three points: the 10-17 baseline (first-parent tips before 2026-10-18 00:00), just before the harmonization pass (tips before 2026-11-11 16:56, when `cloud`'s batch of deployment picks starts), and now (2026-11-12, `r/cloud` `d3c0c4afb`).

| lane | 10-17 | 11-11 16:56 (before pass) | now | Δ pass → now | lag now (commits / lines / oldest) |
|---|---:|---:|---:|---:|---|
| gcs | 5,858 / 75 | 11,049 / 171 | 9,360 / 142 | −1,689 | 12 / 391 / 0.9 d |
| cw | 7,377 / 86 | 14,714 / 184 | 13,019 / 137 | −1,695 | 8 / 148 / 0.9 d |
| hf | 1,483 / 29 | 2,499 / 57 | 2,311 / 60 | −188 | 3 / 6 / 0.4 d |
| r2 | 2,747 / 38 | 8,125 / 111 | 4,218 / 80 | −3,907 | 23 / 1,290 / 1.0 d |
| local | 16,316 / 170 | 17,223 / 184 | 17,223 / 184 | 0 | 384 / 55,767 / 9.3 d |
| m3 (vs local) | 5,269 / 105 | 6,759 / 123 | 6,803 / 123 | +44 | 0 |
| app (vs local) | 10,808 / 87 | 10,856 / 87 | 10,856 / 87 | 0 | 928 / 97,915 / 29.4 d |

- cw's 10-17 figure here is on today's `o/cw` first-parent history; the 10-17 draft measured the `cw-s3` lineage (7,328).
- In the before-pass figures, some lines were already byte-identical on the parent (picked to `cloud`, not yet merged back): gcs 11, cw 416, r2 1,900. Net of those, the pass took gcs 11,038 → 9,299, cw 14,298 → 13,013, hf 2,499 → 2,311, r2 6,225 → 4,160.
- Between 10-17 and the pass the deployment deltas roughly doubled (gcs +5.2k, cw +7.3k, r2 +5.4k). The pass removed about 7.5k lines, a third of that growth. `local`, `m3` and app weren't part of it: `local` last merged `cloud` on 11-10 and is 384 commits behind.

### The harmonization pass (2026-11-11)

`cloud`'s history from 11-11 16:56 to 22:18 lands 47 picks from deployment branches (`-x` sources: gcs 24, r2 10, cw 8, hf 3, m3 2), from `12c25c9b0` ("`/staged` anonymous unfurl …") to `435f099c1` (hf's `cf-preview` publish fix). 13 gcs picks on 11-04 04:28 (the level index work) preceded it. Each deployment then merged `cloud` back: gcs `be0884ff4` ("the parity survey's 32 picks") and `7dc0d7a94`; cw `b6a87f51a`, `c43887cea`, `8063502be`; hf `7146a8880`, `0fe8aa25e`; r2 `96db94363`. `git-didi picks -n lines` on the 48 picks since 11-08: 34 equal (18 exactly, 10 at `lines`, 6 at `context`), 3 differ, 11 with a different file set (adapted picks; `scripts/branch-harmony -p -s 2026-11-08` lists them).

### Today's own delta by class

| lane | intrinsic | accidental | mixed | lag | accidental share |
|---|---:|---:|---:|---:|---:|
| gcs | 5,797 | 3,496 | 6 | 61 | 37% |
| cw | 6,908 | 6,105 | 0 | 6 | 47% |
| hf | 2,160 | 151 | 0 | 0 | 7% |
| r2 | 2,946 | 1,179 | 35 | 58 | 28% |
| local | 17,222 | 1 | 0 | 0 | 0% |
| m3 | 6,803 | 0 | 0 | 0 | 0% |
| app | 10,733 | 0 | 0 | 123 | 0% |

About 10.9k of the four deployments' 28.9k own-delta lines are accidental. Classification is per file (`scripts/branch-harmony.yml`, one reason per rule). A `mixed` file holds both kinds, and its reason says which hunk moves.

## Classification

### Intrinsic (keep)

- **Deployment config**: `site/wrangler.toml` (+ `wrangler.dev.toml`), `site/deploy.env`, `site/index.html`, `site/package.json`'s `devPort`, `.github/cf-preview.json`, `site/scrns.config.ts`, `site/sheet-mirror.yml`. `wrangler.toml` is 140–310 lines per deployment and 220–470 lines apart pairwise: it really is per-deployment.
- **D1**: `site/schema.sql` (gcs's own tables), and applied recipes `git rm`'d (`admin-list`, `session-log-actor`, `scan-dispatch`; each branch removes them after applying, per the D1 workflow).
- **IaC and release config**: `infra/*` (each deployment's GCP / CF stack program and its `Pulumi.<stack>.yaml`), `deploy/*`, `.dvc`.
- **Scan pipeline glue**: `job/*` profiles, submitters, `digest.yml`, `static-names.json`, `scan-runs.json`, lifecycle snapshots, the cw isolation scripts, r2's `job/r2-scan`, and their tests (`cloud/tests/test_job_gcs.py`, `test_cw_*`, `job/tests/*`).
- **The store's entry**: `site/src/stores/<name>.tsx`, `stores/meta.ts`'s per-deployment meta store, gcs's Cloud Console links (`consoleLink.test.ts`), hf's Hub links (`hubLink.test.ts`).
- **Store-intrinsic code**: hf's classes in its own terms (`private_bytes` / `git_bytes`, `b8281f611`: interval store, static names, prefixes, owner bands, scope) and gcs's named class columns (`CLASS_COLS`; both regenerate the `generation-off.json` golden). This is the "deploy code looks standalone" case.
- **Docs**: `README.md`, `AGENTS.md`, `API.md`, `docs/`, m3's `log.md`.
- **The laptop line**: all of `local` (capture, APFS, drainer, staged deletes, laptop routes, device enrollment) and app; m3's owner stripping (`64a076ed8`), AWS ingest stack and Pulumi state (public by design).

### Accidental, by size

| lines | lane(s) | what | commits | move |
|---:|---|---|---|---|
| 4,835 | cw | scan-history thinning + admin review (38 files: `history_*.py`, `*_thinning.py`, `historyAdmin.ts`, `HistoryAdmin.tsx`, `scanRetention.ts`, routes, `/scans` and `/admin` hunks) | `b4338a37a`, `50ce9fcec` (both `[cloud]`, 11-10, unpicked) | pick to `cloud`; cw merges back |
| 803 + | gcs, cw | diff-lookups hold: `cloud`'s `dd6b0650`, `4bc60b3c`, `e7c3ba97`, `196896c5` reverted on gcs and cw (`api/diff.ts`, `diffLookups.test.ts` deleted, 4 test files, cw's `view.ts`; gcs's `view.ts` / `index.ts` share is in the 565 row) | gcs `a12a3bd02`, `b372b4108`; cw `410f0338e` | root: measure the 2.7× diff body, then keep the lookups (gcs and cw drop the revert) or revert on `cloud`. Until then a `cloud` flag, not a code revert |
| 608 | gcs | `owner_prefix_aggs` carried across a compaction (`owner_carry.py`, runner, append, profile, test) | `4c49983d2` | pick to `cloud` (the aggs are `cloud`'s; the opt-in stays per profile) |
| 590 | gcs | the temporary session monitor, expired 2026-10-20/21: module, route, tests, CLI, `Env` | `ae64382ba`, `1a86377eb`, `cdba30d25` | gcs drops it |
| 565 | gcs | `view.ts` / `index.ts`: rich static views + generation diagnostics (plus the hold above) | `a6e393415`, `552411a40` | pick to `cloud` |
| 563 | gcs, cw, hf, r2 | job image build: 4 copies of `build.sh` / `Dockerfile` / `cloudbuild.yaml`; image-sha stamping landed 4× (gcs `7987fc159`, cw `6e1dbb777`, hf `74b3b7ac1`, r2 `2b978ee2d`), the pages-kit copy 2× | — | one `cloud` `job/build.sh` + `job/Dockerfile`, parameterized by an image env file per deployment |
| 542 | r2 | R2 access-lens checkpoint receipts, on `cloud`'s `r2_checkpoint` and r2-access worker | `58a965038`, `165de5db8`, `ae50fe500` | pick to `cloud` |
| 476 | gcs, cw | `job/gen-delta-arrows.py`, `gen-arrow-avatars.py`, `icons/_headers` byte-identical, `rerg` 4 lines apart; absent from `cloud`; flagged 10-10 and 10-17 | `8fd4ff36d`, `e3ca79ec6` (09-28) | pick to `cloud` `job/`; both drop |
| 378 | gcs, cw | `job/static-names.sh`: one launcher, 120 of ~190 lines apart; the per-deployment part is already in `job/static-names.json` | — | one `cloud` launcher reading the profile |
| 299 | r2 | sign-in wall "Continue as <you>" + cookie-name tests | `0a30c225a`, `738a5c6e4` (`[cloud]`, today) | pick to `cloud` (in flight) |
| 229 | gcs | static-generation request diagnostics (`cf_usage.py`, `usage.ts`, middleware, tests) | `552411a40` | pick to `cloud` |
| 211 | gcs | rich static views / unavailable ownership: 19 site files | `a6e393415`, `c1f9b17d1`, `2331cd82c`, `f2d638e2d` | pick to `cloud` |
| 175 | r2 | name-summary ids, diff-bench, version migration, microsecond stamps (10-13), `build.ts` doc, infra test group | `a3a6cb118`, `4bb7b119d`, `27c5c8752`, `d288b8c4b`, `7eced62e3`, `4aca2324f`, `7018a840d`, `02bc73fa3` | pick to `cloud` |
| 164 | gcs | compact `/scans` rows, job-type filters, output sizes | `74bfa26d5`, `b2abddfa7` | pick to `cloud` |
| 96 | gcs | standalone-run job kinds (`JobKind.single_phase` / `nop_marker`, health, `scanRuns.ts`) | `f809c64ed` | pick the mechanism; gcs keeps its `scan-runs.json` entry |
| 87 | gcs | `scripts/spec-audit.py` + test | — | pick to `cloud` |
| 81 | r2 | the shared CF stack program (`infra/cf/stack/__main__.py`) carrying OA bucket names and `oa-r2-usage` token names | `dad8785ee`, `06c68fc11` | r2's own `infra/cf/r2_stack.py` (as cw and hf have); generic parts behind config |
| 43 | cw | shared fixes on cw only: `test_append_runner.py`'s monkeypatch target, require-clean's image half, `.gcloudignore`'s pages-kit, a `specs/done/` comment | `89889995e` et al. | pick |
| 40 | all four | `site/src/oaPeers.ts`: byte-identical on gcs, cw, hf, r2, absent from `cloud`; one change landed 4× (`4e24e505f`, `45f8a6f25`, `c62d0efb2`, `6e9cb1738`) | — | config: a peers var each `wrangler.toml` sets, read by `cloud`'s `peers` |
| 35 | r2 | `ci.yml`'s `r2-collector` job tests `cloud`'s r2-access worker | — | pick that job |
| 32 | gcs | `gatesDeployment.ts` rows that `cloud`'s `gates.test.ts` now has (`/auth/oa*`, `/runs/<id>`, `/og/run.png`) | — | gcs drops them |
| 28 | cw, hf | `infra/cf/run` byte-identical | — | pick to `cloud` |
| 17 | cw | `prompt.txt`: a pasted chat prompt at the repo root | — | cw drops it |
| ≤ 12 | gcs, cw, local | template drift in `wrangler.example.toml`; `StoredBurst` moved on gcs only (merge residue); `_cut`'s worker split in `interval_store.py`; a comment path in `guest-chip.spec.ts`; local's lost blank line in `staticFilter.test.ts` | — | pick, or take `cloud`'s |

What found these (each is a column or flag in the script's output):

- **Tagged but unpicked**: `[cloud]` commits whose lines survive in a leak file (`-c`). cw's `b4338a37a` is two days old and the largest single item.
- **Untagged shared work**: untagged commits whose lines survive in leak files. The untagged 11-07/11-10 commits on gcs and r2 ("Preserve …", "Accept …", "Prepare …") are all of this kind.
- **Duplicates**: the same blob on ≥ 2 siblings, absent from or different on the parent (`dup`). Near-duplicates show as `shared`.
- **Holds**: a branch reverting a parent commit (gcs and cw's diff lookups). Holding a change back is legitimate staging (`stage-and-bm-before-prod`), but done as a code revert it is own delta on two branches and conflicts on every `view.ts` change. Done as a flag it would cost nothing.
- **Expired temporaries**: code whose own commit message dates its end (the session monitor).
- **Stale copies**: deployment rows or files that a later `cloud` change made redundant (gcs's gate rows).

### Could not classify with confidence

- **`local`'s edits to shared site files** (`stores.ts` 80, `ChildrenTable.tsx` 53, `App.tsx` 45, `index.ts` 40, `view.ts` 8, `DiffTable.tsx`, `colors.ts`): classed intrinsic as `[local]` laptop features, but age buckets (`ag`) and `Store.home` may be wanted on `cloud`. 7,182 of local's lines are `merge-only` (kept through merges as `cloud` deleted the laptop code). That is expected there, not a smell.
- **m3's deletions vs local** (owners, ledger, assignments: 4,172 lines removed) are intentional per `64a076ed8`. But they mean `cloud` and `local` still carry gcs-style ownership "bones" that m3 strips on every merge.
- **`infra/cf/sync-secrets`** (cw 115, r2 187 lines, 276 apart) and `infra/gcp/__main__.py` (164–377 lines per deployment, 245–503 apart): intrinsic today. A shared GCP stack program (as `infra/cf/stack/`) would shrink them, but that's a design call.
- **`job/digest.yml`** (29–69 lines, 60–105 apart): config, kept intrinsic.

## Remediation plan, by owner

Every pick follows `AGENTS.md`: the owning session makes (or names) the `[cloud]` commit and sends its sha to the root session, which picks it with `-x`; the deployment then merges `cloud`. Drops are ordinary commits on the deployment branch. Re-run `scripts/branch-harmony -c` after each merge: the accidental column should fall by the listed lines.

**Root session (`cloud`)**
1. Pick cw's `b4338a37a` + `50ce9fcec` (−4.8k on cw) and r2's `0a30c225a` + `738a5c6e4` (−0.3k).
2. Decide the diff-lookups hold (−0.8k on gcs and cw, plus gcs's share of `view.ts` / `index.ts`): measure the 2.7× body, then keep the new lookups or revert them on `cloud`. If the measurement takes more than a day, add a flag so gcs and cw can drop their reverts now.
3. Land the consolidations below on `cloud`: `job/gen-*`, `rerg`, `icons/_headers`, `infra/cf/run`, one `job/build.sh` + `Dockerfile`, one `static-names` launcher, peers as config.
4. Merge `cloud` into `local` (384 commits, 9.3 days behind); then m3 and app merge `local`. App is 29 days behind.

**gcs-7f (gcs)**
1. Drop the session monitor (`ae64382ba` and follow-ups), and the `gatesDeployment.ts` rows that `cloud` has (−0.6k).
2. Send as `[cloud]` commits: `a6e393415` + `c1f9b17d1` + `2331cd82c` + `f2d638e2d` (rich static views), `552411a40` (generation diagnostics), `4c49983d2` (owner carry), `74bfa26d5` + `b2abddfa7` (`/scans` rows), the job-kind half of `f809c64ed`, `scripts/spec-audit.py`, `_cut`'s worker split, the `StoredBurst` move, the `guest-chip` comment (−1.9k).
3. Once the root's consolidations merge: delete `job/gen-delta-arrows.py`, `gen-arrow-avatars.py`, `icons/_headers`, `rerg`, the image build trio and `static-names.sh` in favour of `cloud`'s, and `site/src/oaPeers.ts` for the peers var (−0.6k).
4. Drop the lookups revert when the root decides.

**disky-cw (cw)**
1. Send `b4338a37a` + `50ce9fcec`'s shas to root (they're already `[cloud]`).
2. Drop `prompt.txt`. Send the `test_append_runner.py`, require-clean image half, `.gcloudignore` and `hostRedirect.ts` comment fixes as one `[cloud]` commit.
3. Once the consolidations merge: delete the duplicated `job/gen-*`, `rerg`, `icons/_headers`, image build trio, `static-names.sh`, `infra/cf/run` and `oaPeers.ts`.
4. Drop the lookups revert (`410f0338e`) when the root decides.

**disky-hf (hf)**: once the consolidations merge, delete `infra/cf/run`, `job/build.sh` / `Dockerfile` / `cloudbuild.yaml` and `oaPeers.ts` (−0.15k). The rest of hf's delta is intrinsic, which makes it the model for the others.

**disky-r2 (r2)**
1. Send to root: `0a30c225a` + `738a5c6e4` (in flight); the receipts (`58a965038`, `165de5db8`, `ae50fe500`) with their r2-access and `scan_steps` hunks; the shared fixes (`a3a6cb118`, `4bb7b119d`, `27c5c8752`, `d288b8c4b`, `7eced62e3`, `4aca2324f`, `7018a840d`, `02bc73fa3`, `infra/pyproject.toml`'s test group); `ci.yml`'s `r2-collector` job (−1.1k).
2. Move r2's additions to `infra/cf/stack/__main__.py` into `infra/cf/r2_stack.py`.
3. Once the consolidations merge: delete `job/Dockerfile` / `cloudbuild.yaml` and `oaPeers.ts`.
4. Push `r2`: the local branch is ahead of `o/r2`, and the audit reads `o/r2`.

**disky-m3 (m3)**: nothing accidental. Merge `local` once `local` has merged `cloud`.

If every move lands, the deltas end at about gcs 5.8k, cw 6.9k, hf 2.2k, r2 2.9k, all intrinsic. That is about the 10-17 level, with the features since included.

## Tooling: `scripts/branch-harmony`

`scripts/branch-harmony [-a WHEN] [-c] [-f] [-m] [-p] [-s SINCE] [LANE…]` (Python, `uv run` shebang, click). Rules in `scripts/branch-harmony.yml`; tests in `tests/test_branch_harmony.py` (a synthetic repo, exact equality throughout).

- Prints one row per lane: own `+/−`, lines, files, lines by class (`intrinsic`, `accidental`, `mixed`, `lag`, `unclassified`), lag (commits, lines, oldest age), and lines by flag (`leak`, `dup`, `shared`, `cosmetic`, `merge-only`, `parent-equal`).
- `-a` measures as of a time (first-parent tips before it), for baselines and trends.
- `-f` lists every own-delta file with its class, flags and reason. A new `unclassified` file is the prompt to add a rule or move the file.
- `-c` blames each lane's surviving leak-file lines to the commits that wrote them, marking each `tagged` (`[cloud]` / `[base]`), `untagged`, or `merge` (a merge's resolution). This is the pick list.
- `-m` checks merge invariance of the lane's parent merges since `-s` (`git-didi merge -n lines`); `-p` checks pick fidelity (`git-didi picks -n lines`).
- Appends one JSON line per lane per run to `tmp/branch-harmony.jsonl` (`-o`; `-O` turns it off): refs, tips, merge-base, own delta, lines per surface, class and flag, lag, every file, and with `-c` / `-m` the pending commits and merge results.

`scripts/branch-audit` (pairwise `git-didi stat` per surface) stays for the `/cp` skill. This script is the trend and the classification.

### Thresholds (initial)

| check | warn | fail |
|---|---|---|
| accidental lines (non-laptop lanes) | any new since the last run | > 1,000 |
| own delta growth since the last run (non-app) | > 500 lines | > 2,000 lines without a spec / ledger note |
| lag | > 2 days or > 30 commits | > 7 days |
| `[cloud]`-tagged lines surviving in leaks (`-c`) | any older than 1 day | any older than 3 days |
| `dup` files | any | byte-identical on ≥ 3 siblings |
| `unclassified` files | any | — |
| merge invariance | any `differs` at `context` | `differs` in a parity surface |

### Where it runs

- After every `git merge cloud` / `git merge local` (a `post-merge` hook beside `pre-push`), and as the last step of `/cp`.
- Optionally nightly on `cloud` (fetching `o` and `r`), posting only on `fail`.

## Open

- Whether `static-names/` (DVX outputs on gcs) belongs on the branch at all.
- NCD over the code scope tracks the line counts closely, so it adds little as a gate; keep it in the deck data.
- Whether `cloud` should keep the ownership "bones" m3 strips.

[one-clone]: done/one-clone-layout.md
[didi spec]: ../../git-didi/specs/delta-invariance-audit.md
