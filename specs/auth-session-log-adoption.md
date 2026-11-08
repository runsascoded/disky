# Adopt `@open-athena/auth`'s shared session log

Handoff from the `oa/auth` session, 2026-10-25. disky's session log (`specs/done/session-log.md`) was extracted into `oa/auth` (its `specs/done/ctbk-session-logging-extraction.md`), so CTBK and other apps can reuse it. disky should now use the shared modules and delete its own copies.

Selected by Ryan for implementation on 2026-11-08, after portable query executor Phase 1. Preserve the existing private activity and staged-deletion integrations; adopting the package's separate activity feed is outside this change.

## Pin

Use the verified immutable build `github:Open-Athena/auth#2c9be8e54a709c8a41e2aab2372cb98e501bdc55` (source `22b5eae`; installed-artifact CI `42431492174`). Disky currently pins `184b574b21f1814b4b5e6c933cfdc97150d8c23f`. Read auth's README sections "Session Log" and "Several Accounts in One Browser", plus its `docs/activity-adoption.md`. Several-account mode remains opt-in and is not enabled by this spec. Broker capability changes in this pin fail closed for unsupported `prompt=login` and `max_age`; ordinary sign-in is unchanged.

## What replaces what

| disky | auth |
|---|---|
| `site/functions/_lib/sessionLogShape.ts` | `@open-athena/auth/session-log` (shapes, `LIMITS`, `checkAppEvents`) |
| `site/functions/_lib/sessionLog.ts` | `@open-athena/auth/session-log/d1` (`d1SessionLogStore(db)`) |
| `site/functions/api/session-log/[[path]].ts` | `sessionLogRoutes({ store, identify, requireAdmin, appEvents, filterEvents, onStored, onSwitch, onPurge })`, with `...fromGate(gate, { adminScope })` for the two callbacks |
| `site/src/sessionLog.ts`, `sessionLogBoot.ts` | `@open-athena/auth/session-log/client`: `bootSessionLog({ build })`, `slog(kind, fields)` |
| `site/src/sessionsModel.ts`, `SessionLogSwitch.tsx`, `SessionsPage.tsx` | `@open-athena/auth/react/session-log`: `SessionLogSwitch`, `SessionList`, `SessionView`, plus optional `@open-athena/auth/react/session-log.css` |

The auth demo shows the wiring: `oa/auth` `demo/functions/api/session-log/[[path]].ts` (one routes instance per isolate, so the switch cache and purge throttle persist), `demo/src/pages/Sessions.tsx`, and the switch on `demo/src/pages/Admin.tsx`.

## disky-specific behavior moves to hooks

- **Switch audit to `admin_edits`.** `onSwitch: ({ prior, row, req }) => …` writes the same `admin_edits` row disky writes now (`tbl = 'session_log_switch'`, `pk = '1'`, old/new JSON, `who`).
- **Activity feed.** Write it in `onStored` (run through `waitUntil`). Its purge (`purgeActivity`) moves to `onPurge(cutoffS)`.
- **Per-kind muting**, if disky has it, goes in `filterEvents`.
- **App events.** List every `slog` kind disky emits and its fields, and declare them in `appEvents`. Undeclared kinds and fields are now dropped by the client and refused by the server. That is deliberate (no arbitrary objects in logs). Grep for every `slog(` call site, so none silently stops logging.

## Schema

Prepare an app-owned recipe for auth's `migrations/session-log/0002_actor.sql`, after the existing session-log tables: nullable `grant_id` and `subject_name` on `session_log`. Existing rows remain NULL; do not backfill a private memo as recipient identity. Regenerate `site/schema.sql` with the recipe and verify against the package's schema in tests. Each deployment owner must check/apply the recipe once before deploying the new server code; do not re-run its ALTER statements. Live schema verification and applies are deployment-owner actions, not authorized by this source implementation. The package's separate activity migration is not needed while preserving disky's own activity feed.

## Differences to check against disky's tests

- A built-in event kind only stores its whitelisted fields. Extra fields disky's logger attached will be dropped. If one is needed, raise it in auth, rather than smuggling it through an app event.
- Identity is always the server's (`identify`), never the batch's.
- Port any disky test that covers behavior auth doesn't test. Auth's suites are `test/session-log.test.ts`, `test/client-session-log.test.ts`, and `test/react/session-log*.test.tsx`.

## Rollout

Apply the change on disky's main branch, then carry it to each deployment worktree (`wt/gcs`, `wt/cw`, `wt/hf`, `wt/r2`) per `specs/branch-layout.md`. Don't change any production logging switch's state as part of this. CIC `/admin` (switch) and `/admin/sessions` (list and one session) on the local dev server with logging on, then deploy per deployment.
