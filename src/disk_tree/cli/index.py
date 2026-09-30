import os
from contextlib import nullcontext
from os import getcwd

from click import argument, option

from disk_tree import time
from disk_tree.cli.base import cli
from disk_tree.sqla.db import init
from humanize import naturalsize
from utz import err, iec

_CLOUD = ('s3://', 'gcs://', 'r2://', 'ssh://')
LOW_SPACE_VAR = 'DISK_TREE_LOW_SPACE_BYTES'
REMOTE_TARGET_VAR = 'DISK_TREE_REMOTE_SCAN_TARGET'
DEFAULT_LOW_SPACE_BYTES = 5 * 2**30


def _check_local_space(auto_remote: bool) -> None:
    """Warn — or, with `--auto-remote`, redirect — when the local write target is
    low on space: the crisis a remote target exists for (spec
    `remote-scan-targets.md`). A URL target is never checked; the reduce still
    needs *transient* local scratch either way, only the persistent blob moves.
    """
    from shutil import disk_usage
    from disk_tree import blobfs, config as _config

    wd = _config.scan_write_dir()
    if blobfs.is_url(wd):
        return
    probe = wd
    while not os.path.exists(probe):  # the dir may not exist yet; its volume does
        probe = os.path.dirname(probe)
    free = disk_usage(probe).free
    low = int(os.environ.get(LOW_SPACE_VAR, DEFAULT_LOW_SPACE_BYTES))
    if free >= low:
        return
    remote = os.environ.get(REMOTE_TARGET_VAR)
    if auto_remote and remote:
        target = _config.set_write_target(remote)
        err(f"low space: {iec(free)} free on {wd} (< {iec(low)}); --auto-remote: writing blobs to {target}")
        return
    hint = f"--to {remote}" if remote else f"--to r2://<bucket>/<prefix> (or set {REMOTE_TARGET_VAR})"
    tail = " — pass -R/--auto-remote to redirect automatically" if remote else ""
    err(f"warning: only {iec(free)} free on {wd} (< {iec(low)}); consider {hint}{tail}")


@cli.command
@option('-C', '--no-cache-read', is_flag=True)
@option('-D', '--no-diff', is_flag=True, help="Skip building the diff index against the path's previous scan")
@option('-e', '--require-external', is_flag=True, help='Skip (exit 0) if the resolved write target is the boot-disk default — i.e. no opted-in external volume is mounted. For scheduled scans that must land on external media.')
@option('-g', '--gc', is_flag=True)
@option('-m', '--mean-mtime', is_flag=True, help='Emit `mtime_mean` (size-weighted mean mtime over descendants) per path')
@option('-M', '--measure-memory', is_flag=True)
@option('-q', '--no-progress', is_flag=True, help='Suppress the tqdm scan progress bar (for scheduled/redirected runs — keeps logs small)')
@option('-R', '--auto-remote', is_flag=True, help=f'If the local write target is low on space (< ${LOW_SPACE_VAR}, default 5 GiB) and ${REMOTE_TARGET_VAR} is set, write the blob there instead (default: warn and suggest `--to`)')
@option('-s', '--sudo', is_flag=True, help='Run `find` as sudo')
@option('-t', '--to', default=None, help='Write this scan\'s blob to a local dir or an fsspec URL (`r2://bucket/prefix`, `s3://…`, `gs://…`) instead of the configured write dir; it joins the search path for this run')
@option('-x', '--extents', is_flag=True, help='Also map physical extents → per-dir reclaimable bytes (APFS clones/hardlinks; writes a .reclaim sidecar). macOS, local scans only; exact when the scan root contains the sharing sources (home/full scan)')
@argument('url', required=False)
def index(
    no_cache_read: bool,
    no_diff: bool,
    require_external: bool,
    gc: bool,
    mean_mtime: bool,
    measure_memory: bool,
    no_progress: bool,
    auto_remote: bool,
    sudo: bool,
    to: str | None,
    extents: bool,
    url: str | None,
):
    """Index a directory, persisting data to a SQLite DB."""
    db = init()
    from disk_tree.sqla.model import Scan
    db.create_all()
    url = url or getcwd()
    url = url.rstrip('/') or '/'
    if to:
        from disk_tree import config as _config
        target = _config.set_write_target(to)
        err(f"--to: writing blobs to {target}")
    elif not url.startswith(_CLOUD):
        _check_local_space(auto_remote)
    # Scheduled scans that must land on external media: bail before doing any
    # work when the write target fell back to the boot disk (no opted-in volume
    # mounted). Exit 0 so a launchd/cron wrapper logs a skip, not a failure.
    if require_external and not url.startswith(('s3://', 'gcs://', 'r2://', 'ssh://')):
        from disk_tree import config as _config
        wd = _config.scan_write_dir()
        if wd == _config.DEFAULT_SCANS_DIR:
            err(f"--require-external: write target is the boot disk ({wd}); no external scans volume mounted — skipping")
            return
        err(f"--require-external: writing to {wd}")
    # `load_or_create` returns any existing scan unconditionally (no freshness
    # check) and can't tell a sudo scan from a plain one — so without this,
    # `index --sudo` silently re-serves a cached *non*-sudo scan and never
    # elevates. Asking for sudo means you want a fresh, privileged walk.
    if sudo and not no_cache_read:
        err("--sudo forces a fresh scan (-C)")
        no_cache_read = True
    if measure_memory:
        try:
            from utz.mem import Tracker
        except ImportError as e:  # utz.mem needs memray (the `mem` dependency group)
            raise SystemExit(f"--measure-memory needs the `mem` dependency group (`uv sync --group mem`): {e}")
        mem = Tracker()
        ctx = mem
    else:
        mem = None
        ctx = nullcontext()

    with ctx, time("scan"):
        if no_cache_read:
            scan, df = Scan.create(url, gc=gc, sudo=sudo, mean_mtime=mean_mtime, progress=not no_progress)
        else:
            scan, df = Scan.load_or_create(url, gc=gc, sudo=sudo, mean_mtime=mean_mtime, progress=not no_progress)

    elapsed = time['scan']
    if not no_diff and not gc:
        # Overnight prep: the "what changed since last time" view is a slice,
        # not a walk, by the time anyone asks. (`--gc` deleted the previous
        # scan, so there's nothing to diff against.)
        from disk_tree.cli.diff_index import build_previous
        build_previous(scan.id)
    # Find root row: try 'path == "."', fallback to 'parent == ""'
    root_rows = df[df['path'] == '.']
    if root_rows.empty:
        root_rows = df[df['parent'] == '']
    res = root_rows.iloc[0]
    n_desc = res.n_desc
    size = res['size']
    speed = n_desc / elapsed

    if mem:
        peak_mem = mem.peak_mem
        err(f"Peak memory use: {peak_mem:,} ({naturalsize(peak_mem, binary=True, format='%.3g')})")

    print("Timings:")
    for k, v in time.fmt().items():
        print(f"  {k}: {v}s")
    summary = f"{n_desc:,} descendents ({elapsed:.3g}s, {round(speed):,d}/s), {naturalsize(size, binary=True, format='%.3g')}"
    if scan.error_count:
        summary += f", {scan.error_count} permission errors"
    print(summary)
    # Blobs may live on any read dir (external volume incl.), so resolve via the
    # search path — not a naive join with the *write* dir, which stats a path
    # that need not exist (e.g. blob on the boot disk, write target on X6).
    from disk_tree import blobfs
    from disk_tree.diff import resolve_blob
    blob_path = resolve_blob(scan.blob)
    if blobfs.exists(blob_path):
        print(f"Scan cached path: {blob_path} ({iec(blobfs.size(blob_path))})")
    else:
        print(f"Scan blob: {scan.blob}")
    if to and blobfs.is_url(blob_path):
        # A remote blob's metadata travels with it (`disk-tree scans register`).
        from disk_tree.scan_manifest import write_scan_manifest
        print(f"Scan manifest: {write_scan_manifest(scan, blob_path)}")
        # Precompute the footer as a `.groups.json` sidecar so the serverless
        # reader (`ui/cfn`) plans range reads without a cold thrift-footer parse.
        from disk_tree.find.groups import write_groups_sidecar
        gs = write_groups_sidecar(blob_path)
        if gs:
            print(f"Scan groups: {gs.path} ({gs.n_groups} groups, {iec(gs.n_bytes)})")
    if scan.error_count:
        import json
        error_paths = json.loads(scan.error_paths) if scan.error_paths else []
        if error_paths:
            print(f"\nPermission errors (showing first {len(error_paths)}):")
            for p in error_paths[:10]:
                print(f"  {p}")
            if len(error_paths) > 10:
                print(f"  ... and {len(error_paths) - 10} more")
        print(f"\nTip: Run with --sudo for full access: disk-tree index --sudo {url}")

    if extents:
        if blobfs.is_url(blob_path):
            err(f"--extents writes a local sidecar beside the blob; skipping for remote blob {blob_path}")
        elif os.path.exists(blob_path):
            _build_reclaim_sidecar(url, blob_path)
        else:
            err(f"--extents needs a resolvable blob to write the sidecar beside; skipping ({scan.blob})")


def _build_reclaim_sidecar(url: str, blob_path: str):
    """Map physical extents of the just-scanned tree → per-dir reclaimable bytes.

    Walks the live filesystem (not the blob), so it only applies to a local path
    that still exists. The blob's paths are relative to `url`, and
    `reclaimable_by_dir` returns the same scheme, so the sidecar joins directly.
    """
    import sys
    from os.path import isdir
    from disk_tree.extents import SUPPORTED, reclaimable_by_dir, write_reclaim_sidecar

    if not SUPPORTED:
        err(f"--extents is macOS-only (got {sys.platform}); skipping")
        return
    if url.startswith(('s3://', 'gcs://', 'r2://', 'ssh://')) or not isdir(url):
        err(f"--extents needs a local directory that still exists; skipping ({url})")
        return
    with time("extents"):
        recl, n_err = reclaimable_by_dir(url)
    out = write_reclaim_sidecar(blob_path, recl)
    root = recl.get('.', 0)
    err(f"extents: reclaim sidecar → {out} ({time['extents']:.1f}s"
        + (f", {n_err} unreadable" if n_err else "") + ")")
    from humanize import naturalsize as _ns
    top = sorted(((k, v) for k, v in recl.items() if k != '.'), key=lambda kv: -kv[1])[:8]
    print(f"Reclaimable (rm -rf frees): {_ns(root, binary=True, format='%.3g')} total")
    for k, v in top:
        print(f"  {_ns(v, binary=True, format='%.3g'):>9}  {k}")
