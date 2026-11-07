# Findings from the `~/.disk` cleanup session (2026-09-23)

Written by the `~/.disk` session while running a local-laptop browse + staged-delete flow off `cloud` @ `47eee40` (own `disk-tree-server` on :5791 with `DISK_TREE_DELETE_APPROVAL=staged`, own Vite on :7791 proxying to it).

## 1. Missing migration: `deletion_run.batch_job` (bug; `/api/staged` 500s on existing DBs)

`sqla/deletion.py` added `batch_job: Mapped[str | None]` to `DeletionRun`, but nothing adds the column to an existing `~/.config/disk-tree/disk-tree.db` (`create_all` doesn't `ALTER`, and `disk-tree migrate` only covers `scan`). Every `/api/staged` then fails:

```
sqlite3.OperationalError: no such column: deletion_run.batch_job
```

Diffing every `Base.metadata` table against the live DB found this as the only gap. I applied `ALTER TABLE deletion_run ADD COLUMN batch_job VARCHAR` by hand (backup at `~/.disk/tmp/disk-tree.db.bak-20260923`). **Fix**: a generic "add missing nullable columns" pass (models vs `PRAGMA table_info`) run by `migrate` and ideally on server/CLI startup, so the next added column doesn't repeat this.

## 2. `disk-tree repos` shells out to `disk-tree` by name

It fails with `FileNotFoundError: 'disk-tree'` whenever the CLI isn't on `PATH` (e.g. outside the repo's direnv venv). Invoke via `sys.executable -m …` / `sys.argv[0]` instead.

## 3. `index` peak RSS up ~65% since Sep 9

Full `/Users/ryan` scan (6.6M files, `-C -q -D --to r2://…`): peak RSS **7.1 GB** (footprint 10.6 GB), vs **4.3 GB** on Sep 9 (`5c32460`, same tree size, same flags minus `-D`). On this 98%-full laptop that goes straight to swap (the `VM` volume holds 31 GB). Worth a `-M`/`--measure-memory` bisect between `5c32460` and `47eee40`.

## 4. The bundled `src/disk_tree/static` UI is stale (Aug 30)

`STATIC_DIR` prefers the packaged `static/` over `ui/dist/` (Sep 20), so a bare `disk-tree-server` serves an Aug 30 UI that predates staged deletes. Either refresh `static/` on build, or prefer whichever is newer in a dev checkout.

## Resolution (2026-09-25, `cloud`)

1. **Fixed** (`15b1b86`): `sqla/migrate.py` — `add_missing_columns(engine)` diffs every model against `PRAGMA table_info` and `ALTER TABLE … ADD COLUMN`s the gaps (nullable as-is; NOT NULL with a scalar default gets `DEFAULT`; anything else raises). Runs after every `create_all` (`sqla.db.init`, `staged_backend._engine`) and first in `disk-tree migrate`. Your hand-applied `batch_job` column is exactly what the pass would have added, so the live DB is already in the shape it expects.
2. **Fixed** (`15b1b86`): `python -m disk_tree` is the CLI (`disk_tree/__main__.py`); `repos` runs it via `sys.executable`.
3. **Not a code regression — measured.** `~/c` (3.6M files) at `5c32460` vs HEAD: 2.71 GB vs 4.22 GB max RSS, but the `5c32460` worktree venv resolved to Python 3.14 and the main venv is 3.13.7. Crossing them on `~/c/oa` (1.07M files): old code 1.54 GB (3.14) / 1.86 GB (3.13); HEAD 1.50 GB (3.14) / 1.93 GB (3.13). Code delta ≤ 4% either way; the interpreter is the lever (3.13 ≈ +25% over 3.14 on the same code). `git diff 5c32460 HEAD -- src/disk_tree/{find,storage,cli/index.py}` touches only the `--to` branch (`.groups.json` sidecar) and boto profiles. Your Sep 9 → Sep 23 pair (both on the main venv's 3.13.7) isn't explained by that; candidates left open for you: the `--to r2://` upload path (s3fs multipart buffering; both my runs were local), a `-M` (memray `Tracker`) vs `time -l` measurement difference, and memory pressure on a 98%-full disk. Cheap win regardless: `vsw 3.14` for the scan venv. Also: `-M` needs the `mem` dependency group (`uv sync --group mem`) — the traceback is now a one-line hint (`cli/index.py`).
4. **Fixed** (`15b1b86`): `STATIC_DIR` serves whichever candidate's `index.html` is newest, so a dev checkout's fresh `ui/dist` beats a stale packaged `static/`.
