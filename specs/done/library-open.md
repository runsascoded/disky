# Spec: open a scans "library" at runtime

Status: **done** — runtime root swap, API, and header UI shipped. Moved from in-progress; see the commit trailer.

## Idea

A disk-tree **library** is a self-contained `DISK_TREE_ROOT` directory: `disk-tree.db` + `scans/` + `diffs/` + `logs/`. Today the root is fixed at process start from `DISK_TREE_ROOT` (or the `~/.config/disk-tree` default). The app (and server) should be able to **open a different library while running** — point at any directory, one open at a time. "Open library X" ≡ "set the active root to X and rebind everything that hangs off it."

This is also the clean answer to "make the scan location changeable from within the app": rather than a settings panel editing env vars, it's *open a library* — a familiar mental model (like a project/workspace switcher).

## Why it's a refactor

`config.py` computes `ROOT_DIR`, `SQLITE_PATH`, `DEFAULT_SCANS_DIR`, `SCANS_DIR` **at import** and many modules bind those values at import time. Long-running consumers that must follow a root change:

- `sqla/db.py` — holds a single `SQLAlchemy`/`Flask` app + engine bound to one sqlite URL.
- `storage/__init__.py` — `get_backend()` singleton (+ `HybridBackend._cache`).
- `server.py` — `_cache` dict, `DB_PATH`, per-request reads.
- `diff_index.py` — reads `_config.SQLITE_PATH` / `_config.ROOT_DIR` **live** (attribute access) → already follows a swap.

One-shot CLIs that `from disk_tree.config import SQLITE_PATH` freeze the value at import — fine, they never swap.

## Design

### 1. `config.set_root(path)` (+ `current_root()`, `on_root_change(cb)`)
Recompute and **reassign** the module globals (`ROOT_DIR`, `DEFAULT_SCANS_DIR`, `SQLITE_PATH`, `SCANS_DIR`), `makedirs` the new root (guarded — refuse a path under an unmounted `/Volumes/x`, the makedirs-on-boot-disk trap from `specs/done/macos-app.md`), then fire registered `on_root_change` hooks. Idempotent for the same path.

### 2. `sqla/db.py: reinit()`
Dispose the current engine, drop the app/db singletons, rebuild against `config.SQLITE_PATH` (create tables). Registered as an `on_root_change` hook.

### 3. `library.py` — orchestration + recents
- `open_library(path)`: `config.set_root` → `db.reinit` → `storage.reset_backend` → `server.clear_cache` → record in recents. Returns the new library's summary (path, scan count).
- Recents live in a **pointer file outside any library** — `~/.config/disk-tree/libraries.json` (`{recents: [{path, opened_at, label}], current}`) — since libraries come and go. `list_libraries()`, `record_open(path)`, `current_library()`.

### 4. API — done
- `GET /api/library` → `{current, recents:[...]}`
- `POST /api/library/open {path}` → opens, returns summary; 400 on a nonexistent/unmounted path.

### 5. UI — done
- Header shows the open library's name; a menu lists recents + "Open…" → pywebview `create_file_dialog(FOLDER_DIALOG)` in the app, a path input in the browser.

## Tests
`tests/test_library_open.py`: build two temp roots each with a distinct scan, `open_library(A)` then `open_library(B)`, assert the scan list / backend follows each swap; assert opening an unmounted `/Volumes/x/...` path raises rather than makedirs-ing on boot.

## Non-goals (v1)
- Concurrent multiple-library views (one open at a time by design).
- Moving the *default* boot-disk pointer file into a library (it must stay always-mounted).

## Implementation notes (as shipped)

- `GET /api/library` → `{current: {path,label,exists,scans}, recents: [{path,label,opened_at}]}`; `POST /api/library/open {path}` returns the same payload (400 on a missing/unmounted path). `POST /api/library/pick` opens a native folder dialog **only** inside the pywebview app (`webview.windows[0].create_file_dialog`); the browser gets 501 and falls back to a path input.
- **Schema on open**: a freshly-opened root has no DB, so the server's `on_root_change` hook runs `init_db()` (idempotent `CREATE TABLE IF NOT EXISTS`) after re-pointing `DB_PATH` — otherwise the raw-sqlite `/api/scans` read 500s on `no such table: scan`. Regression-tested (`test_open_empty_library_serves_empty_scans`).
- UI: `ui/src/components/LibrarySwitcher.tsx` in the header — current library + scan count, recents (click to switch), "Open folder…". On switch it `invalidateQueries()` (the whole dataset changed) and navigates to `/`.
- `library.summary()` deliberately omits on-disk `exists`/`scans` (a provisioning hook may create the DB on open); the API payload reads those from the file instead.
