# Single files: assignable and stageable

Status: implemented on branch `file-assign` (off `cloud`); the migrations below await Ryan's go (none applied), and the gcs lineage's twin is the gcs session's to add.

Today owner assignments and plan items (and so the sweep) only take folder prefixes. A lone matching file — e.g. `ACS_(Part_1)_…_chunk003.mp3` in a folder that also holds non-matching files — can't be acted on: the filtered bulk bar lists it but never sends it, and the unfiltered table's trash button on a file row stages `key/`, which deletes nothing.

## Design: every item carries an explicit kind

An action, an owner row and a plan item each carry `kind`:

- **`prefix`** (folder): `<scheme><bucket>/<path>/` — everything under it. Stored with `kind` NULL (every pre-existing row), so old rows read as prefixes unchanged.
- **`object`** (exact): `<scheme><bucket>/<key>` — that one key, nothing else. Stored with `kind = 'object'`.

The kind is never inferred from the string. Validation pins the two shapes apart as a second line of defence: a prefix is canonicalized to end in `/`; an object key must not end in `/` (folder-placeholder objects are not exact-assignable or stageable) and must not be a bucket root. So the stored strings of the two kinds never collide, but every reader still takes the kind from the column / the wire field.

### Ledger (`/api/actions`, gcs lineage)

- `POST /api/actions` takes `kind: 'prefix' | 'object'` (absent = `prefix`, the old wire). An object action's `pattern` is the exact key URI.
- `actions.kind` and `owner_prefixes.kind` record it (NULL = prefix). `GET /api/actions` returns `kind` on every row (`'prefix'` for NULL).
- **Resolution.** An exact action owns exactly its key. On a path's ancestor-or-self chain, an exact action sits at the leaf: it is more specific than any folder, but recency still decides — the newest action on the chain wins, exactly as between folders (`ownership-ledger-nesting`):
  - folder A (older) + object K under A (newer) → K is the object's owner; the rest of A stays A's.
  - object K (older) + folder A over it (newer) → A repaints K (a newer broad action is a bulk override, as today).
  - object K never covers anything else: not `K.bak`, not `K/…` (a folder that happens to share K's name).
- The folds that implement this: the client `ownerIndex.assignmentOf(uri, kind)`, the server `computeOwners` (bands: an object node is a leaf band, its parent folder's band excludes it) and `ownerLens` / `poolLens` (by index path; an object is a leaf). The audit log's `superseded` / `overridden` status: only a folder row can override a deeper row; an object row is superseded only by a newer row on the identical object.

### Staging and sweep

- `POST /api/plans/stage` takes `objects: string[]` beside `prefixes: string[]`. `plan_items.kind` records the kind (NULL = prefix).
- No-nesting rule (`planStaging`): an object under a staged prefix is `covered`; staging a prefix absorbs the staged objects under it; an object never covers or absorbs anything (`clip.mp3` vs `clip.mp3.bak` are independent).
- Dispatch snapshots: the cw `plan.json` gets `objects` (relative keys) beside `sweep` (relative prefixes); the gcs `plan.json` gets `objects` (canonical `gs://` keys) beside `sweep`. `as_of` is keyed by either. An old executor ignores `objects` (an objects-only plan fails its non-empty-`sweep` check): the safe direction.
- `planDigest` hashes prefixes as before and objects as `=<key>` lines, so a prefix-only plan's digest is unchanged and a kind change changes it.
- Manifest (gcs `sweep_manifest.py`, cw `sweep.py`): an exact item matches `name == key` only — through row-group pruning, the eligible mask, the `as_of` hold (generation / created pinning) and the minimal-band reduction (`minimal_items`: an exact key under a staged prefix is dropped as redundant). `plan-summary.json` lists exact items as `approved_objects` beside `approved` (gcs: only when there are any, so a prefix-only summary is byte-identical; cw: `approved_objects`, since its summary's `objects` is a count). `sweep_reviewed` / `sweep_reuse` compare and validate exact items too.
- Execute (`sweep_exec.py`): deletes are manifest-driven and generation-matched already; for exact items, (1) per-band accounting maps a deleted row to its exact item (so `deletion_bands.prefix` = the object key and `unstageDeleted` takes the item out), (2) a new sibling in an exact-only directory is not drift (drift gates prefix-covered directories only), (3) the listing root of an exact-only directory is that directory, not its top-level segment.
- Undo is per-object from the deletion log (generation-pinned), so it restores exactly what was deleted; its per-band accounting is exact-aware like execute's.

### Folds: one known corner

The server folds (`computeOwners`, `ownerLens`) size and place items by index path. A GCS key `a/b` that is also a folder (`a/b/…` keys exist) shares that path's index row, so an exact assignment on `a/b` and a folder assignment on `a/b/` would read the same aggregate. The client resolver and the audit log keep them apart (by kind); the byte totals don't. The index doesn't separate them either; left as is.

### UI

- Filtered bulk bar: a `file` cover item is sent as an object item (assign: `kind: 'object'`; stage: `objects`). Counts read "N folders, M files". The "lone file can't be acted on" caveat is gone; whole buckets and unchecked matches keep theirs.
- Review panel copy: a line is a folder or a single file; either can be sent.
- Unfiltered table trash button on a file row stages the file as an object item (was `key/`).

### Migrations (additive only; apply needs Ryan's go)

- cw (`site/migrations/cw/0017_plan_items_kind.sql`, on `cloud`): `ALTER TABLE plan_items ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object')`.
- gcs (the gcs session adds it under `site/migrations/gcs/`, next number after `0040`):
  ```sql
  ALTER TABLE plan_items ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
  ALTER TABLE actions ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
  ALTER TABLE owner_prefixes ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
  ```
  No table rebuild (`owner_prefixes.action_id REFERENCES actions(id)`; see `d1-enforces-foreign-keys`). The code reads the column unconditionally, so the migration lands before the deploy.

### Tests

- `site/functions/_lib/fileAssign.test.ts`: the ledger resolution matrix (client resolver, server totals, user lens: file vs folder, nested, newest wins, `.bak`, a same-named `key/` folder), the audit log's statuses with object rows, `/api/actions` and `/api/plans/stage` over a local D1 (`sqliteD1('cw')` + `GCS_LEDGER`), `planStagingItems`, `canonicalObject`, the plan-item round trip through `planDetail` and both dispatch snapshots, the digest (prefix-only unchanged; kind changes it), the bulk bar's submission, and both migrations over seeded rows with `PRAGMA foreign_key_check`.
- UI payloads: `BulkBar.test.ts` (`actionTargets`, `targetsText`, copy), `childrenTableFilter.test.ts` (rows / selection / unfiltered file rows send `{ key, kind: 'object' }`), `objects.test.ts` (`actionItem`).
- Python: `cloud/tests/test_file_assign_sweep.py` (plan parsing, manifest vs a fake listing incl. `clip.mp3.bak` and `clip.mp3/x`, `as_of` holds, execute dry/real against a fake GCS client, undo, cw manifest + execute).
- Mutation checks (each fails at least one test, then reverted): the resolver treating an object as `key/`, the audit log letting an object override, the server fold dropping an object's parent, staging an object as a prefix, the UI sending a file as a prefix, the manifest / cw SQL matching an exact key by prefix.

### Real-data dry run

`dt-cloud sweep manifest` (read-only, local output) over the gcs scan `2026-10-09T1236`, `marin-us-central1`, with a plan of three exact `…/Adam_Carolla_Show/ACS_(Part_1)_Anthony_Cumia_and_The_Rotten_Tomatoes_Game_chunk00{0,1,2}_….mp3` objects: the manifest held exactly those three (2,882,166 B), out of 484,772 objects in that folder, whose other 69 Part_1 chunks and the same chunk names in ~30 other Parts stayed out.

### Back-compat

Every existing row is NULL-kind = prefix; every existing wire body has no `kind` / `objects` = prefixes only. Prefix semantics (coverage, nesting, digest, manifest, drift, roots) are unchanged, and tests pin that.
