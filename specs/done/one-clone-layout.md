# One clone, N worktrees — `disky` (né disk-tree) as the single repo behind every deployment

**Status: done 2026-09-28.** Written by the gcs session, executed the same day (gcs, then the root, then cw-s3 and m3 moved; the old clones were retired). The per-machine migration mechanics (session moves, clone scripts) lived in that session's transcript and memory and are not repeated here; what follows is the durable policy.

## Layout

One clone, `~/c/disky`, with **one linked worktree per long-lived branch** under `wt/` (untracked; globally ignored). Every peer branch lives in one object store, so merges, cherry-picks and diff-of-diffs between `cloud`, `gcs` and `cw-s3` are plain local operations; adding a deployment is `git worktree add wt/<name> <name>`.

| path | branch | pushes to | role |
|---|---|---|---|
| `~/c/disky` | `cloud` | `r` (runsascoded/disky) | the base: core library work + the public demo site (r2.rbw.sh) |
| `wt/gcs` | `gcs` | `o` (Open-Athena/marin-gcs-usage) | gcs.oa.dev |
| `wt/cw-s3` | `cw-s3` | `o` | cw-s3.oa.dev (the branch formerly `cw-s3-next`; the old lineage is tag `cw-s3-legacy`) |
| `wt/m3` | `m3` | `r` | this Mac as a deployment: the laptop disk-cleanup loop, free to change the CLI in place |
| `wt/app` | `tauri-native-app` | `r` | the macOS app + native walker — a feature line, not a deployment |
| `wt/mgu-scale`, `wt/flask`, `wt/www` | as named | `r` | as needed |

The root worktree (`cloud`) owns **core library work** (what every deployment merges in) and the public demo. Deployment worktrees own only their deployment delta. `.claude/cp.yml` at the root lists the peers and the parity surfaces; a linked worktree has no copy of its own, so tooling falls back to the main worktree's.

`gh` needs a default repo and the shared `.git/config` can't hold one per worktree (two remotes), so each worktree's `.envrc` exports `GH_REPO` (`runsascoded/disky` at the root and in `wt/m3`/`wt/app`, `Open-Athena/marin-gcs-usage` in `wt/gcs`/`wt/cw-s3`).

## Model: deployments merge from upstream, never rebase

Ryan's decision, 2026-09-28. Consequences:

- **Deployment SHAs are stable.** No history rewrites under another session's cursor; `refs/cp/*` cursors and the `*-prod` / `*-dev` deploy pointers stay valid across upstream bumps.
- **The delta is a tree diff, not a commit list.** `git log cloud..gcs` accumulates forever after the first merge; the delta that matters is `git diff cloud...gcs` restricted to the parity surfaces.
- **Base work is authored on `cloud` in the root worktree and merged down**, then exercised on a deployment (`site/deploy --dev`). A base fix that has to be written on a deployment first (it needs that deployment's data to reproduce) is the exception: commit it there with the `[base]` prefix; the root `git cherry-pick -x`es it onto `cloud`; the next merge down reconciles (identical patches merge clean). `git cherry cloud <branch>` finds the ones not yet picked, by patch-id.
- **A merge never touches the delta's intent.** Conflicts resolve toward the base; if the base and the delta genuinely disagree, the fix is a base change (a wider config seam) or a ledger entry, never a deployment-side rewrite of base code.

## Monitoring: the delta must be minimal and intrinsic

Before and after every `git merge cloud` on a deployment branch, run `scripts/branch-audit` (git-didi per surface since the pair's merge-base; pairs and surfaces from `.claude/cp.yml`) and compare.

- **Intrinsic delta** = what a deployment *must* differ in: `site/wrangler.toml` (+ `[env.preview]`), `site/migrations/<store>/`, `site/index.html` + `public/og.jpg`, `cf/__main__.py` (never `cf/cfn_dashboard.py`), `job/` (its cloud's snapshot pipeline), deployment specs and the two READMEs, the deploy/health workflows.
- **Parity surfaces** — `packages/*/src`, `src/disk_tree`, `cloud/src`, `site/src`, `site/functions` — must print `parity`. A diff there is either base-bound work not yet cherry-picked up, or a regression from a merge. A new surface in the "differ" column after a merge is the alarm.
- **Trend**: the intrinsic delta's `--stat` total, appended to the ledger (`specs/branch-parity-discipline.md`) at each merge, should only go down as seams widen (the `Store` registry, `wrangler.toml` `[vars]`, `cf/` per-deployment wiring exist to pull code out of the delta). `tauri-native-app` is the opposite case: a large code delta expected to shrink as pieces merge into `cloud`.

## Push safety

Both repos are public, so this is about clutter and confusion, not leakage, but it is mechanical:

1. **Per-branch push targets** in the shared `.git/config`: `branch.<b>.remote` + `pushRemote` = `r` for `cloud`/`flask`/`www`/`dev`/`mgu-scale`/`tauri-native-app`/`m3`, `o` for `gcs*`/`cw-s3*`; **no `remote.pushDefault`**, so a bare `git push` on an unconfigured branch fails rather than guesses.
2. **The `pre-push` guard**, tracked as `scripts/hooks/pre-push` and installed by `scripts/hooks/install`: deployment refs never reach `r`, base refs never reach `o`, any other remote (ec2 nodes) is unconstrained. The install sets a **local, absolute `core.hooksPath`** for the whole clone, because this machine's global `core.hooksPath` is pnpm-dep-source's, whose chain into `.git/hooks` drops `"$@"` (the guard would see no remote/url) and loops if the local hook chains back; the local hooks inline `pds check` instead. `git push --dry-run` runs the guard, so dry-runs are the test.
3. **`site/deploy` keeps `git push o …` hardcoded**, so the Open-Athena remote must be named `o`.

## The rename: disk-tree → disky

Done 2026-09-28: the directory, the GitHub repo (`gh repo rename`; `runsascoded/disk-tree` redirects), the remote URLs, and the Claude session dirs. **Not renamed**: the package and CLI names (`disk_tree` / `disk-tree` on PyPI, `@disk-tree/react`, `dt-cloud`), the `~/.config/disk-tree/` index dir, and the in-repo prose. A project name that differs from its package names is fine indefinitely; renaming those is a code change with its own compat story (no PyPI redirects; `pip install disk-tree` users; the `pip install git+…#subdirectory=cloud` line in the OA docs). Name check that day: PyPI `disky` is free; npm `disky` is taken (a Discord bot framework), so the react package would need the `@disky` org; GitHub `runsascoded/disky` was free. Own spec if ever wanted.

## Open

- **`m3`'s upstream cadence**: the cleanup loop changes the CLI faster than the OA sites change the site, so the root should survey `m3` (`git cp m3`) more often than the OA pair; a growing `m3` delta on `src/disk_tree` means upstream is falling behind, not that `m3` is wrong.
- **Project auto-approve rules in a linked worktree**: confirm the hook resolves `.claude/hooks/auto-approve.yml` from the worktree root, else the deployments' project rules merge into the root's file.
- **Memory is shared across worktrees** (Claude Code keys the auto-memory dir by the main worktree), so one `MEMORY.md` serves every session in the clone, sectioned by deployment.
- **`pre-public-original`** stays local-only (it predates the history purge; never push).
