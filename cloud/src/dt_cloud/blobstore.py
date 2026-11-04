"""The store seam: a generation store's keys under one root, a local dir or an fsspec URL (`gs://`, `r2://`, `s3://`,
`memory://`; `r2://` takes its endpoint from `DISKY_R2_ENDPOINT_URL` or `buckets.yml`, `disk_tree.blobfs.fs_for`).
Keys are relative to the root and `/`-separated, as the readers' `INDEX_R2` keys are relative to their bucket.

The static name index's single-machine path (`static_local`) and the interval store (`interval_store`,
`interval_append`, `interval_local`) read, write and list through it; `RunStore` adapts it to `append_runner`'s
runs-and-manifests protocol.

A deployment's profile names its store by URL; a bare bucket name there means `gs://<name>` (`store_url`), as the
profiles written before the seam did. A CLI's `-o DIR|URL` is taken as given (`Store`).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from .heartbeat import Progress

T = TypeVar("T")

#: A bare bucket name's scheme (the profiles predating URL stores name a GCS bucket).
DEFAULT_SCHEME = "gs"

#: A URL store's transfers (`get`/`put`) try this many times, sleeping `RETRY_BASE_S · 2^n` between: an R2 read that
#: stalls past fsspec's socket timeout is transient, and one such stall aborted a 153-scan build (cw, 2026-10-15).
RETRIES = 5
RETRY_BASE_S = 2.0


def _retrying(op: Callable[[], T]) -> T:
    """`op()`, retried on a transient error (a timeout, a dropped or truncated response, an I/O error); a missing key or a
    refused credential is not transient and raises at once, as does the last attempt's error."""
    from aiohttp import ClientPayloadError
    from fsspec.exceptions import FSTimeoutError

    for n in range(RETRIES):
        try:
            return op()
        except (FileNotFoundError, PermissionError):
            raise
        except (ClientPayloadError, FSTimeoutError, TimeoutError, OSError):
            if n == RETRIES - 1:
                raise
            time.sleep(RETRY_BASE_S * 2**n)


def store_url(root: str) -> str:
    """`root` as a store root: a URL or a local dir as given (a path holding `/`, or starting with `.` or `~`); a bare
    bucket name as `gs://<name>`."""
    root = root.strip()
    if not root:
        raise ValueError("an empty store root")
    if "://" in root or "/" in root or root.startswith((".", "~")):
        return root.rstrip("/") or "/"
    return f"{DEFAULT_SCHEME}://{root}"


def scheme(root: str) -> str | None:
    """A store root's URL scheme (None: a local dir)."""
    return root.split("://", 1)[0] if "://" in root else None


def serves(root: str, r2_bucket: str | None) -> bool:
    """Whether the store at `root` is the serving R2 bucket `r2_bucket` itself (its root, where the readers' keys
    start): then a copy of the store's files to R2 has nothing to do."""
    return bool(r2_bucket) and store_url(root).rstrip("/") == f"r2://{r2_bucket.strip().strip('/')}"


class Store:
    """The keys under `root`: a local dir or a URL, taken as given (a profile's bucket goes through `store_url` first).
    `listing(prefix)` is `{key: size}` of the files under `prefix/`."""

    def __init__(self, root: str, *, fs=None, base: str | None = None):
        from disk_tree.blobfs import fs_for, is_url

        self.root = root.rstrip("/") or "/"
        self.url = is_url(self.root)
        if fs is not None:
            if base is None or not self.url:
                raise ValueError("an explicit filesystem requires a URL root and a base path")
            self.fs, self.base = fs, base.rstrip("/")
            return
        if self.url:
            self.fs, self.base = fs_for(self.root)
            self.base = self.base.rstrip("/")

    def __repr__(self) -> str:
        return f"Store({self.root!r})"

    def uri(self, key: str = "") -> str:
        """`key`'s full location: `<root>/<key>` (a URL, or a local path), what DuckDB / pyarrow / fsspec open."""
        return f"{self.root}/{key}" if key else self.root

    def _p(self, key: str) -> str:
        return f"{self.base}/{key}"

    def listing(self, prefix: str) -> dict[str, int]:
        prefix = prefix.rstrip("/")
        if not self.url:
            d = Path(self.root) / prefix
            return {p.relative_to(self.root).as_posix(): p.stat().st_size for p in sorted(d.rglob("*")) if p.is_file() and not _is_landing(p)} if d.is_dir() else {}
        # Fresh, not the filesystem's listings cache: other writers (a Batch job, another run) change the store.
        self.fs.invalidate_cache(self._p(prefix))
        try:
            found = self.fs.find(self._p(prefix), detail=True)
        except FileNotFoundError:
            return {}
        return {k[len(self.base) + 1:]: int(v["size"]) for k, v in sorted(found.items()) if v.get("type", "file") == "file"}

    def mtimes(self, prefix: str) -> dict[str, float]:
        """`{key: write time}` (epoch s) of the files under `prefix/`; a URL store's from its listing (`blobfs`'s reading
        of the driver's `mtime` / `LastModified` / `created`). Raises when the driver reports none: a prune must not read
        a manifest as superseded long ago."""
        prefix = prefix.rstrip("/")
        if not self.url:
            d = Path(self.root) / prefix
            return {p.relative_to(self.root).as_posix(): p.stat().st_mtime for p in sorted(d.rglob("*")) if p.is_file() and not _is_landing(p)} if d.is_dir() else {}
        from disk_tree.blobfs import _info_mtime

        self.fs.invalidate_cache(self._p(prefix))
        try:
            found = self.fs.find(self._p(prefix), detail=True)
        except FileNotFoundError:
            return {}
        out = {k[len(self.base) + 1:]: _info_mtime(v) for k, v in sorted(found.items()) if v.get("type", "file") == "file"}
        if bad := [k for k, t in out.items() if not t]:
            raise RuntimeError(f"{self.root}: no write time for {bad[0]} (and {len(bad) - 1} more)")
        return out

    def keys(self, prefix: str) -> list[str]:
        """The keys starting with `prefix` (a dir `a/b/`, or a dir plus a name's start `a/b/c`)."""
        d, _, _ = prefix.rpartition("/")
        return sorted(k for k in self.listing(d) if k.startswith(prefix))

    def exists(self, key: str) -> bool:
        """Whether `key` is a file. Not found (or a directory) is False; any other failure raises — fsspec's `isfile`
        reads a refused credential (403) as False, and a `build` vs `add` decided on that rebuilds over a live store."""
        if not self.url:
            return (Path(self.root) / key).is_file()
        self.fs.invalidate_cache(self._p(key))
        try:
            return self.fs.info(self._p(key)).get("type", "file") == "file"
        except FileNotFoundError:
            return False

    def open(self, key: str):
        """`key` opened for reading (binary)."""
        return open(Path(self.root) / key, "rb") if not self.url else self.fs.open(self._p(key), "rb")

    def read_bytes(self, key: str) -> bytes:
        return (Path(self.root) / key).read_bytes() if not self.url else self.fs.cat_file(self._p(key))

    def read_json(self, key: str) -> dict:
        return json.loads(self.read_bytes(key))

    def write_bytes(self, key: str, data: bytes) -> None:
        if self.url:
            self.fs.pipe_file(self._p(key), data)
        else:
            with _landing(Path(self.root) / key) as tmp:
                tmp.write_bytes(data)

    def write_text(self, key: str, text: str) -> None:
        self.write_bytes(key, text.encode())

    def create(self, key: str, text: str) -> None:
        """Write a new key; `FileExistsError` when it exists (never overwrites). Atomic on a local dir and on `gs://` (a
        generation precondition); elsewhere a check then a write, so one writer per generation (a single-machine job, or
        `append_runner`'s lease)."""
        if not self.url:
            p = Path(self.root) / key
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "x") as fh:
                fh.write(text)
            return
        if scheme(self.root) == "gs":
            from google.api_core.exceptions import PreconditionFailed
            from google.cloud import storage

            bucket, _, path = self._p(key).partition("/")
            try:
                storage.Client().bucket(bucket).blob(path).upload_from_string(text, if_generation_match=0)
            except PreconditionFailed as e:
                raise FileExistsError(key) from e
            return
        if self.exists(key):
            raise FileExistsError(key)
        self.write_text(key, text)

    def delete(self, keys: Iterable[str], *, workers: int = 8) -> int:
        """Delete `keys` (one already gone is skipped); returns how many were there."""
        def one(k: str) -> int:
            try:
                if self.url:
                    self.fs.rm_file(self._p(k))
                else:
                    (Path(self.root) / k).unlink()
            except FileNotFoundError:
                return 0
            return 1

        with ThreadPoolExecutor(workers) as ex:
            return sum(ex.map(one, list(keys)))

    def size(self, key: str) -> int:
        return (Path(self.root) / key).stat().st_size if not self.url else int(self.fs.size(self._p(key)))

    def read_range(self, key: str, lo: int, hi: int) -> bytes:
        """Bytes `[lo, hi)` of `key`: one ranged read."""
        if self.url:
            return self.fs.cat_file(self._p(key), start=lo, end=hi)
        with open(Path(self.root) / key, "rb") as f:
            f.seek(lo)
            return f.read(hi - lo)

    def get(self, key: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self.url:
            _retrying(lambda: self.fs.get(self._p(key), str(dst)))
        else:
            shutil.copyfile(Path(self.root) / key, dst)

    def copy(self, src: str, dst: str) -> None:
        """`src` copied to `dst` within the store: server side on a bucket (a GCS rewrite, an S3/R2 `CopyObject`)."""
        if self.url:
            _retrying(lambda: self.fs.copy(self._p(src), self._p(dst)))
        else:
            out = Path(self.root) / dst
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(self.root) / src, out)

    def put(self, src: Path, key: str) -> None:
        if self.url:
            _retrying(lambda: self.fs.put(str(src), self._p(key)))
        else:
            with _landing(Path(self.root) / key) as tmp:
                shutil.copyfile(src, tmp)

    def put_tree(self, local: Path, prefix: str, *, last: tuple[str, ...] = (), workers: int = 32) -> list[dict]:
        """Every file under `local` → `prefix/<rel>`, the `last` (relative paths, in that order) after all the others
        (completeness markers); `{key, size}` per file."""
        files = sorted(p for p in local.rglob("*") if p.is_file() and p.relative_to(local).as_posix() not in last)
        tail = [local / r for r in last if (local / r).is_file()]
        prog = Progress(f"upload {self.uri(prefix)}", len(files) + len(tail), total_bytes=sum(p.stat().st_size for p in [*files, *tail]), unit="files")

        def one(p: Path) -> dict:
            key = f"{prefix}/{p.relative_to(local).as_posix()}"
            self.put(p, key)
            prog.tick(nbytes=p.stat().st_size)
            return {"key": key, "size": p.stat().st_size}

        with ThreadPoolExecutor(workers) as ex:
            out = list(ex.map(one, files))
        out += [one(p) for p in tail]
        prog.done()
        return out


def mirror(store: Store, prefix: str, local: Path, keep: Callable[[str], bool] = lambda rel: True) -> dict[str, int]:
    """`prefix` (a generation) copied into `local` (files already there with the same size are kept; `keep(rel)` false:
    not copied), `stage_files`' parallel copies, every one or it raises; returns the whole listing."""
    listing = store.listing(prefix)
    todo = {k: n for k, n in listing.items() if keep(rel := k[len(prefix) + 1:]) and not ((dst := local / rel).is_file() and dst.stat().st_size == n)}
    got = stage_files(store.uri(prefix), {k[len(prefix) + 1:]: n for k, n in todo.items()}, local, label=f"mirror {prefix}",
                      log=lambda m: print(m, file=sys.stderr, flush=True))
    if got["failed"]:
        raise OSError(f"mirror {prefix}: {len(got['failed'])} files not copied, e.g. {got['failed'][0]}")
    return listing


#: Processes `stage_files` downloads in from GCS (spawned: none of them shares a GIL with the caller's threads; gcs's
#: first probe ran 64 threads inside the fold's process beside a CPU-saturated DuckDB, 8 KiB per GIL hand-off, and
#: staged at ~24 MB/s), each one ranged GET at a time.
STAGE_WORKERS = 32
#: Bytes per ranged GET: a file is ⌈size / this⌉ requests spread over the workers, and a failure retries one slice.
STAGE_SLICE = 64 << 20
#: Attempts per slice (backing off `RETRY_BASE_S · 2^n`), each GET's own read timeout and its client retries' deadline.
STAGE_TRIES = 4
STAGE_TIMEOUT_S = 600
STAGE_DEADLINE_S = 3600
#: Seconds between `stage_files`' progress lines.
STAGE_PROGRESS_S = 30.0

_buckets: dict[str, object] = {}

#: A local store's write lands as `.<name>.<random>.landing` beside its key, then is renamed into place: a listing (which
#: skips them) never sees a key half written. A bucket's objects appear whole already.
LANDING = ".landing"


@contextmanager
def _landing(dst: Path):
    """A temporary path beside `dst` to write, renamed onto `dst` when the block ends (removed when it raises)."""
    import secrets

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{secrets.token_hex(4)}{LANDING}")
    try:
        yield tmp
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _is_landing(p: Path) -> bool:
    return p.name.startswith(".") and p.name.endswith(LANDING)


def _gcs_slice(bucket: str, name: str, path: str, lo: int, hi: int) -> int:
    """A `stage_files` worker: bytes `[lo, hi)` of `gs://<bucket>/<name>` written into `path` at `lo`; retried
    `STAGE_TRIES` times. Returns the bytes."""
    from google.cloud import storage
    from google.cloud.storage.retry import DEFAULT_RETRY

    if bucket not in _buckets:
        _buckets[bucket] = storage.Client().bucket(bucket)
    b = _buckets[bucket]
    for n in range(STAGE_TRIES):
        try:
            with open(path, "r+b") as fh:
                fh.seek(lo)
                b.blob(name).download_to_file(fh, start=lo, end=hi - 1, checksum=None, timeout=STAGE_TIMEOUT_S,  # type: ignore[attr-defined]
                                              retry=DEFAULT_RETRY.with_timeout(STAGE_DEADLINE_S))
                if fh.tell() != hi:
                    raise OSError(f"gs://{bucket}/{name} [{lo}, {hi}): {fh.tell() - lo} bytes read")
            return hi - lo
        except Exception:
            if n == STAGE_TRIES - 1:
                raise
            time.sleep(RETRY_BASE_S * 2**n)
    raise AssertionError("unreachable")


def _gcs_put(bucket: str, name: str, path: str) -> int:
    """An `upload_files` worker: local `path` → `gs://<bucket>/<name>` (the client checks its crc32c); retried
    `STAGE_TRIES` times. Returns the bytes."""
    from google.cloud import storage
    from google.cloud.storage.retry import DEFAULT_RETRY

    if bucket not in _buckets:
        _buckets[bucket] = storage.Client().bucket(bucket)
    b = _buckets[bucket]
    for n in range(STAGE_TRIES):
        try:
            b.blob(name).upload_from_filename(path, timeout=STAGE_TIMEOUT_S, retry=DEFAULT_RETRY.with_timeout(STAGE_DEADLINE_S))  # type: ignore[attr-defined]
            return os.path.getsize(path)
        except Exception:
            if n == STAGE_TRIES - 1:
                raise
            time.sleep(RETRY_BASE_S * 2**n)
    raise AssertionError("unreachable")


def upload_files(root: str, files: Mapping[str, Path], *, workers: int = STAGE_WORKERS, label: str = "upload",
                 log: Callable[[str], None] = print) -> dict:
    """Local files (`{key: path}`) to `<root>/<key>`: a checkpoint's save. To `gs://`, over the JSON API in `workers`
    spawned processes, a file each (a thread pool's `Store.put`s ran at ~0.25–0.55 GB/s: gcs's c001 fold waited 16 + 24
    min on two saves); elsewhere `workers` `Store.put`s at a time on threads. Any file failing its retries raises.
    Returns `{files, bytes, s}`."""
    t0 = time.monotonic()
    sizes = {k: Path(p).stat().st_size for k, p in files.items()}
    rate = _Rate(label, len(files), sum(sizes.values()), log)
    if scheme(root) == "gs":
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor, as_completed

        bucket, _, base = root.removeprefix("gs://").partition("/")
        with ProcessPoolExecutor(max(1, workers), mp_context=mp.get_context("spawn")) as ex:
            futs = {ex.submit(_gcs_put, bucket, f"{base}/{k}" if base else k, str(p)): k for k, p in sorted(files.items())}
            for f in as_completed(futs):
                f.result()
                rate.tick(1, sizes[futs[f]])
    else:
        store = Store(root)

        def one(kv: tuple[str, Path]) -> None:
            store.put(Path(kv[1]), kv[0])
            rate.tick(1, sizes[kv[0]])

        with ThreadPoolExecutor(max(1, workers)) as ex:
            list(ex.map(one, sorted(files.items())))
    rate.tick(final=True)
    return {"files": len(files), "bytes": sum(sizes.values()), "s": round(time.monotonic() - t0, 1)}


def _crc32c(path: str) -> str:
    """`path`'s crc32c as GCS lists it (base64 of the big-endian value)."""
    import base64

    import google_crc32c

    c = google_crc32c.Checksum()
    with open(path, "rb") as fh:
        while b := fh.read(16 << 20):
            c.update(b)
    return base64.b64encode(c.digest()).decode()


class _Rate:
    """`stage_files`' progress lines: files and bytes done, and MB/s, at most one per `STAGE_PROGRESS_S`, then the last."""

    def __init__(self, label: str, files: int, total: int, log: Callable[[str], None]):
        self.label, self.files, self.total, self.log = label, files, total, log
        self.t0 = self.last = time.monotonic()
        self.done = self.nbytes = 0
        self.lock = threading.Lock()

    def tick(self, files: int = 0, nbytes: int = 0, final: bool = False) -> None:
        with self.lock:
            self.done += files
            self.nbytes += nbytes
            now = time.monotonic()
            if not final and now - self.last < STAGE_PROGRESS_S:
                return
            self.last = now
            s = max(now - self.t0, 1e-3)
            self.log(f"{self.label} {self.done}/{self.files} files, {self.nbytes / 2**30:,.2f} of {self.total / 2**30:,.2f} GiB in {s:.0f}s "
                     f"({self.nbytes / s / 1e6:,.0f} MB/s){' (done)' if final else ''}")


def stage_files(root: str, keys: dict[str, int], dst: Path, *, workers: int = STAGE_WORKERS, label: str = "stage",
                log: Callable[[str], None] = print) -> dict:
    """`keys` (`{key: size}` under `root`: a local dir or a store URL) copied to `dst/<key>`: a local cache of what a
    fold reads many times. Each copy is written to `<key>.part`, checked against its listed size (and on GCS its
    object's crc32c), then renamed into place.

    A `gs://` root downloads over the GCS JSON API (not a bucket mount) in `workers` spawned processes, each file in
    `STAGE_SLICE` ranged GETs; others copy `workers` files at a time on threads (`Store.get`). A file whose copy still
    fails after its retries is logged and left out (`failed`): a cache, so its reader reads it in place. Returns
    `{files, bytes, s, failed}` (`files`, `bytes`: what was staged)."""
    t0 = time.monotonic()
    rate = _Rate(label, len(keys), sum(keys.values()), log)
    failed: dict[str, str] = {}
    staged: list[str] = []

    def part(key: str) -> Path:
        out = dst / key
        out.parent.mkdir(parents=True, exist_ok=True)
        return out.with_name(out.name + ".part")

    def land(key: str) -> None:
        part(key).rename(dst / key)
        staged.append(key)

    def fail(key: str, e: BaseException) -> None:
        if key not in failed:
            failed[key] = f"{type(e).__name__}: {e}"
            log(f"{label}: {key} not staged ({failed[key]}); read in place")
        part(key).unlink(missing_ok=True)

    if scheme(root) == "gs":
        _stage_gcs(root, keys, part, land, fail, rate, workers)
    else:
        store = Store(root)

        def one(kv: tuple[str, int]) -> None:
            key, size = kv
            try:
                store.get(key, part(key))
                if (got := part(key).stat().st_size) != size:
                    raise OSError(f"{got} bytes staged, {size} listed")
            except Exception as e:
                fail(key, e)
                return
            land(key)
            rate.tick(1, size)

        with ThreadPoolExecutor(max(1, workers)) as ex:
            list(ex.map(one, sorted(keys.items())))
    rate.tick(final=True)
    return {"files": len(staged), "bytes": sum(keys[k] for k in staged), "s": round(time.monotonic() - t0, 1), "failed": sorted(failed)}


def _stage_gcs(root: str, keys: dict[str, int], part: Callable[[str], Path], land: Callable[[str], None],
               fail: Callable[[str, BaseException], None], rate: _Rate, workers: int) -> None:
    """`stage_files` from `gs://`: every file's slices over a pool of spawned processes; a file whose slices all landed
    is checked (size, then crc32c in a worker) before `land`; any failure `fail`s the file."""
    import multiprocessing as mp
    from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait

    from google.cloud import storage

    bucket, _, base = root.removeprefix("gs://").partition("/")
    name = lambda key: f"{base}/{key}" if base else key  # noqa: E731
    listed = {b.name: (int(b.size), b.crc32c) for b in storage.Client().list_blobs(bucket, prefix=f"{base}/" if base else None)}
    left: dict[str, int] = {}
    with ProcessPoolExecutor(max(1, workers), mp_context=mp.get_context("spawn")) as ex:
        pending: dict[Future, tuple[str, str]] = {}

        def check(key: str) -> None:
            pending[ex.submit(_crc32c, str(part(key)))] = (key, "crc")

        for key, size in sorted(keys.items()):
            got = listed.get(name(key))
            if got is None or got[0] != size:
                fail(key, OSError(f"gs://{bucket}/{name(key)}: listed {got[0] if got else 'missing'}, expected {size} bytes"))
                continue
            with open(part(key), "wb") as fh:
                fh.truncate(size)
            spans = [(lo, min(lo + STAGE_SLICE, size)) for lo in range(0, size, STAGE_SLICE)]
            left[key] = len(spans)
            if not spans:
                check(key)
            for lo, hi in spans:
                pending[ex.submit(_gcs_slice, bucket, name(key), str(part(key)), lo, hi)] = (key, "slice")
        while pending:
            done, _ = wait(list(pending), timeout=STAGE_PROGRESS_S, return_when=FIRST_COMPLETED)
            for f in done:
                key, what = pending.pop(f)
                if key not in left:  # already failed
                    continue
                if (e := f.exception()) is not None:
                    del left[key]
                    fail(key, e)
                elif what == "slice":
                    rate.tick(0, f.result())
                    left[key] -= 1
                    if left[key] == 0:
                        check(key)
                elif f.result() != listed[name(key)][1]:
                    del left[key]
                    fail(key, OSError(f"crc32c {f.result()}, listed {listed[name(key)][1]}"))
                else:
                    del left[key]
                    land(key)
                    rate.tick(1)
            rate.tick()


#: Files `upload_new` (and `put_tree`) keep in flight within one parallel tier: a compacted generation is ~300 GiB in
#: thousands of files, and one stream to GCS or R2 carries ~100 MB/s.
UPLOAD_WORKERS = 32


def upload_new(store: Store, local: Path, prefix: str, before: dict[str, int], order: Callable[[list[str]], list[str]] = sorted,
               *, tier: Callable[[str], object] | None = None, workers: int = UPLOAD_WORKERS) -> list[str]:
    """Every file under `local` (the mirror of `prefix`) not in `before` → `store`, in `order` (the store's publish order:
    data, then liveness markers, then manifests). Files in `before` are never rewritten (a generation's files are written
    once).

    `tier(key)`: consecutive keys (in `order`) of one tier other than None go up together, `workers` at a time, and all of
    them before the next key starts; a key of tier None goes up alone, after every key before it and before any after it
    (a liveness marker, a manifest). Without `tier`, every key is its own step: one at a time, in order."""
    keys = [p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file()]
    new = order([k for k in keys if f"{prefix}/{k}" not in before])
    prog = Progress(f"upload {prefix}", len(new), total_bytes=sum((local / k).stat().st_size for k in new), unit="files")

    def one(k: str) -> None:
        store.put(local / k, f"{prefix}/{k}")
        prog.tick(nbytes=(local / k).stat().st_size)

    with ThreadPoolExecutor(max(1, workers)) as ex:
        for step in publish_steps(new, tier):
            if len(step) == 1:
                one(step[0])
            else:
                list(ex.map(one, step))  # every one up (a failure raises) before the next step
    prog.done()
    return new


def publish_steps(keys: list[str], tier: Callable[[str], object] | None) -> list[list[str]]:
    """`keys` (in publish order) as `upload_new`'s steps: maximal runs of consecutive keys of one tier other than None,
    and each key of tier None alone."""
    steps: list[list[str]] = []
    last = None
    for k in keys:
        t = tier(k) if tier is not None else None
        if t is not None and steps and t == last:
            steps[-1].append(k)
        else:
            steps.append([k])
        last = t
    return steps


class RunStore:
    """`append_runner.RunStore` over a `Store`: a generation's keys (relative to `<prefix>/<gen>/`) in `data`, the merge
    lease in `scratch` (`<prefix>/<gen>/merge.lease.json`; None: a store that never takes it). `create` is the `Store`'s
    (atomic on `gs://` and local dirs); so is the lease, whose stale holder is replaced by a delete then a create."""

    def __init__(self, data: Store, scratch: Store | None, gen: str, *, prefix: str):
        self.data, self.scratch, self.gen, self.prefix = data, scratch, gen, f"{prefix}/{gen}"

    def listing(self, prefix: str) -> dict[str, int]:
        return {k.removeprefix(self.prefix + "/"): n for k, n in self.data.listing(self.prefix).items() if k.startswith(f"{self.prefix}/{prefix}")}

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.listing(prefix))

    def exists(self, key: str) -> bool:
        return self.data.exists(f"{self.prefix}/{key}")

    def read_json(self, key: str) -> dict:
        return self.data.read_json(f"{self.prefix}/{key}")

    def create(self, key: str, text: str) -> None:
        try:
            self.data.create(f"{self.prefix}/{key}", text)
        except FileExistsError as e:
            raise FileExistsError(key) from e

    def upload(self, local: Path, prefix: str, last: tuple[str, ...] = ("meta.json",)) -> list[dict]:
        return self.data.put_tree(local, f"{self.prefix}/{prefix}", last=last)

    def lease(self, owner: str, now: datetime, ttl_s: int) -> dict | None:
        from .append_runner import lease_stale

        key = f"{self.prefix}/merge.lease.json"
        for _ in range(2):
            try:
                self.scratch.create(key, json.dumps({"owner": owner, "at": now.isoformat()}) + "\n")
                return None
            except FileExistsError:
                try:
                    held = self.scratch.read_json(key)
                except FileNotFoundError:
                    continue
                if not lease_stale(held, now, ttl_s):
                    return held
                self.scratch.delete([key])
        return {"owner": "?", "at": "?"}

    def release(self, owner: str) -> None:
        key = f"{self.prefix}/merge.lease.json"
        try:
            if self.scratch.read_json(key)["owner"] == owner:
                self.scratch.delete([key])
        except FileNotFoundError:
            pass


def run_store(data: str, scratch: str | None, gen: str, *, prefix: str):
    """The `append_runner.RunStore` of a generation on the store `data` (a bucket name or URL): `GcsRunStore` (the GCS
    client's preconditions throughout) on whole `gs://` buckets, else `RunStore` over `Store`s."""
    if gs_bucket(data) and (scratch is None or gs_bucket(scratch)):
        from .append_runner import GcsRunStore

        return GcsRunStore(gs_bucket(data), gs_bucket(scratch) if scratch else None, gen, prefix=prefix)
    return RunStore(Store(store_url(data)), Store(store_url(scratch)) if scratch else None, gen, prefix=prefix)


def gs_bucket(root: str) -> str | None:
    """The GCS bucket a store root is the whole of (`gs://<bucket>`, or a bare name), else None."""
    u = store_url(root)
    if scheme(u) != "gs":
        return None
    name = u.removeprefix("gs://").strip("/")
    return None if "/" in name else name
