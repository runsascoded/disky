# Branch harmony audit: trend each branch's own delta, check picks and merges

**Status:** draft, 2026-10-17. Extends `scripts/branch-audit` and the "Monitoring" section of [`specs/done/one-clone-layout.md`][one-clone]; complements [`specs/deployment-divergence.md`][divergence] (what the deltas contain) with how they move. Depends on git-didi features drafted in [`git-didi:specs/delta-invariance-audit.md`][didi spec].

## Goal

Each long-lived branch's difference from its parent should be intrinsic (config, migrations, deployment glue) and roughly constant: it should not grow because a merge was skipped, a pick was adapted, or a merge mangled the delta. The audit measures that after every merge and every pick, and over time.

## What exists

`scripts/branch-audit` runs `git-didi stat` per surface since each pair's merge-base and prints `parity` or "N files differ". It doesn't record anything, so there's no trend. It compares a pair's two sides since their merge-base, not a branch's delta before and after a merge. It also detects "parity" by grepping for git-didi's stderr text.

## Evidence (2026-09-28 → 10-17; `~/c/oa/disky-deep-dive/data/branch-similarity.json`)

Code scope: everything except `specs/`, `*/fixtures/*`, `*/vendor/*`, lockfiles, `site/bench/baselines/`, `static-names/` (gcs's DVX outputs, ~51k JSON lines), and binaries.

- **The tip-to-tip diff is a poor alarm; most of its movement is lag.** `cloud`↔`local` went from 16k to 111k changed lines on 10-09 and back to 22k on 10-13. Over those days `local`'s own delta (`merge-base..local`) stayed at 13.3k, while `cloud` landed ~96k lines that `local` hadn't merged yet. `tauri-native-app` is in that state today: 46.9k tip-to-tip, of which 36.5k is lag (it last merged `local` on 10-14).
- **Own deltas today** (+/− lines): gcs 5.9k, cw 7.3k, hf 1.5k, r2 2.7k, local 16.3k, m3 5.3k, app 10.8k.
  - Most are step-wise, not drifting: gcs 4.5k → 3.2k (10-01 restructure) → 5.5–6.6k, with a one-day 8.3k spike on 10-09.
  - Steps up: cw 3.9k → 6.6k on 10-13 (the `cw-isolation-*` migration scripts); m3 2.2k → 5.2k on 10-14.
- **Picks are faithful.** Of 405 `-x` picks, 331 are identical modulo line numbers, 30 more modulo context, and 44 were adapted (31 content, 13 file set).
- **Merges mostly preserve the delta.** Of 18 recent `cloud` merges into gcs/cw, 8 leave the own delta byte-identical. The larger residuals are absorbed picks. The rest are line shifts, or same-line edits that only a token-level comparison would call equal.
- **Duplicated own code.** `job/gen-delta-arrows.py`, `job/gen-arrow-avatars.py` and `job/rerg` are byte-identical on gcs and cw and absent from `cloud`; `infra/gcp/__main__.py`, `job/digest.yml`, `Dockerfile` and `job/static-names.sh` are edited in the own deltas of 2–4 siblings. `deployment-divergence.md` flagged these on 10-10; they're still there.

## Audit

A script `scripts/branch-harmony` (Python, on `cloud`; pairs from `.claude/cp.yml`, each child against its immediate parent). For each child, at its tip:

1. **Own delta**: `+/−` lines and files of `mb(child, parent)..child` in the code scope, with a per-surface breakdown (parity surfaces vs intrinsic paths, as in `one-clone-layout.md`).
2. **Lag**: `mb..parent` size, plus the number of parent commits not yet merged and the oldest one's age.
3. **Merge invariance**: for each first-parent merge of the parent since the last run, `git-didi delta`/`merge M` at `--normalize lines`. Report files `equal` / `absorbed` / `equal@context` / `differs`.
4. **Pick fidelity**: `git-didi picks` over picks landed since the last run; list the non-equal ones with their subjects.
5. **Duplicates**: files present in ≥ 2 siblings' own deltas, flagged hard when byte-identical (same blob) and absent from the parent: candidates for `cloud`.
6. **Leaks into parity surfaces**: own-delta files under `packages/*/src`, `src/disk_tree`, `cloud/src`, `site/src`, `site/functions`, which `one-clone-layout.md` says should print `parity`.

Output: a human summary to stdout. Append one JSON line per child per run to `tmp/branch-harmony.jsonl`, and a dated row to the branch's ledger (`specs/branch-parity-discipline.md` where one exists) at each merge. The ledger trend is the "intrinsic delta only goes down" check `one-clone-layout.md` asks for.

## Thresholds (initial, tune after two weeks)

| check | warn | fail |
|---|---|---|
| own delta growth since last run (non-app) | > 500 lines | > 2,000 lines without a spec/ledger note |
| lag | > 2 days or > 30 commits | > 7 days |
| merge invariance | any `differs` at `context` | `differs` in a parity surface |
| pick fidelity | adapted pick without a note in its message | file set differs on a parity surface |
| duplicates | ≥ 2 siblings edit a file | byte-identical file on ≥ 2 siblings, absent from parent |
| parity-surface leak | any new file | — |

`tauri-native-app` is exempt from growth thresholds (its delta is expected to shrink by upstreaming); `local`'s growth is expected while laptop code moves out of `cloud`.

## Where it runs

- Locally after every `git merge cloud` / `git merge local` (a `post-merge` hook in `scripts/hooks/`, alongside `pre-push`), and as the last step of the `/cp` skill.
- Optionally a nightly GHA on `cloud` that fetches all deployment branches from both remotes. It would post the summary only on `fail`.

## Open

- Whether `static-names/` (DVX outputs on gcs) belongs on the branch at all, or in DVX remote storage only.
- NCD over the code-scope text tracks the line counts closely (latest: gcs↔cloud 0.037, cw 0.039, hf 0.012, r2 0.015, local 0.085, m3↔local 0.029, app↔local 0.176), so it adds little as a gate. Keep it in the deck data only.

[one-clone]: done/one-clone-layout.md
[divergence]: deployment-divergence.md
[didi spec]: ../../git-didi/specs/delta-invariance-audit.md
