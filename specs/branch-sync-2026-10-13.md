# Bidirectional branch synchronization — 2026-10-13

Status: parent integration complete; deployment verification and root-owner cherry-pick handoff in progress. Ryan requested regular audits in both directions after discovering that m3 lacks the upstream `/scans` route.

The integration path is `cloud` → `local` → `m3`. Deployment branches merge their immediate parent; they never merge `cloud` directly into `m3`, and they do not rebase published commits. Shared changes authored here move up as reviewed `-x` cherry-picks: `[cloud]` to the root owner, `[local]` to `local`. m3 configuration, operational records, NAS configuration, and deployment state stay here.

Initial checkpoints: `cloud=c56a1525660bf701875812244145af5289ebf3bf`, `local=729feb255`, `m3=2b8a325b`. `local` is missing 350+ commits from cloud; m3 contains local and has 154 commits beyond it, including repeated parent merges and deployment-only work. Counts indicate audit scope, not a cherry-pick queue. Adapted picks must be checked by trailers, matching intent, and current behavior rather than patch-id alone.

Pre-merge audits are saved under `tmp/branch-audit-before-*`. The existing `.claude/cp.yml` in the root omits `local`, and `scripts/branch-audit` still assumes every deployment has `cloud` as its parent; updating that workflow is part of this change.

Implementation:

1. Pin a cloud checkpoint and resolve cloud→local in an isolated integration checkout, preserving the local CLI, file-store UI, executor, app-link, and migrations.
2. Test and advance the clean local branch by fast-forwarding to its tested integration merge, then merge that local checkpoint into m3. Preserve the physical disk/VM metadata, path-hidden guest behavior, subdaily capture IDs, and viewer-local timestamps.
3. Classify outstanding m3 commits against current parent trees and hand the root owner a concrete shared-pick queue. Put local-only engine features on local before returning to normal parent merges.
4. Complete the requested command-palette route ranking on the updated UI, verify in Chrome, and publish through the existing deployment workflow.

Cadence: audit incoming and outgoing changes when resuming work, before each deployment, and before/after a parent merge. Record exact checkpoints, deliberate remaining deltas, upstream picks/handoffs, tests, and deployment evidence. The audit must distinguish expected laptop/deployment differences from unreviewed shared drift; a zero-diff requirement against cloud would erase local support.

## Outgoing classification

Already upstream: `47db8b14` → cloud `5e67abc8` (capacity bar); `fc7fdb10` → local `0eb42736` (capture metadata); auth hint/location/expiry commits `015d2c23`, `54b4e7f9`, `f10aea25`, `48316efa` have corresponding parent commits. Older file-store display, executor, app-link, du, age-strata, and drainer work is already retained by local. The original `[base]` tags do not imply that laptop-only code belongs back on cloud.

Cloud review queue, in dependency order: `a96496b4` (keyboard-only help focus); `5671c86a` (path-hidden guest responses/raw-route denial), `3109d771` (Hide grant UI), `ef17b432` (secondary-store hint), `eba1ca5b` and `9653a8c3` (largest files API/UI), `0483510c` (largest respects hidden paths), `d5612dd5` (redeem guest keys before routing and encode path segments), `5f2fc258` (VM and physical disk diff UI), `2b8a325b` (viewer-local timestamp details). Current cloud has none of their matching trailers or feature implementations. Picks require adaptation to its newer scan-ID/route helpers; m3 configuration must not accompany them.

Local queue: `2f5c7c3a` (SSH capture), `b6a8a36d` (preserve captured_at and reject occupied snapshot slots), `66cf48d6` (VM volume metadata). `fc7fdb10` is already there and must not be picked again.

Superseded/adaptation review: `19745cfb`'s exact old scan-history implementation is superseded by cloud's `snapshot_scans` and `scan_id` helpers. `8e8c064c` has useful single-day grouping and date-only capture-time behavior, but its scan model must be reconciled with cloud's newer short-slug resolution and `scanTimes`; it is not a blind cherry-pick. `c8b651df`'s deployment-aware dev identity should be retained on local/m3; a hard-coded gcs/cw stub would regress laptop access. `59a462fc` is the m3 adaptation of largest files and home tooltips, not a second shared feature.

Deployment-only: NAS module/config, host routes, AWS snapshot publishing glue, IAM/Pulumi state, launchd/app operational records, log.md and live privacy-check evidence stay on m3. No whole-bucket public settings or R2 demo branch push is part of this integration.

## Landed checkpoints and checks

`local` advanced from `729feb25` to `7c2da9ba`: merge `4615459c` brings in committed cloud `c56a1525`; capture picks `404c0d70`, `39da3257`, `7c2da9ba` retain their original `-x` trailers. The 11 conflict files preserve local age-strata, file-store paths/home, app-link, executor migrations and the newer shared view/scan helpers. The parent audit now includes local→cloud and m3/app→local, two-way counts and tagged outgoing candidates.

`m3` then merged this actual local checkpoint in `86a5d59b` and added the shared palette fix in `51ff1c25`. The 12 conflict files preserve post-cache guest filtering (including partial-cache upgrades), key redemption before routing, home-path encoding, VM/physical disk diffs, local timestamp details and legacy capture-time decorations. New unfilterable search, health and scan-run endpoints deny path-hidden grants. The ordinary viewer policy and private deployment configuration stay unchanged.

Validation: parent engine 510 passed/1 skipped, cloud 1280 passed/2 skipped, site 1975 passed/1 skipped, widgets 200 passed; m3 site 2025 passed/1 skipped before the palette regression, focused palette/guest/scan checks 41 passed, and both workspace builds passed. Capture picks: 18 Python and 6 laptop-dispatch tests passed. Post-merge m3/local engine and cloud surfaces are at parity; remaining UI differences are the classified shared fixes and m3 configuration. Logs are under `tmp/local-sync-*`, `tmp/m3-sync-*`, and `tmp/branch-audit-*`.

The `/scans` route now exists, with the shared run-history UI. Its AWS recording/backfill adapter is still separate work: the upstream recorder/backfill targets GCP Batch, and m3 has not previously recorded its AWS ingest jobs into `scan_runs`. Showing that page does not fabricate a history for existing laptop scans.
