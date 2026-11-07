# D1 schema: one current-state file per branch; migrations only while pending

Status: 2026-10-15, built on `cloud` (Ryan's model). `site/schema.sql` replaced `site/migrations/cw/`, `twins.json` and `scripts/port-migration`; each deployment branch takes it on its next merge of `cloud` (see [On merge, per branch](#on-merge-per-branch)).

## Before

- There was no "current schema" file. `0001_init.sql` was the schema as of the project's start, and the current schema was whatever resulted from replaying every file in `migrations_dir` in order. A new database (e.g. hf's) got created by replaying the whole history, including tables that a later file drops (`marks*`, `mark_log`, `index_schema_new`).
- `wrangler d1 migrations apply` keeps a `d1_migrations` table in each database, one row per applied file **name**. It applies every file in the directory, sorted by name, whose name has no row yet. It doesn't keep a cursor or index: the number in a file name only sets the order, and rows for files that no longer exist are never consulted again.
- Each branch carried its own set of migrations: gcs its own (`site/migrations/gcs/`), and the others the set inherited through `cloud` (`site/migrations/cw/`), with `twins.json` + `scripts/port-migration` pairing the same migration across the two numberings.
- **Why migrations exist at all:** each deployment's D1 is long-lived and holds data that can't be regenerated (sign-in allowlists and grants, agent and share tokens, sweep plans and approvals, scan-run records, access and session logs). A schema change has to keep those rows, so it's applied to the live database in place, not by recreating it. That's the only reason. Mixed versions in the wild don't apply here.

## The workflow

- **`site/schema.sql` is the current state** of the branch's database schema: what a new database is created from, and what tests build from (site: `sqliteD1()` in `functions/_lib/testD1.ts`; dt-cloud: the `d1_db` fixture in `cloud/tests/conftest.py`, and `d1_schema.table_ddl` for a subset of tables). One file, no history.
- **Its form is canonical:** the schema loaded into an empty SQLite database (foreign keys ON) and dumped from `sqlite_master`: each object's stored `CREATE` text, `;`-terminated, tables first (parents before the tables whose foreign keys reference them, else by name), then views, indexes and triggers (by table, then name); `sqlite_*`, autoindexes, `_cf_*` and `d1_migrations` left out; a fixed header comment. The stored text is exactly what the live databases hold (an `ALTER TABLE … ADD COLUMN` shows up as `, col TYPE` appended before the closing paren, as SQLite rewrites it), so comparing a dump with a live database's is an exact text comparison. A dump reloads to itself.
- **A schema change is two commits:**
  1. Add `site/migrations/<slug>.sql`, the recipe that takes a live database from the old state to the new one (`ALTER`, or rebuild-and-copy), and regenerate `schema.sql` in the same commit: `dt-cloud d1-schema dump site/schema.sql site/migrations/<slug>.sql -o site/schema.sql`. `dt-cloud d1-schema check -p` verifies it (before or after committing), and so does CI (below).
  2. After applying it to each of the branch's live databases (prod D1, plus any separate dev D1), `git rm` the recipe.
- **Pending migrations are the files present.** No numbers are needed. If two are pending at once, which should be rare, a date-prefixed slug orders them (`check -p` applies them in name order). `git log -- site/migrations` is the history.
- **Shared changes:** the recipe is cherry-picked (or merged) across branches like any `[cloud]` commit. Each branch updates its own `schema.sql` and removes the recipe once its own databases have it. A branch that never needed a change (a gcs-only table) never sees it.
- **Applying:** `wrangler d1 execute <db> --remote --file site/migrations/<slug>.sql`, a prod write (Ryan's go). `wrangler d1 migrations apply` and its `d1_migrations` table drop out (no `migrations_dir` in any `wrangler*.toml`); existing rows are harmless leftovers. A new database: `wrangler d1 execute <db> --remote --file site/schema.sql`.
- **Checking a live database:** `dt-cloud d1-schema check -d <db> [-c <wrangler config>]` (from the repo; runs `wrangler d1 execute <db> --remote --json` from `site/`, a read-only `sqlite_master` query) prints a unified diff and exits 1 on any drift.
- **Local and test databases** are disposable: rebuilt from `schema.sql` (`site/dev --local-db` creates a fresh local D1 with `wrangler d1 execute <db> --local --file schema.sql`; `site/refresh-db` seeds one from a prod export, which carries its own schema).

## `dt-cloud d1-schema` (`cloud/src/dt_cloud/d1_schema.py`)

- `dump [-o OUT] SOURCES…`: the canonical DDL of `.sql` files replayed in order (a schema, or a schema then recipes), or of one SQLite database file.
- `check [-s SCHEMA] [SOURCES… | -d DB [-c CONFIG] | -p]`: the schema file (default `site/schema.sql`) is canonical (loads with foreign keys ON, every foreign key names an existing table, dumps to itself) and, given one, equals the other side: SOURCES, a live D1, or the pending recipes over their pre-image. Exit 1 with a unified diff on any difference.
- The pre-image (`pre_image`) of the pending recipes is `schema.sql` at the first parent of the commit that added the earliest of them, walking first parents with `--diff-merges=first-parent`: a recipe merged in from `cloud` dates from the merge on the deployment branch, so its pre-image is that branch's own schema, not `cloud`'s. An uncommitted recipe's pre-image is `HEAD`'s.

## The CI checks, and why these

`cloud/tests/test_d1_schema.py` (dt-cloud job):

1. `schema.sql` loads cleanly into an empty SQLite database with foreign keys ON, every foreign key's table exists, and it is its own canonical dump (so it is exactly what `dump` produces, and a hand edit that drifts from the stored form fails).
2. While any recipe is pending: the pre-image plus the pending recipes, replayed with foreign keys ON, dump to `schema.sql` — the spec's "the two agree" check, exactly. With none pending the test is skipped (a legitimate state, most of the time).

Why the pre-image comes from git history: the alternatives each fail somewhere. Asserting something about a recipe applied to the *new* `schema.sql` can't work (an additive recipe fails against the state it produces, and a rebuild proves nothing). Keeping a separate `schema.base.sql` beside `schema.sql` is a second copy of the schema to keep in step. Diffing against `HEAD~` breaks for a recipe that is not in the last commit, and for merges. Following first parents to the commit that added the recipe is exact for a direct commit, a cherry-pick and a merge alike, needs no extra files, and is the same code the author runs (`check -p`). It needs the history: the dt-cloud CI job checks out with `fetch-depth: 0`, and `pre_image` refuses a shallow clone rather than guessing. (Its pass cache keys on the tree only; a pre-image is a function of the history too, but a tree that already passed with the same recipes and schema can only differ by an unrelated rewrite of history.)

Recipe-specific assertions (data survives, foreign keys intact on a seeded database) belong in a test next to the recipe while it is pending, and go when it does. The tests that used to replay numbered migrations over seeded rows are gone with the migrations; what they pinned about the final state stays as tests over `schema.sql` (`site/functions/_lib/indexTables.test.ts`: the index tables' keys, store-scoped pointers, `started`). Tests of code paths for a database *missing* a table or column now drop it from a schema-built database (`DROP TABLE footer_cache`, `ALTER TABLE allowed_emails DROP COLUMN read_only`, …); the pre-store-migration index tests went (no database lacks it).

## Moving to it (per branch)

`cloud` (done here; its only database is the r2.rbw.sh demo `disk-tree-demo-db`, bound by both `wrangler.r2.toml` and `wrangler.r2-dev.toml`):

1. `schema.sql` generated from `site/migrations/cw/` (`dt-cloud d1-schema dump site/migrations/cw/*.sql -o site/schema.sql`). A live check (2026-10-15, read-only) found every database on the lineage — cw-s3, m3, hf, r2, the demo — equal to the replay (gcs/cw had a dead `agents` table, since dropped).
2. Tests (`testD1.ts` lost lineages, `migrations()`, `migrationNamed()` and `schemaFork`; `RUN_DIR` is read from the schema: `manifest` on cloud's, `plan` on gcs's), dt-cloud's `scan_runs.SqliteSink` (its tables from `schema.sql` via `table_ddl`) and test fixtures, `site/dev --local-db`, the `wrangler*.toml` comments (no `migrations_dir`), `infra/README.md` and `infra/cf/cfn_dashboard.py` switched to `schema.sql`.
3. `site/migrations/cw/`, `site/migrations/twins.json`, `scripts/port-migration` (and its test) removed.

### On merge, per branch

- **cw-s3, m3, hf, r2** (on the cw lineage, merging `cloud`): the merge brings `schema.sql`, removes `migrations/cw/` and `twins.json`, and drops `migrations_dir` from cloud's wrangler files. Each branch then: drops `migrations_dir` from its own `site/wrangler.toml` (and switches its comment to `wrangler d1 execute <db> --remote --file schema.sql`); updates any deployment checklist that says `wrangler d1 migrations apply` (e.g. a `specs/hf-deployment.md`-style setup doc); and runs `dt-cloud d1-schema check -d <db>` once against its prod (and any dev) D1 — expected equal, per the live check above. A branch-only migration that was still pending in `migrations/cw/` (none known) becomes a recipe in `site/migrations/` with `schema.sql` regenerated over it.
- **gcs** (its own lineage `site/migrations/gcs/`, which `cloud` never had): the merge removes `migrations/cw/` and `twins.json` and brings cloud's `schema.sql` (the cw schema). The gcs session then makes `schema.sql` its own: `dt-cloud d1-schema dump site/migrations/gcs/*.sql -o site/schema.sql`, `dt-cloud d1-schema check -d <gcs db>` against prod (resolve any drift first), then `git rm -r site/migrations/gcs`. From then on, a merge of `cloud` that touches `schema.sql` is an intended conflict: keep gcs's file and replay cloud's recipe into it (`dump site/schema.sql site/migrations/<slug>.sql`). Tests to adjust on gcs: those that build gcs's ledger tables ad hoc (`GCS_LEDGER` in `bulkSubmit`/`fileAssign`, `LEDGER_KIND`, the `LEDGER` in `ledger.test.ts`/`actionLog.test.ts`, …) now meet tables gcs's schema already has; `RUN_DIR` already follows the schema (`plan`), and `me.test.ts`'s no-`user_emails` case drops the table itself.
- **Removing a recipe vs. merging:** a `cloud` commit that `git rm`s a recipe (once the demo has it) merges into a deployment branch as a removal too. A branch whose databases don't have it yet keeps the file in the merge (restore it) until they do.

## Caveats

- **Don't remove a recipe early.** It goes only after every database of that branch has it: prod, a separate dev database if there is one, and the r2 demo database for `cloud`.
- **Deploy order still matters, briefly:** apply the recipe, then deploy the code that reads the new column. Additive changes (a new table, a new nullable column) keep the old code working in between.
- **D1 enforces foreign keys** where SQLite by default doesn't: test recipes with `foreign_keys=ON` on a seeded database (every replay here runs with them ON), and never rebuild a table something else references (memory `d1-enforces-foreign-keys`).
- **`schema.sql` is generated.** Edit it through a recipe and `dump`, not by hand: the canonical check fails on any text that isn't the dump's (whitespace, comments between statements).
