"""The store seam: a generation store's keys under one root, a local dir or an fsspec URL (`gs://`, `r2://`, `s3://`,
`memory://`; `r2://` takes its endpoint from `DISK_TREE_R2_ENDPOINT_URL` or `buckets.yml`, `disk_tree.blobfs.fs_for`).
Keys are relative to the root and `/`-separated, as the readers' `INDEX_R2` keys are relative to their bucket.

The static name index's single-machine path (`static_local`) and the interval store (`interval_store`,
`interval_append`, `interval_local`) read, write and list through it; `RunStore` adapts it to `append_runner`'s
runs-and-manifests protocol.

A deployment's profile names its store by URL; a bare bucket name there means `gs://<name>` (`store_url`), as the
profiles written before the seam did. A CLI's `-o DIR|URL` is taken as given (`Store`).
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

#: A bare bucket name's scheme (the profiles predating URL stores name a GCS bucket).
DEFAULT_SCHEME = "gs"


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

    def __init__(self, root: str):
        from disk_tree.blobfs import fs_for, is_url

        self.root = root.rstrip("/") or "/"
        self.url = is_url(self.root)
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
            return {p.relative_to(self.root).as_posix(): p.stat().st_size for p in sorted(d.rglob("*")) if p.is_file()} if d.is_dir() else {}
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
            return {p.relative_to(self.root).as_posix(): p.stat().st_mtime for p in sorted(d.rglob("*")) if p.is_file()} if d.is_dir() else {}
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
        if not self.url:
            return (Path(self.root) / key).is_file()
        self.fs.invalidate_cache(self._p(key))
        return self.fs.isfile(self._p(key))

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
            dst = Path(self.root) / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)

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

    def get(self, key: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self.url:
            self.fs.get(self._p(key), str(dst))
        else:
            shutil.copyfile(Path(self.root) / key, dst)

    def put(self, src: Path, key: str) -> None:
        if self.url:
            self.fs.put(str(src), self._p(key))
        else:
            dst = Path(self.root) / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

    def put_tree(self, local: Path, prefix: str, *, last: tuple[str, ...] = (), workers: int = 8) -> list[dict]:
        """Every file under `local` → `prefix/<rel>`, the `last` (relative paths, in that order) after all the others
        (completeness markers); `{key, size}` per file."""
        files = sorted(p for p in local.rglob("*") if p.is_file() and p.relative_to(local).as_posix() not in last)
        tail = [local / r for r in last if (local / r).is_file()]

        def one(p: Path) -> dict:
            key = f"{prefix}/{p.relative_to(local).as_posix()}"
            self.put(p, key)
            return {"key": key, "size": p.stat().st_size}

        with ThreadPoolExecutor(workers) as ex:
            out = list(ex.map(one, files))
        return out + [one(p) for p in tail]


def mirror(store: Store, prefix: str, local: Path, keep: Callable[[str], bool] = lambda rel: True) -> dict[str, int]:
    """`prefix` (a generation) copied into `local` (files already there with the same size are kept; `keep(rel)` false:
    not copied); returns the whole listing."""
    listing = store.listing(prefix)
    for key, size in listing.items():
        rel = key[len(prefix) + 1:]
        dst = local / rel
        if keep(rel) and not (dst.is_file() and dst.stat().st_size == size):
            store.get(key, dst)
    return listing


def upload_new(store: Store, local: Path, prefix: str, before: dict[str, int], order: Callable[[list[str]], list[str]] = sorted) -> list[str]:
    """Every file under `local` (the mirror of `prefix`) not in `before` → `store`, in `order` (the store's publish order:
    data, then liveness markers, then manifests). Files in `before` are never rewritten (a generation's files are written
    once)."""
    keys = [p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file()]
    new = order([k for k in keys if f"{prefix}/{k}" not in before])
    for k in new:
        store.put(local / k, f"{prefix}/{k}")
    return new


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
