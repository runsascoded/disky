# Scan Source Backends

Formalize the abstraction for *where* disk-tree scans. Today: local filesystem (via `gfind`) and S3 (via `aws s3 ls`), with dispatch hard-coded on `path.startswith('s3://')`. Target: pluggable backends with a common protocol, starting with SSH and S3-compatible endpoints (R2, MinIO, etc.).

## Goals

- Unified URL scheme across the CLI, API, DB, and UI
- Backend protocol with well-defined semantics for `list` / `delete` / `exists` / capability flags
- Ship SSH first; refactor S3 onto the same protocol; slot R2/GCS/Azure in later
- Preserve the "bulk recursive ls" fast path — no per-entry RTTs across backends

## Non-goals

- Replacing [fsspec] — we want bulk/paginated streaming, not per-file stat calls
- Supporting arbitrary [rclone] remotes in v1 (nice-to-have follow-up)
- Transparent writes across backends (the tool is read-mostly; deletes are the only mutation)

## URL Schemes

| Scheme | Example | Backend | Notes |
|--------|---------|---------|-------|
| (none) / `file://` | `/Users/ryan` | `LocalBackend` | Bare paths treated as local; `file://` canonicalized |
| `s3://` | `s3://ctbk/csv` | `S3Backend` | Current behavior |
| `ssh://` | `ssh://m1/Users/ryan` | `SshBackend` | Canonical; `[user@]host[:port]/abs-path` |
| `<host>:<path>` | `m1:/Users/ryan` | `SshBackend` | scp-shorthand; normalized to `ssh://m1/Users/ryan` |
| `r2://` | `r2://<account>/bucket/prefix` | `S3Backend` | S3-compatible w/ R2 endpoint (see [Endpoint Config]) |
| `gcs://` | `gcs://bucket/prefix` | *(future)* | |
| `az://` | `az://container/prefix` | *(future)* | |

### Normalization rules

- Trailing slashes stripped (except root)
- `~` expanded for local paths only (never expanded in SSH paths — remote `~` differs)
- Relative local paths resolved to absolute via `os.path.abspath`
- `host:path` → `ssh://host/path` before DB insert (canonical form in storage)
- Display: UI and breadcrumbs show the canonical form

## Backend Protocol

```python
class Backend(Protocol):
    scheme: str  # "file" | "ssh" | "s3" | "r2" | ...

    def list(self, url: str, *, excludes: list[str] | None = None,
             errors: ErrorCollector | None = None,
             progress: ProgressCallback | None = None) -> Iterator[Entry]:
        """Recursive bulk listing. Must stream (bounded memory)."""

    def delete(self, url: str) -> None:
        """Delete a single path (file or directory). Recursive for dirs."""

    def exists(self, url: str) -> bool:
        """Cheap existence check (for navigation fallback)."""

    @property
    def is_local(self) -> bool:
        """Whether reveal-in-Finder / local shell integrations apply."""

    @property
    def supports_sudo(self) -> bool:
        """Whether the --sudo flag is meaningful."""
```

Entries:

```python
@dataclass
class Entry:
    path: str       # relative to scan root
    size: int       # bytes (post-block-conversion for local)
    mtime: int      # unix seconds
    kind: Literal['file', 'dir', 'link', 'other']
    uri: str        # fully-qualified URL
```

## Backend-specific details

### `LocalBackend`

- Unchanged from today: `gfind <root> -printf '%y %b %T@ %p\0'`
- `excludes` applies (default: `CloudStorage` paths on macOS)
- `sudo` prefixes the command
- `delete` uses `os.remove` / `shutil.rmtree`
- `is_local = True`

### `SshBackend`

- `list`: `ssh <host> "gfind <path> -printf '%y %b %T@ %p\0'"` — *same parse logic*, just piped through SSH
- Rely on `~/.ssh/config` for user, port, identity; no credential handling
- `sudo` requires a TTY and NOPASSWD entry — document as user-provided; don't prompt
- `delete`: `ssh <host> rm -rf <escaped-path>` (shell-escape with `shlex.quote`)
- `exists`: `ssh <host> 'test -e <path> && echo y'` — cached per-request
- `is_local = False` (no Finder reveal)
- **Error handling**: same stderr regex (`PERMISSION_DENIED_RE`) should match remote gfind output; add a pre-flight check (`ssh <host> which gfind` — fall back to `find` on Linux where `find` ≈ `gfind`)
- **Remote binary**: prefer `gfind`, accept `find` if GNU (`find --version | grep -q GNU`); error clearly on BSD `find` (macOS default minus coreutils)

### `S3Backend` (refactor)

- Same `aws s3 ls --recursive` as today
- Extract `s3_files_iter` into a method; hoist parse logic from `find/index.py`
- New: accept an optional `endpoint_url` + `profile` via config lookup
- `delete`: `aws s3 rm --recursive` (batched)

### `R2Backend` / generic S3-compatible

- Not a new backend — a *configured* `S3Backend` with `endpoint_url` set
- URL `r2://<account>/bucket/prefix` → resolves config → calls S3 client with `--endpoint-url=https://<account>.r2.cloudflarestorage.com`
- Registration happens at startup from endpoint config

### Endpoint Config

Config file: `~/.config/disk-tree/endpoints.yaml`

```yaml
schemes:
  r2:
    endpoint: https://{account}.r2.cloudflarestorage.com
    profile: r2         # AWS CLI profile
    account_in_host: true   # parse <account> out of r2://<account>/...
  minio-local:
    endpoint: http://localhost:9000
    profile: minio
```

User registers a scheme once; URLs resolve against the scheme table. Unknown schemes fall through to the default dispatch.

## Dispatch

Central function `backend_for(url)` in `disk_tree/backends/__init__.py`:

1. Parse URL, identify scheme
2. If scheme has registered backend class: construct + return (with any endpoint config)
3. Else fall through to `LocalBackend`

Caching: one instance per scheme+config (SSH backend needs per-host state: connection reuse via `ssh -o ControlMaster=auto -o ControlPath=...` — let OpenSSH handle multiplexing).

## Capability flags on the UI

The frontend needs to know, for a scan:

- Can we rescan? (yes for all)
- Can we delete? (yes for all — but SSH asks for stronger confirmation)
- Can we reveal-in-Finder? (local only)
- Can we open-in-terminal? (local + SSH; SSH opens a terminal with `ssh <host>` preloaded)

Add a `/api/scans/<id>/capabilities` endpoint or inline `capabilities: {...}` on the scan payload.

## DB / migration concerns

- `Scan.path` is already a free-form string; new URLs coexist with existing ones
- `uri` column in parquet: update construction per-backend (S3 already does this; SSH paths should carry `ssh://host/abs-path`)
- Add `scheme` column on `Scan` (denormalized) so the UI can filter/group without URL-parsing

## Open questions

- SSH `sudo`: prompt interactively, require passwordless, or skip entirely? Proposal: **skip entirely in v1** — document "configure passwordless sudo on the remote host if you need it"
- Handling hosts where `gfind` isn't installed: fail loudly with a remediation hint, or attempt `find` + detect BSD? Proposal: **detect and require GNU find**, print the apt/brew install command
- Remote scans with identical path on two hosts (e.g. `/home/ryan` on both `m1` and `m2`) — already fine since we store the full URL

## Phasing

1. Extract `Backend` protocol and `LocalBackend`/`S3Backend` from current code (no behavior change)
2. Add `SshBackend` + URL parsing + scp-shorthand normalization
3. Wire `/api/scan/start`, `/api/delete` through `backend_for(url)`
4. UI capability flags; disable reveal-in-Finder for non-local
5. Endpoint config + `r2://` scheme
6. (future) GCS, Azure, generic rclone

[fsspec]: https://filesystem-spec.readthedocs.io
[rclone]: https://rclone.org
[Endpoint Config]: #endpoint-config
