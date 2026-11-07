# One `disky` CLI: inventory, duplication, target architecture

**Status:** 2026-10-17, design only. Implementation after the ~10/19 freeze ([`disky-rename.md`] step 2 is the first slice of it). Nothing here changes a command line any process, job, cron or other session looks up before then.

Ryan's asks: one `disky` command; no redundant parallel implementations in Rust and Python (his prior: Rust preferred, Python calling Rust binaries). This spec inventories what exists, says where the duplication actually is and how close each pair is, then recommends a layout and a sequence.

Branches read: `cloud` (root, `ec8fc624`), `local` (`wt/local`), `tauri-native-app` (`wt/app`), `m3` (`wt/m3`), and `gcs` / `cw` / `r2` / `hf` via `git grep refs/heads/<b>`. Paths below are relative to the branch named; `app:` = `wt/app/apps/tauri/`.

## 1. Inventory

### 1.1 Three executables today

| executable | what | where it is on PATH | commands |
|---|---|---|---|
| `disk-tree` | Python engine, `disk_tree.cli.base:cli` ([`pyproject.toml`] `[project.scripts]`) | any venv that installs the engine (deployment images, laptop worktree venvs) | `cloud`: 6 (`bulk-list`, `import`, `listing-format`, `notify`, `recompress`, `tiers {cut,plan}`). `local` / `m3` / app branch: 21 top-level (adds `capture`, `capture-meta`, `du`, `index`, `overcount`, `reclaim`, `repos`, `volumes`, `stage`, `staged`, `unstage`, `dispatch`, `undo`, `scans {list,move,register,dirs,chunks,info}`, `trash {ls,restore,empty}`) |
| `dt-cloud` | Python cloud overlay, `dt_cloud.cli:main` (`cloud/pyproject.toml:49`), ~38.8K lines in `cloud/src/dt_cloud/*.py`, imports `disk_tree` in 53 places | the job images (`gcs` `Dockerfile:58`, `cw` `Dockerfile:63`, `r2` `job/Dockerfile:36`, `hf` `job/Dockerfile:45`, m3 `infra/aws/Dockerfile:32`), GHA `daily-ingest.yml`, laptop venvs | 53 top-level, 197 including subcommands |
| `disky` | the Tauri app binary, `disky.app/Contents/MacOS/disky` (`app:src-tauri/tauri.conf.json:43` `mainBinaryName`) | not on PATH; launchd runs it via the bundled plists | headless subcommands below, else the GUI |

Plus four standalone Rust dev binaries (`dt-walker`, `dt-capture`, `dt-index`, `dt-serve`), one per crate's `src/bin/`. Only `dt-walker` ships, as an app resource (`app:src-tauri/tauri.conf.json:38-39`); the other three exist for the parity tests and manual runs.

### 1.2 The app binary's CLI

`main.rs` calls `headless(args)` before any Tauri/AppKit init (`app:src-tauri/src/main.rs:5-9`, `lib.rs:57`); dispatch is `agent::dispatch` (`app:src-tauri/src/agent.rs:43-123`). An unknown `argv[1]` falls through to the GUI.

| subcommand | does | pure Rust? | shells out to |
|---|---|---|---|
| `version` | prints the crate version (`agent.rs:45`) | yes | — |
| `settings show` | prints `disky.json` `settings` (`agent.rs:46-49`) | yes | — |
| `serve [--addr 127.0.0.1:PORT]` | the local read API + bundled SPA over the app's local scans (`dt_index::http::serve`, `agent.rs:50-65`) | yes | — |
| `alert TITLE MSG` / `alert ask` | sends a test failure alert / asks for notification permission (`agent.rs:68-87`) | mostly | `osascript` banner; the `disky.json` `notify.command` fallback (`notify.rs:203-206`) = `disk-tree notify -f` |
| `agent [--] CMD ARGS…` | runs CMD as a child (TCC: the app stays the responsible process), forwards TERM/INT/HUP, sets `DISK_TREE_WALKER` to the bundled `dt-walker` (`agent.rs:125-173`, `lib.rs:45-52`) | wrapper | whatever CMD is |
| `pushover set\|clear\|status` | Pushover keys in the login Keychain (`notify.rs:157-192`) | yes | `/usr/bin/security` |
| `tunnel set\|token\|on\|off\|status` | file-preview tunnel settings + Keychain token (`tunnel.rs:298+`); the GUI supervises `cloudflared` | yes | `cloudflared` (from the GUI process) |
| `probe` | reads TCC-protected dirs in-process, prints ok/denied/absent; exit 3 if any denied (`agent.rs:175-223`) | yes | — |
| `job scan --scheduled` | the scan LaunchAgent's 15-min wake: runs `job scan` only if a schedule slot passed, or "Scan now" set `force`; retries an interrupted run once (`jobs.rs:151-187`) | yes | — |
| `job NAME` | runs `disky.json` `jobs.NAME` (`jobs.rs:199-281`), logs to `~/Library/Logs/disk-tree/<log>.{out,err}.log`, alerts on failure. `scan` with no `command`: **in-process** capture and/or local index (`jobs.rs:237`, `capture()` at `:292-413`); any job with a `command`: a child via `run_child` | `scan`: yes; others: wrapper | `drain`: the configured command (Ryan's: m3's `aws/laptop-drain`, which `execve`s `disk-tree dispatch -s …`, `wt/m3/aws/laptop-drain:38-40`); `scan`'s optional `then` command |
| `scan now` | "Scan now": sets `force`, `launchctl kickstart`s the agent, else spawns `disky job scan` (`status.rs:40-60`) | yes | `launchctl` |
| `agents register\|unregister\|status [NAME]`, `login-item on\|off\|status` | writes/loads the per-user LaunchAgent plists from the bundled templates; `SMAppService.mainApp` for the login item (`services.rs:285-312`) | yes | `launchctl` |

Not CLI (GUI/menu only): "Connect this Mac" (device enrollment + presigned uploads, `connect.rs`, `dt-capture/src/device.rs`), `disky://` links (`links.rs`, `applink.rs`), the window. The plists invoke `disky job scan --scheduled` and `disky job drain` (`app:src-tauri/launchd/com.runsascoded.disky.{scan,drain}.plist`).

So: of the app's subcommands, only `job drain` (via config) and the alert fallback reach the Python engine today; the scan path is pure Rust. `agent` is a generic TCC wrapper.

### 1.3 The Rust crates (`app:crates/`)

| crate | implements | lines | Python counterpart |
|---|---|---|---|
| `dt-walker` | `getattrlistbulk(2)` walker; emits gfind's `%y %b %T@ %p\0` byte stream (bin) or callbacks (lib); `--one-fs`, CloudStorage excludes, `--private` (APFS private size in the same call). macOS only (no `cfg` gating; won't build on Linux) | 770 | `gfind`/`find` subprocess in `disk_tree/backends/local.py:20-22,88-115`, parsed by `backends/gfind.py:23` (`run_gfind`). `DISK_TREE_WALKER=<dt-walker>` swaps the source command (`local.py:95-99`); the app sets it for every child (`agent.rs:143-145`) |
| `dt-capture` | walk → layer-1 shards (`bucket,name,size_bytes,created,storage_class_id`, snappy, 64K groups) + `_SUCCESS.json` (incl. APFS `container`, `apfs.rs`); targets dir / `r2://` / `s3://` / `file://` (`target.rs`, `object_store`); credential/endpoint resolution as `blobfs` (`creds.rs` reads `buckets.yml`, `DISK_TREE_R2_ENDPOINT_URL`, AWS profiles); presigned-URL uploads for connected Macs (`device.rs`) | 1011 | `disk-tree capture` (`local:src/disk_tree/cli/capture.py:60`, gfind/walker via `LocalBackend`); `apfs.py` (`disk-tree volumes`); `blobfs.py` bucket config. No Python counterpart for `device.rs` |
| `dt-index` | `Reducer` (capture or live walk → per-dir rollup) → `path-index.parquet` + `-bysize` + `meta.json` + `age.json` (`lib.rs`); `view.rs` = the site's `/api/subtree`, `/api/series`, diff, filter (port of `site/functions/_lib/view.ts`); `query.rs` = port of `querySyntax.ts`/`queryAst.ts`/`pathQuery.ts`; `http.rs` loopback server + SPA; `files.rs` + `access.rs` = `/files/` behind Cloudflare Access JWT | 3445 | `dt-cloud path-index -g` (`cloud/src/dt_cloud/viz.py` `write_path_index`, `disk_tree/find/tiers.py`). The view/query/server side has **no Python counterpart**: its twin is the site's TypeScript Functions |
| `src-tauri` | app shell: jobs, settings, schedule, notifications (native + Pushover), Keychain, tunnel, LaunchAgents, volumes (`volumes.rs`: `(other volumes)` rows from `diskutil apfs list`) | 3818 | `disk-tree notify` (`src/disk_tree/notify/push.py`: Pushover/Discord/Slack); `apfs.py` for volumes |

### 1.4 What `~/.config/disk-tree/disky.json` runs (this laptop, 2026-10-17)

Read by `jobs.rs:91-109` (path `$DISK_TREE_ROOT/disky.json`, else `~/.config/disk-tree/disky.json`; note `~/.config/disky/` does not exist on this machine).

| key | value (shape) | effect |
|---|---|---|
| `jobs.scan` | `{to: "r2://disk-tree/captures", host: "m3", log: "index", env: {AWS_PROFILE, DISK_TREE_R2_ENDPOINT_URL}}`, no `command`, no `then` | in-process `dt-capture` of `/` (one-fs) straight to R2. The R2 `_SUCCESS.json` event triggers m3's AWS Batch ingest (`aws/submit` retired as `then`). Logs show twice-daily runs, e.g. 2026-10-17T10:01Z, 8.65M files, 44 shards, 6.4 min |
| `jobs.drain` | `command: [<wt/m3>/.venv/bin/python, <wt/m3>/aws/laptop-drain]`, `env.PATH` | Python: `disk-tree dispatch -s --d1 <m3 D1> --trash --ttl 7d -a "<disky> scan now" -H m3` (`wt/m3/aws/laptop-drain:38-40`) |
| `notify.command` | `direnv exec <wt/m3> <wt/m3>/.venv/bin/disk-tree notify -f` | Python fallback for alerts when native notifications are refused |
| `settings` | `site: dev`, `scope: machine`, `schedule: [06:00, 18:00]`, `files: {on, team, aud, client_id, root: "/"}` | `site != local` ⇒ no local index (`jobs.rs:71-73`); the last local scans in `~/Library/Application Support/disky/scans/` are from 2026-10-02/03 |

### 1.5 Where each engine is used

| path | runs | Rust? | Python? |
|---|---|---|---|
| m3 laptop scan (prod, disk.rbw.sh) | `disky job scan` | walk + capture (`dt-walker`, `dt-capture`) | — |
| m3 ingest (AWS Batch, `wt/m3/infra/aws/ingest.sh`, image `python3.14-bookworm-slim`) | `dt-cloud hosts resolve`, `dt-cloud path-index -g` (`:160`), `disk-tree capture-meta` (`:164`), `dt-cloud index-sync` (`:181`), `dt-cloud static-names add` (`:205`), entrypoint `dt-cloud scan-run supervise` | — | all of it |
| m3 laptop drain | `disky job drain` → `disk-tree dispatch` | wrapper only | `dispatch`, trash |
| app local-only mode (`site: local`) | `disky job scan` + GUI / `disky serve` | walk, reduce (`dt-index`), serve, view, filter, diff | — |
| gcs / cw / r2 / hf / r2 demo | `dt-cloud scan head` (Nextflow) / `scan-run supervise` / GHA `daily-ingest.yml` | — | all of it. `dt-cloud` shells out to `disk-tree bulk-list` / `import` / `notify` by PATH name (`cloud/src/dt_cloud/scan_steps.py:372,440,475`, `batch.py:89`); the r2 demo GHA runs `uv run disk-tree bulk-list` (`.github/workflows/daily-ingest.yml:81`); cw's `job/cw-path-store-phase0.sh:98` runs `disk-tree tiers plan` |
| laptop-only Python | `du`, `overcount`, `reclaim`, `repos`, `volumes`, `trash`, `stage`/`staged`/`unstage`/`dispatch`/`undo`, `scans`, `index`, `capture` | — | yes |

Engine commands the cloud deployments invoke: `bulk-list`, `import`, `notify` (all deployments via `scan_steps.py`), `tiers` / `recompress` / `listing-format` (cw tooling), `capture-meta` (m3 ingest). Everything else in the `local` engine CLI is laptop-only.

## 2. Duplication

| # | Rust | Python (or TS) | parity evidence | used in prod today |
|---|---|---|---|---|
| D1 | `dt-walker` | `gfind` subprocess + `run_gfind` | byte-identical stream by construction; `tests/test_gfind_errors.py`, `test_backends.py` | Rust (in-process, scan job). Python only when someone runs `disk-tree index`/`capture` by hand; with `DISK_TREE_WALKER` set they use the Rust walker anyway |
| D2 | `dt-capture` | `disk-tree capture` | `tests/test_dt_capture.py` (app branch): identical rows, schema, shard layout, manifest bar `time`; real R2 run matched 2026-10-01 ([`rust-engine.md`] phases 1–2). **Skipped in CI** (needs a macOS release build; `ci.yml` is ubuntu, `macos-app.yml` builds only `dt-walker` then `cargo test`) | Rust. The Python capture has had no scheduled caller since `aws/laptop-scan` was replaced (2026-10-01) |
| D3 | `dt-index` `Reducer` + writers | `dt-cloud path-index -g` (DuckDB) | `tests/test_dt_index.py`: frames, schema, kv, row groups, `meta`/`age` equal; prod-index match on an 8.36M-row scan (2026-10-02). **Subset:** `usr`, `last_read` NULL, class pivots 0, no `.groups.*` sidecars. Also skipped in CI | Python for every hosted store (m3 ingest included); Rust only for local-only mode, which this laptop isn't using now |
| D4 | `dt-index` `view.rs` / `query.rs` / `http.rs` | `site/functions/_lib/{view,querySyntax,queryAst,pathQuery}.ts` (TS, not Python) | manual: 7 `/api/subtree` views identical vs prod (2026-10-02); `view.rs` unit tests. No automated cross-language test | Rust only in local mode; TS everywhere else |
| D5 | `dt-capture/src/apfs.rs`, `src-tauri/src/volumes.rs` | `disk_tree/apfs.py` (`disk-tree volumes`, capture manifest `container`) | "same JSON shape" per `apfs.rs` header; covered by the D2 manifest compare. Also an intra-Rust overlap (`volumes.rs` re-parses `diskutil apfs list`) | Rust in the scan; Python in `disk-tree volumes` |
| D6 | `dt-capture/src/creds.rs` | `blobfs.bucket_profile` / `r2_endpoint`, `buckets.yml` | unit tests both sides; no cross test | both (Rust on the laptop, Python in jobs) |
| D7 | `src-tauri/src/notify.rs` (Pushover from Keychain) | `disk_tree/notify/push.py` (Pushover / Discord / Slack from env) | none needed; small (~50 lines each). The app falls back to the Python one | both, different hosts |
| D8 | `dt-walker --private` (`ATTR_CMNEXT_PRIVATESIZE`) | `disk_tree/extents.py:187-234` (ctypes `getattrlist`), `overcount` / `reclaim` | none | Python (`overcount`, `reclaim`, `repos --reclaim`); the walker flag only feeds `--stats` |
| D9 | — (Rust `local` store: `~/Library/Application Support/disky/scans/<id>/path-index*.parquet`) | `disk-tree index` → layer-2 blob + SQLite `Scan` row, read by `du` (no `-p`), `repos`, `scans` | different formats; not a port | neither is refreshed by a job: `du.py:84-85` already says "nothing refreshes the blob store on the laptop any more (`du -p` reads the ingest's path index)" |

Reading: D1, D2 and D5 are true duplicates where the Rust side won and the Python side is now dead weight on macOS (but is the only implementation on Linux, where `dt-walker` doesn't build). D3 is a deliberate subset port, required by the self-contained-app goal ("no Python, no Homebrew `gfind`, no checkout", [`rust-engine.md`] "Why"). D4 is a Rust↔TS port for the same reason. D6/D7 are small config/notification twins that cost little. D9 is two generations of the laptop store, the older one orphaned.

## 3. Target architecture

### 3.1 Principles

- **One implementation per artifact, chosen by where it must run.** Anything that must run inside the app on a Mac with no Python (walk, capture, laptop reduce, local serve, TCC, Keychain, launchd) is Rust and only Rust. Anything that runs in the cloud jobs over buckets (listing, DuckDB aggregation, interval store, static names, sweeps, digests, D1 sync) stays Python: it's ~40K lines of `dt-cloud` plus the engine, mostly DuckDB/pyarrow-shaped, and won't be rewritten.
- **Formats are the contract.** Layer-1 captures, layer-2 frames and the path store's sorts stay byte/row-compatible across languages; each surviving cross-language pair keeps a parity test **that CI actually runs**.
- **Python calls Rust as a binary when it needs Rust**, files in / files out, the way `dt-cloud` already calls `disk-tree bulk-list` (`scan_steps.py:475`).

### 3.2 Rust ↔ Python boundary: subprocess vs PyO3

| | subprocess to a Rust binary | PyO3 / maturin extension module |
|---|---|---|
| fit with today's boundaries | the stages already exchange parquet dirs and exit codes (`ctx.sh([...])`) | finer-grained calls (Arrow C data interface to pyarrow), which nothing needs yet |
| cloud images (`python:slim` / uv `bookworm-slim`, Batch + Nextflow, GHA) | one multi-stage Docker step (`rust:slim` builder → `COPY` the binary); GHA: `cargo build` or a release artifact | needs platform wheels (manylinux x86_64/aarch64, macOS arm64). There is no release workflow and deps are pinned as GitHub tarballs, so images would build from sdist with a Rust toolchain in every image build unless wheels are published somewhere |
| laptop dev | `cargo build` once; the binary on PATH or next to the app | `uv sync` builds the member with cargo (maturin build backend), slower syncs, the shared `.venv` gotchas multiply |
| failure isolation | a crash is an exit code; memory is the child's (good for 2 GB reduces) | a panic or OOM takes the Python process (and a Nextflow task's supervisor) with it |
| cost | process start (ms) per call; irrelevant at stage granularity | zero per-call cost |

Recommendation: **subprocess to binaries**, PyO3 deferred until a measured hot loop needs in-process calls. For packaging, collapse the four crate bins into one headless binary, `disky-core` (`walk`, `capture`, `index`, `serve`), built for macOS (all subcommands) and Linux (`index`, `serve`; `walk`/`capture` gated behind `cfg(target_os = "macos")`). The app links the same crates, so the app's `disky` also answers those subcommands.

What this does **not** buy: deleting `dt-cloud path-index`. It reduces bucket unions with owner (`usr`) attribution, class pivots, access logs and footer sidecars that `dt-index` doesn't do. Replacing it would mean growing `dt-index` to the full schema and adopting it in every deployment's scan, which only pays if Batch time/RSS on the reduce step is a real cost. That's a later decision on measurements, not part of the CLI work. The near-term Rust-in-cloud case is m3's ingest, which reduces laptop captures, exactly `dt-index`'s domain (see P4).

### 3.3 One `disky` command

There will be two `disky` executables on different hosts, and each forwards what it doesn't own to the other:

- **macOS app binary** (`disky.app/Contents/MacOS/disky`, symlinked to `~/.local/bin/disky` by the app, e.g. a menu item / `disky install-cli`): owns app subcommands (§1.2) and the `disky-core` subcommands; forwards anything else to the engine.
- **Python console script `disky`** (engine dist): owns engine subcommands, plus `cloud` when `dt-cloud` is installed; on macOS, forwards app subcommands to the app binary when it finds one (`/Applications`, `~/Applications`, or `disky.json` `app`). On Linux there is no app, so it is the only `disky`.
- **Loop guard:** the forwarder sets `DISKY_FORWARDED=1`; a forwarded call that is unknown on the other side too exits 2 with the merged usage instead of bouncing.
- **Locating the engine from the app:** a `disky.json` top-level `engine` argv prefix (e.g. `["/…/wt/m3/.venv/bin/disky-engine"]`), else `disky-engine` on PATH; unset and absent ⇒ "engine not installed" (the M1 case per [`disky-rename.md`]). The `drain` job's command can then become `["disky", "dispatch", …]`-style through the same lookup.
- **Help:** each side lists its own commands as JSON (`disky commands --json`); `disky --help` merges both, marking which side runs each.
- **Name reservations:** app `scan` (`scan now`) vs engine `scans` and `index`; app `serve`; app `job` vs `dt-cloud job` (namespaced under `cloud`, so no clash). New top-level names get checked against both sides; a CI test asserts the two sets are disjoint.
- **TCC bonus** (from [`disky-rename.md`]): engine commands forwarded from the app binary run as its children, so they inherit the app's Full Disk Access (`disky trash restore` can read `~/.Trash`).

### 3.4 `dt-cloud` as `disky cloud`

- The engine's root group loads plugins from an entry-point group (`disky.commands`); `cloud/pyproject.toml` registers `cloud = "dt_cloud.cli:main"`. Installed ⇒ `disky cloud <cmd>`; not installed ⇒ absent (the engine stays light: no gcsfs, thrds, pyrmts).
- Short alias: `disky c` (a click alias on the same group object).
- Lazy import: the entry point resolves only when `cloud`/`c` is invoked, so `disky du` doesn't pay `dt_cloud`'s import cost.
- `dt-cloud` stays a console script pointing at the same `main`, and `disk-tree` stays pointing at the engine root, until every call site below has moved. Both are one-line `[project.scripts]` entries, so keeping them costs nothing.
- Engine console script names: `disky` (front door) and `disky-engine` (the same group; what the app execs, so the app never execs itself through a PATH lookup).

Call sites to migrate before removing `disk-tree` / `dt-cloud`:

| call site | branch(es) |
|---|---|
| `dt-cloud` shells out `disk-tree notify` / `import` / `bulk-list` by name: `cloud/src/dt_cloud/scan_steps.py:372,440,475`, `batch.py:89`. Switch to `sys.executable -m disk_tree …` (or the `disky` name) so it never depends on PATH | `cloud` → all |
| image entrypoints `dt-cloud scan-run supervise …` / `dt-cloud` | `gcs` `Dockerfile:58`, `cw` `Dockerfile:63`, `r2` `job/Dockerfile:36`, `hf` `job/Dockerfile:45`, `m3` `infra/aws/Dockerfile:32` |
| Batch job specs `"entrypoint": "dt-cloud"` | `gcs` `job/batch-submit.sh:197`, `cw` `job/cw-batch-submit.sh:107,164`, `hf` `job/hf-batch-submit.sh:73`, `r2` infra (`infra/tests/test_r2_gcp.py:140` pins it) |
| Nextflow `dt-cloud secrets exec … dt-cloud scan step` | `job/nf/scan.nf:66` (all) |
| GHA `uv run disk-tree bulk-list`, `dt-cloud …` | `.github/workflows/daily-ingest.yml` (all) |
| m3 ingest `disk-tree capture-meta`, `dt-cloud …` | `m3` `infra/aws/ingest.sh:59,105,160,164,181,205` |
| cw shell helpers | `cw` `job/cw-env.sh:70`, `job/cw-path-store-phase0.sh:98` |
| laptop: `disky.json` `notify.command`, `wt/m3/aws/laptop-drain:38-40` | this laptop, `m3` |
| fixtures / tests that run `disk-tree tiers plan` | `site/functions/_lib/fixtures/gen.py:158` (all) |

## 4. Phases

**P0 (now → ~10/19): nothing that churns.** This spec. No console-script, entrypoint, plist or `disky.json` change.

**P1 (after 10/19): names, additive only** (`cloud`, merged down).
- Engine dist adds console scripts `disky` and `disky-engine` beside `disk-tree` (same group).
- The `disky.commands` entry-point group; `dt-cloud` registers `cloud` (+ alias `c`). `dt-cloud` script kept.
- `scan_steps.py` / `batch.py` stop shelling out to the `disk-tree` name (`sys.executable -m disk_tree`). This is the one step that must ship to every deployment's image before any alias removal.
- Test: `disky --help` lists engine + `cloud` when installed; `disky cloud --help` == `dt-cloud --help`.

**P2: the app front door** (app session).
- Forwarding both ways (§3.3), loop guard, `disky.json` `engine`, `disky commands --json`, merged help, the `~/.local/bin/disky` symlink.
- Move this laptop's `disky.json` `notify.command` and the drain to the `disky`/`disky-engine` names.
- Test: a forwarded unknown command exits 2 once (no ping-pong); app and engine command sets disjoint.

**P3: retire the dead laptop duplicates** (`local`).
- Python `capture` (D2): on macOS it should exec `disky-core capture`. Keep the Python path only if a Linux/ssh capture user exists (open question 1); otherwise delete it with its tests (the parity test then compares against a frozen fixture).
- Python `index` + the blob-store side of `du` / `repos` / `scans` (D9): move `du`/`repos` to the path store (`du -p` already reads it) over the app's local scans dir or the ingest's R2 index; drop `index`, `scans`, the SQLite `Scan` reads, and `DISK_TREE_WALKER` plumbing once nothing reads blobs. Confirm with Ryan first (open question 2).
- D8: `overcount`/`reclaim` keep Python `extents.py` (it does `F_LOG2PHYS_EXT`, which the walker doesn't); drop `dt-walker --private` unless something consumes it.
- D5: `volumes.rs` reuses `dt_capture::apfs` instead of re-parsing `diskutil`.

**P4: Rust in a cloud image, where it fits** (m3 first).
- `disky-core` Linux build in CI; the `test_dt_index.py` parity test runs there (un-skipped), and a macOS CI job runs `test_dt_capture.py`.
- m3's ingest image gains a Rust build stage; `ingest.sh` reduces laptop captures with `disky-core index` instead of `dt-cloud path-index -g` (the store, D1 sync and static names stay Python). Gate: parity on the latest real capture, plus the Batch time/RSS delta recorded in m3's spec.
- Bucket-union reduces (`gcs`/`cw`/`r2`/`hf`/demo) stay on Python `path-index` unless a measured case appears (§3.2).

**P5: remove aliases** once §3.4's call-site table is empty on every deployment branch and in `disky.json`: drop `disk-tree`, `dt-cloud` console scripts; [`disky-rename.md`] step 4's package rename can coincide.

## 4b. Notes from Ryan's review (2026-10-17)

- **Rename the crates and binaries** with the CLI work (after 10/19): `dt-walker` / `dt-capture` / `dt-index` / `dt-serve` → crates `disky-walk`, `disky-capture`, `disky-index`, folded into one `disky-core` binary (§3.2); `DISK_TREE_WALKER` → `DISKY_WALKER` through the env helper. Tracked as a row in `disky-rename.md`'s sequence.
- **Python → Rust today:** one case. `disky agent` sets `DISK_TREE_WALKER` to the bundled `dt-walker`, so a Python `disk-tree index` / `capture` run under the app walks with Rust. Nothing else calls a Rust binary; `disky-core` doesn't exist yet (four dev binaries do, one of which ships).
- **Packaging a Rust binary for Python users:** the uv/ruff model. maturin's `bindings = "bin"` builds platform wheels that carry the binary, so `pip install` gets it with no Rust toolchain. That needs a release workflow building a wheel matrix (macOS arm64/x86_64, Linux x86_64/arm64), which we don't have; until then, images build `disky-core` in a Docker stage and the app bundles it.
- **Rust rewrites are justified by measurement, not taste.** Before porting any Python stage, an exp-sweep-sized experiment on real inputs: e.g. `dt-walker` vs `gfind` on the same tree (wall, CPU, syscalls), `dt-index` vs `dt-cloud path-index -g` on the latest real capture (wall, peak RSS). Most of `dt-cloud`'s time is already in DuckDB / pyarrow (C++), so the expected gain there is small and the bar is high; the walker and capture, where Python shells out per entry or pays per-object overhead, are where Rust plausibly wins.
- **Where native code makes disky feel native** (candidates; macOS first, Linux analogues in parentheses):
  - APFS clones: what a delete actually frees (`ATTR_CMNEXT_PRIVATESIZE`, extents), shown unobtrusively next to apparent size (Linux: reflinks on btrfs/XFS via `FIEMAP`);
  - hardlinks: count shared inodes once;
  - iCloud / File Provider placeholders (dataless files): local bytes vs cloud-only bytes, so "evicted" files don't count against the disk;
  - transparent compression (APFS/HFS `decmpfs`): logical vs physical;
  - last-opened dates from Spotlight (`kMDItemLastUsedDate`): a "not opened in N months" lens that mtime can't give;
  - purgeable space and local Time Machine / OS-update snapshots (`volumes`): the bytes no walk shows;
  - sparse files (already exact: block counts).

## 5. Open questions

1. Does anyone run `disk-tree capture` or `disk-tree index` on Linux (EC2 nodes, `SshBackend`)? If not, the Python walk/capture path is deletable outright; if so, it stays as the Linux implementation and D1/D2 remain intentional.
2. Is the SQLite `Scan` / layer-2 blob store on the laptop still wanted at all (`du` without `-p`, `repos`, `scans move/register`, `stage`/`dispatch` reading it)? `du.py:84-85` says nothing refreshes it. The `dispatch` drainer reads `DISK_TREE_SCAN_DIRS=r2://disk-tree/scans:~/.config/disk-tree/scans`, so some layer-2 reads may be live; this needs a trace before P3.
3. Is local-only mode (`site: local`) in use anywhere (the M1? another Mac)? If not, D3/D4 are maintained for a future product, which is fine but should be stated in [`rust-engine.md`].
4. Connected Macs (device enrollment) upload captures but have no engine: do they ever need `trash` / `dispatch` (deletes)? If yes, the drain would have to move to Rust for a Python-less install, which is the one real "port more to Rust" pressure the CLI design surfaces.
5. Front-door precedence on a dev laptop with both `disky`s on PATH: forwarding makes either work, but `which disky` differs by PATH order. Acceptable, or should the venv not install `disky` on macOS dev machines?
6. Distribution of `disky-core` for Linux images: build in each image (multi-stage) vs a GH release artifact pinned by SHA. The former needs no release process; the latter makes image builds faster. No release workflow exists today.
7. [`disky-rename.md`]'s Status says `~/.config/disky` "already exists (the app's `disky.json`)". On this laptop it doesn't; `disky.json` is in `~/.config/disk-tree/` (`jobs.rs:91-96`). Worth correcting there before step 3b.

[`disky-rename.md`]: disky-rename.md
[`rust-engine.md`]: https://github.com/runsascoded/disky/blob/tauri-native-app/specs/rust-engine.md
[`pyproject.toml`]: ../pyproject.toml
