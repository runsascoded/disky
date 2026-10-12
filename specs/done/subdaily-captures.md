# Preserve scheduled captures as separate snapshots

Requested and deployed 2026-10-11 (America/New_York). Production now uses Batch revision 14 and Pages deployment `14e9e8a3`. The image is pinned at `sha256:627ff78200141d418bd398b3d7d17bfb3d39c67e4f41998f0462b59965d4d1a8`; source archive hash `0f7b592c8adb14ab`.

The ingest previously chose `YYYY-MM-DD` from a capture stamp. Both scheduled captures therefore uploaded to the same snapshot directory and updated the same D1 scan. It now chooses `YYYY-MM-DDTHHMM` in UTC, the reader's existing scan-ID grammar, while retaining the deterministic index generation. Re-ingesting a capture uses its original slot; malformed capture stamps fail before any cloud operation instead of silently using the current day.

`capture-meta` always records `captured_at`, including NAS captures without APFS measurements. Its `--check` option refuses to publish over a slot whose existing metadata belongs to a different capture. A same-minute second capture is retained in the raw capture store but refused for publication; second-resolution scan IDs and an atomic concurrent-publication reservation remain future work. This change targets the distinct morning/evening slots; it does not claim general concurrent immutable publication.

The laptop deletion executor accepts sub-daily IDs and keeps the complete ID on the deletion run. Existing date-only snapshots remain readable. Nothing deletes or renames old data. Today's raw morning and evening captures were re-ingested after deployment into independent slots; the existing daily snapshot was left intact. Earlier raw captures can likewise reconstruct history without rewriting existing daily snapshots.

Validation: the complete ingest shell runs against local AWS/dt-cloud stand-ins with the real `capture-meta` guard. Tests verify two same-day snapshots, deterministic re-ingest, independent NAS/laptop stores, collision refusal before upload/D1 sync, and rejection of malformed stamps. The site's executor test verifies the exact sub-daily ID recorded on a run.

The privacy check also exposed two viewer URL defects, fixed alongside this work: browser paths are decoded once and directory names encoded when navigating; a guest key is redeemed before React Router snapshots the URL, so later navigation cannot restore it. Object-opening URLs also discard consumed guest/preview keys. The replacement Chrome check passed on dev: a guest sees the folder's aggregate but no file names, parent navigation keeps the key out of the URL, and reloading the spaced path still resolves the correct folder. Both temporary test grants were revoked and each was verified to return 401. Production and dev API checks verified empty hidden largest-file lists, no hidden search matches, hidden-file 404s and raw-index/admin API 403s.

Checks passed: 733 site tests (one skip), site/Functions typechecks and build, 28 capture/ingest/real snapshot-writer tests, shell syntax and diff whitespace. The new R2 publication check also ran against a real, unoccupied scan slot, modifying only a local fixture. The Pulumi preview has two updates (CodeBuild source and Batch job definition) and two replacements (source archive and the local image-build command); no IAM, queue or bucket replacement is proposed.

Live rollout verification:

- Morning capture `2026-10-11T10-11-12Z` → scan `2026-10-11T1011`, job `848b68d2-b9c3-401d-8256-d00df7e7fc63`, succeeded. Capture instant `2026-10-11T10:11:12.241205+00:00`; 31,719,010,304 physical bytes free.
- Evening capture `2026-10-11T22-01-35Z` → scan `2026-10-11T2201`, job `02378796-6b96-436c-bd22-79e8ab82233a`, succeeded. Capture instant `2026-10-11T22:01:35.843657+00:00`; 26,205,544,448 physical bytes free.
- Both scans have independent `path` and `bysize` index-schema rows in D1; `2026-10-11` remains intact. Chrome's production picker offers both times, selecting morning changes the URL to `?d=261011-1011`, and its capacity bar changes to 32 G free / 93.6% used. The retained legacy daily copy also appears at its recorded evening time.
- A fresh `/mnt/user` SSH metadata capture contained 77,360 files and triggered job `92e9db47-ce52-4ce3-80e3-02f9361df510` automatically. Revision 14 succeeded and published independent NAS scan `2026-10-12T0232`, with `captured_at` `2026-10-12T02:32:20.615078+00:00` and no APFS annotation.
- Production resolves the encoded spaced directory and navigates to its parent cleanly. The guest-specific browser check passed on dev before deployment; both temporary credentials remain revoked.

NAS recurring scheduling is separate app work, tracked by `wt/app/specs/m3-nas-deployment.md`; this rollout does not enable it. Deployment logs, metadata verification and the production picker screenshot are under this worktree's ignored `tmp/`.
