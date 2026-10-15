# D1 schema: one current-state file per branch; migrations only while pending

Status: 2026-10-15, proposal (Ryan's model). Nothing has changed yet.

## Today

- There is no "current schema" file. `0001_init.sql` is the schema as of the project's start, and the current schema is whatever results from replaying every file in `migrations_dir` in order. A new database (e.g. hf's) gets created by replaying the whole history, including tables that a later file drops (`marks*`, `mark_log`, `index_schema_new`).
- `wrangler d1 migrations apply` keeps a `d1_migrations` table in each database, one row per applied file **name**. It applies every file in the directory, sorted by name, whose name has no row yet. It doesn't keep a cursor or index: the number in a file name only sets the order, and rows for files that no longer exist are never consulted again.
- Each branch carries its own set of migrations: gcs its own (`site/migrations/gcs/`), and the others the set inherited through `cloud` (`site/migrations/cw/`). Per-branch sets with different numbering are expected under the branch-per-deployment model, just as commits get cherry-picked across deployment branches.
- **Why migrations exist at all:** each deployment's D1 is long-lived and holds data that can't be regenerated (sign-in allowlists and grants, agent and share tokens, sweep plans and approvals, scan-run records, access and session logs). A schema change has to keep those rows, so it's applied to the live database in place, not by recreating it. That's the only reason. Mixed versions in the wild don't apply here.

## Proposed workflow

- **`site/schema.sql` is the current state** of the branch's database schema: what a new database is created from, and what tests build from (`sqliteD1`). One file, no history.
- **A schema change is two commits:**
  1. Add `site/migrations/<slug>.sql`, the recipe that takes a live database from the old state to the new one (`ALTER`, or rebuild-and-copy), and update `schema.sql` to the new state in the same commit. A CI test checks the two agree: applying the old `schema.sql` plus the recipe to an empty SQLite database, with `PRAGMA foreign_keys=ON`, gives the same normalized `sqlite_master` as the new `schema.sql`.
  2. After applying it to each of the branch's live databases (prod D1, plus any separate dev D1 such as r2's), `git rm` the recipe.
- **Pending migrations are the files present.** No numbers are needed. If two are pending at once, which should be rare, a date-prefixed slug orders them. `git log -- site/migrations` is the history.
- **Shared changes:** the recipe is cherry-picked across branches like any `[cloud]` commit. Each branch updates its own `schema.sql` and removes the recipe once its own databases have it. A branch that never needed a change (a gcs-only table) never sees it.
- **Applying:** `wrangler d1 execute <db> --remote --file site/migrations/<slug>.sql`, a prod write (Ryan's go). `wrangler d1 migrations apply` and its `d1_migrations` table drop out; existing rows are harmless leftovers. A new database: `wrangler d1 execute <db> --remote --file site/schema.sql`.
- **Local and test databases** are disposable: rebuilt from `schema.sql` (local dev DBs are seeded from prod exports anyway).

## Moving to it (per branch)

1. Generate `schema.sql` from the branch's current migrations (replay into an empty SQLite database, dump normalized DDL), and check it against each live database's actual `sqlite_master`. Any difference is drift to resolve first.
2. Switch the tests and the new-database instructions (`wrangler*.toml` comments, deployment specs) to `schema.sql`.
3. `git rm` the whole migrations dir, plus `twins.json` and `scripts/port-migration`, which only tracked pairs across the old numbered sets.

Step 1 is read-only (schema queries on each live D1). Steps 2–3 are commits on each branch, made by that branch's session.

## Caveats

- **Don't remove a recipe early.** It goes only after every database of that branch has it: prod, a separate dev database if there is one, and the r2 demo database for `cloud`.
- **Deploy order still matters, briefly:** apply the recipe, then deploy the code that reads the new column. Additive changes (a new table, a new nullable column) keep the old code working in between.
- **D1 enforces foreign keys** where SQLite by default doesn't: test recipes with `foreign_keys=ON` on a seeded database, and never rebuild a table something else references (memory `d1-enforces-foreign-keys`).
