# Bidirectional branch synchronization — 2026-10-13

Status: integration in progress. Ryan requested regular audits in both directions after discovering that m3 lacks the upstream `/scans` route.

The integration path is `cloud` → `local` → `m3`. Deployment branches merge their immediate parent; they never merge `cloud` directly into `m3`, and they do not rebase published commits. Shared changes authored here move up as reviewed `-x` cherry-picks: `[cloud]` to the root owner, `[local]` to `local`. m3 configuration, operational records, NAS configuration, and deployment state stay here.

Initial checkpoints: `cloud=c56a1525660bf701875812244145af5289ebf3bf`, `local=729feb255`, `m3=2b8a325b`. `local` is missing 350+ commits from cloud; m3 contains local and has 154 commits beyond it, including repeated parent merges and deployment-only work. Counts indicate audit scope, not a cherry-pick queue. Adapted picks must be checked by trailers, matching intent, and current behavior rather than patch-id alone.

Pre-merge audits are saved under `tmp/branch-audit-before-*`. The existing `.claude/cp.yml` in the root omits `local`, and `scripts/branch-audit` still assumes every deployment has `cloud` as its parent; updating that workflow is part of this change.

Implementation:

1. Pin a cloud checkpoint and resolve cloud→local in an isolated integration checkout, preserving the local CLI, file-store UI, executor, app-link, and migrations.
2. Test and advance the clean local branch by fast-forwarding to its tested integration merge, then merge that local checkpoint into m3. Preserve the physical disk/VM metadata, path-hidden guest behavior, subdaily capture IDs, and viewer-local timestamps.
3. Classify outstanding m3 commits against current parent trees and hand the root owner a concrete shared-pick queue. Put local-only engine features on local before returning to normal parent merges.
4. Complete the requested command-palette route ranking on the updated UI, verify in Chrome, and publish through the existing deployment workflow.

Cadence: audit incoming and outgoing changes when resuming work, before each deployment, and before/after a parent merge. Record exact checkpoints, deliberate remaining deltas, upstream picks/handoffs, tests, and deployment evidence. The audit must distinguish expected laptop/deployment differences from unreviewed shared drift; a zero-diff requirement against cloud would erase local support.
