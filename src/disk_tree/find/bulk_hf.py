"""Hugging Face plug-in for ``disk-tree bulk-list``: a namespace's storage as one listing.

``hf://<namespace>`` lists an org's (or user's) Storage Buckets and its model,
dataset and space repos into canonical layer-1 shards (``bucket`` = the
namespace), so the namespace aggregates and renders like any bucket.
``hf://<namespace>/<kind>`` narrows to one kind and
``hf://<namespace>/<kind>/<name>`` to one bucket or repo; the URI path is
exactly the listing prefix it covers.

**Path layout** (``name`` column, under ``bucket = <namespace>``)::

    buckets/<bucket>/<path>                    every file in a Storage Bucket
    models/<repo>/<path>                       the default branch's current tree
    datasets/<repo>/<path>                     (likewise; spaces/ too)
    <kind>/<repo>/@history/<filename>/<oid>    an LFS object the current tree no
                                               longer references (`lfs_files` mode)
    <kind>/<repo>/@history/@unlisted           the rest of the repo's billed bytes

**Totals match billing.** HF bills a repo's ``usedStorage``: every LFS/Xet
object it stores, across all refs and history, each once — plain git blobs
(READMEs, small JSON) count 0. So a repo's rows are its current tree (git
files at their real size but in their own class, ``git``; LFS files
deduplicated by oid: a second path pointing at an already-listed object
lists at size 0), plus old versions:

- ``lfs_files=True``: ``GET /api/<kind>/<id>/lfs-files`` lists every stored
  LFS object, unreferenced ones included, with its pushed-at time; those the
  current tree doesn't reference land under ``@history/<filename>/<oid>``.
  The endpoint needs **write** access to the repo (a read token gets 403).
- Always: whatever ``usedStorage`` still exceeds the listed LFS bytes by
  (other branches, PR refs, old versions without ``lfs_files``) is one
  ``@history/@unlisted`` row, so the repo's LFS rows sum to ``usedStorage``
  exactly. A listing that *exceeds* it (``usedStorage`` is recomputed
  asynchronously after a push) only warns.

A bucket's rows sum to its ``size`` (the tree is flat and versionless); a
mismatch warns.

**Columns.** ``created`` is a repo file's last-commit date
(``expand=true``; ``commit_dates=False`` uses the repo's ``lastModified``
instead, at 1000 entries per tree page rather than 50), an old version's
``pushedAt``, the residual's repo ``lastModified`` (an upper bound on its
bytes' age), and a bucket file's ``uploadedAt`` (falling back to its
``mtime``). ``storage_class_id`` is the billing class, per
:data:`~disk_tree.listing.HF_CLASS_IDS`: public 1, private 5, unbilled git
files 6 — so every non-6 byte is a billed byte. ``generation`` is NULL.

**Rate limits.** The Hub answers with ``RateLimit: "api";r=<left>;t=<secs>``
under a fixed 1000-request / 300 s window (a free account's quota). The
client keeps ``concurrency`` requests in flight (≈8 drew 429s), stops issuing
while the window is nearly spent until it resets, and retries 429 / 5xx
honoring ``Retry-After``. Throughput is therefore window-bound, ~3.3 req/s.

Auth: ``HF_TOKEN`` (a read token sees private repos and buckets; none lists
public ones only). ``HF_ENDPOINT`` overrides ``https://huggingface.co``.
No request writes to the Hub.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional
from urllib.parse import quote, urlencode

from disk_tree.listing import HF_CLASS_IDS


def err(*args) -> None:
    print(*args, file=sys.stderr)  # resolved per call, so capture (pytest `capsys`) sees it


DEFAULT_ENDPOINT = "https://huggingface.co"
REPO_KINDS = ("models", "datasets", "spaces")
KINDS = ("buckets", *REPO_KINDS)
HISTORY_DIR = "@history"
UNLISTED = "@unlisted"


# --- URIs --------------------------------------------------------------------

@dataclass(frozen=True)
class HfTarget:
    """What an ``hf://`` URI covers: a namespace, optionally one kind, optionally one name."""
    namespace: str
    kind: Optional[str] = None
    name: Optional[str] = None

    @property
    def prefix(self) -> str:
        """The listing prefix the target covers (``''`` for a whole namespace)."""
        return "".join(f"{p}/" for p in (self.kind, self.name) if p)


def parse_hf_uri(uri: str) -> HfTarget:
    """``hf://<ns>[/<kind>[/<name>]]`` → :class:`HfTarget` (case preserved)."""
    if not uri.startswith("hf://"):
        raise ValueError(f"not an hf:// URI: {uri!r}")
    parts = [p for p in uri[len("hf://"):].split("/") if p]
    if not parts or len(parts) > 3:
        raise ValueError(f"expected hf://<namespace>[/<kind>[/<name>]]; got {uri!r}")
    namespace, *rest = parts
    kind = rest[0] if rest else None
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r} in {uri!r}; expected one of {', '.join(KINDS)}")
    return HfTarget(namespace, kind, rest[1] if len(rest) > 1 else None)


# --- HTTP --------------------------------------------------------------------

@dataclass
class Response:
    status: int
    headers: "dict[str, str]"  # lower-cased names
    body: bytes


Transport = Callable[[str, "dict[str, str]"], Response]


def urllib_transport(url: str, headers: "dict[str, str]") -> Response:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')
_RATELIMIT = re.compile(r"\br=(\d+)\b.*?\bt=(\d+)\b")


class HfError(RuntimeError):
    def __init__(self, status: int, url: str, body: bytes):
        self.status = status
        super().__init__(f"HTTP {status} for {url}: {body[:300].decode(errors='replace')}")


@dataclass
class HfClient:
    """Rate-limited, paginating GET-only client for the Hub API.

    ``transport``, ``sleep`` and ``clock`` are injectable so tests replay
    recorded responses without a network or real waits.
    """
    token: Optional[str] = None
    endpoint: str = DEFAULT_ENDPOINT
    concurrency: int = 4
    max_retries: int = 8
    # Stop issuing once the window has this few requests left; in-flight
    # requests (≤ concurrency) still land inside it.
    reserve: Optional[int] = None
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    requests: int = 0
    waited: float = 0.0
    _slots: threading.Semaphore = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)
    _resume_at: float = field(init=False, default=0.0, repr=False)

    def __post_init__(self):
        self._slots = threading.Semaphore(self.concurrency)
        self._lock = threading.Lock()
        if self.reserve is None:
            self.reserve = self.concurrency

    def _url(self, path_or_url: str, params: "Optional[list[tuple[str, str]]]" = None) -> str:
        url = path_or_url if "://" in path_or_url else f"{self.endpoint}{path_or_url}"
        return f"{url}?{urlencode(params)}" if params else url

    def _pause(self, seconds: float) -> None:
        """Hold every thread's next request for ``seconds`` (window spent / 429)."""
        with self._lock:
            self._resume_at = max(self._resume_at, self.clock() + seconds)

    def _wait_turn(self) -> None:
        while True:
            with self._lock:
                delay = self._resume_at - self.clock()
            if delay <= 0:
                return
            self.waited += delay
            self.sleep(delay)

    def get(self, url: str) -> Response:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            with self._slots:
                r = self.transport(url, headers)
            with self._lock:
                self.requests += 1
            m = _RATELIMIT.search(r.headers.get("ratelimit", ""))
            if m and int(m.group(1)) <= (self.concurrency if self.reserve is None else self.reserve):
                self._pause(int(m.group(2)) + 1)
            if r.status == 429 or r.status >= 500:
                if attempt == self.max_retries:
                    break
                retry_after = r.headers.get("retry-after")
                if retry_after is not None and retry_after.isdigit():
                    delay = float(retry_after)
                elif m and r.status == 429:
                    delay = float(int(m.group(2)) + 1)
                else:
                    delay = float(min(2 ** attempt, 60))
                err(f"hf: HTTP {r.status} on {url}; retrying in {delay:g}s ({attempt + 1}/{self.max_retries})")
                self._pause(delay)
                continue
            break
        if r.status != 200:
            raise HfError(r.status, url, r.body)
        return r

    def get_json(self, path: str, params: "Optional[list[tuple[str, str]]]" = None):
        return json.loads(self.get(self._url(path, params)).body)

    def paginate(self, path: str, params: "Optional[list[tuple[str, str]]]" = None) -> Iterator[dict]:
        """Every item of a ``Link: <…>; rel="next"``-paginated JSON array."""
        url: Optional[str] = self._url(path, params)
        while url is not None:
            r = self.get(url)
            yield from json.loads(r.body)
            m = _LINK_NEXT.search(r.headers.get("link", ""))
            url = m.group(1) if m else None


# --- listing -----------------------------------------------------------------

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _lfs_sha256(o: dict) -> str:
    """An ``lfs-files`` item's SHA-256 (the tree's ``lfs.oid``). The item carries
    both ``fileOid`` and ``oid`` and huggingface_hub's docs leave which one is
    the SHA-256 ambiguous, so take whichever has its shape."""
    for k in ("fileOid", "oid"):
        v = o.get(k)
        if isinstance(v, str) and _SHA256.fullmatch(v):
            return v
    raise ValueError(f"lfs-files item without a SHA-256 oid: {o}")


def _visibility(private: Optional[bool]) -> str:
    return "private" if private else "public"


@dataclass
class UnitTotals:
    """One bucket's or repo's reconciliation: what the Hub bills vs what was listed."""
    unit: str             # e.g. `datasets/foo`
    expected: int         # bucket `size` / repo `usedStorage`
    listed: int           # listed bytes the expectation covers (bucket: all; repo: LFS)
    git_bytes: int = 0    # repo: current-tree non-LFS bytes (listed, not billed)
    residual: int = 0     # repo: the `@history/@unlisted` row's size
    history: int = 0      # repo: `@history/<filename>/<oid>` bytes (lfs_files mode)


def list_bucket(client: HfClient, namespace: str, name: str, private: Optional[bool], size: Optional[int]) -> "tuple[list, UnitTotals]":
    rows = []
    cls = _visibility(private)
    for e in client.paginate(f"/api/buckets/{namespace}/{quote(name)}/tree", [("recursive", "true")]):
        if e["type"] != "file":
            continue
        rows.append((f"buckets/{name}/{e['path']}", int(e["size"]), e.get("uploadedAt") or e.get("mtime"), cls, None))
    listed = sum(r[1] for r in rows)
    totals = UnitTotals(f"buckets/{name}", expected=listed if size is None else int(size), listed=listed)
    if totals.expected != listed:
        err(f"WARN: hf bucket {namespace}/{name}: listed {listed:,} B, API size {totals.expected:,} B")
    return rows, totals


def list_repo(
    client: HfClient,
    kind: str,
    namespace: str,
    name: str,
    lfs_files: bool = False,
    commit_dates: bool = True,
) -> "tuple[list, UnitTotals]":
    repo_id = f"{namespace}/{name}"
    base = f"/api/{kind}/{namespace}/{quote(name)}"
    info = client.get_json(base, [
        ("expand[]", "usedStorage"), ("expand[]", "private"),
        ("expand[]", "lastModified"), ("expand[]", "sha"),
    ])
    cls = _visibility(info.get("private"))
    modified = info.get("lastModified")
    unit = f"{kind}/{name}"
    rows = []
    oids: "set[str]" = set()
    lfs_bytes = git_bytes = 0
    if info.get("sha"):  # an empty repo has no commit to list
        params = [("recursive", "true")] + ([("expand", "true")] if commit_dates else [])
        for e in client.paginate(f"{base}/tree/{info['sha']}", params):
            if e["type"] != "file":
                continue
            created = (e.get("lastCommit") or {}).get("date") or modified
            size = int(e["size"])
            oid = (e.get("lfs") or {}).get("oid") or e.get("xetHash")
            file_cls = cls
            if oid is None:
                git_bytes += size
                file_cls = "git"
            elif oid in oids:
                size = 0  # stored (and billed) once; the first path listed carries it
            else:
                oids.add(oid)
                lfs_bytes += size
            rows.append((f"{unit}/{e['path']}", size, created, file_cls, None))
    history = 0
    if lfs_files:
        try:
            objects = list(client.paginate(f"{base}/lfs-files"))
        except HfError as e:
            if e.status == 403:
                raise HfError(403, f"{base}/lfs-files (needs a token with write access to {repo_id})", b"") from e
            raise
        for o in objects:
            oid = _lfs_sha256(o)
            if oid in oids:
                continue
            oids.add(oid)
            history += int(o["size"])
            filename = (o.get("filename") or "").strip("/") or "?"
            rows.append((f"{unit}/{HISTORY_DIR}/{filename}/{oid}", int(o["size"]), o.get("pushedAt"), cls, None))
    used = int(info.get("usedStorage") or 0)
    residual = used - lfs_bytes - history
    if residual > 0:
        rows.append((f"{unit}/{HISTORY_DIR}/{UNLISTED}", residual, modified, cls, None))
    elif residual < 0:
        err(f"WARN: hf {kind[:-1]} {repo_id}: listed {lfs_bytes + history:,} LFS B > usedStorage {used:,} B")
    totals = UnitTotals(unit, expected=used, listed=lfs_bytes + history, git_bytes=git_bytes, residual=max(residual, 0), history=history)
    return rows, totals


@dataclass(frozen=True)
class Unit:
    kind: str
    name: str
    private: Optional[bool] = None
    size: Optional[int] = None  # buckets: the API's total


def enumerate_units(client: HfClient, target: HfTarget) -> "list[Unit]":
    """Every bucket / repo the target covers, sorted by (kind, name)."""
    ns = target.namespace
    kinds = [target.kind] if target.kind else list(KINDS)
    units = []
    for kind in kinds:
        if kind == "buckets":
            if target.name:
                b = client.get_json(f"/api/buckets/{ns}/{quote(target.name)}")
                units.append(Unit(kind, target.name, b.get("private"), b.get("size")))
                continue
            for b in client.paginate(f"/api/buckets/{ns}"):
                units.append(Unit(kind, b["id"].split("/", 1)[1], b.get("private"), b.get("size")))
        elif target.name:
            units.append(Unit(kind, target.name))
        else:
            for r in client.paginate(f"/api/{kind}", [("author", ns), ("limit", "1000")]):
                units.append(Unit(kind, r["id"].split("/", 1)[1]))
    return sorted(units, key=lambda u: (u.kind, u.name))


def list_hf(
    client: HfClient,
    target: HfTarget,
    lfs_files: bool = False,
    commit_dates: bool = True,
    threads: int = 4,
    progress_s: float = 60,
) -> "Iterator[tuple[list, UnitTotals]]":
    """``(rows, totals)`` per bucket / repo, units fetched ``threads`` at a time
    (the client still bounds requests in flight), yielded in (kind, name) order.
    A progress line at most every ``progress_s`` seconds: a rate-bound listing
    otherwise says nothing for its whole run."""
    units = enumerate_units(client, target)
    err(f"hf://{target.namespace}/{target.prefix}: {len(units)} buckets/repos")
    lock = threading.Lock()
    done, last = 0, time.monotonic()

    def listed(u: Unit):
        if u.kind == "buckets":
            return list_bucket(client, target.namespace, u.name, u.private, u.size)
        return list_repo(client, u.kind, target.namespace, u.name, lfs_files=lfs_files, commit_dates=commit_dates)

    def one(u: Unit):
        nonlocal done, last
        res = listed(u)
        with lock:
            done += 1
            now = time.monotonic()
            if done < len(units) and now - last >= progress_s:
                err(f"hf: {done}/{len(units)} buckets/repos, {client.requests:,} requests, waited {client.waited:.0f}s")
                last = now
        return res

    with ThreadPoolExecutor(max(threads, 1)) as ex:
        yield from ex.map(one, units)


def list_hf_to_parquet(
    uri: str,
    out_dir: str,
    threads: int = 4,
    exists: str = "error",
    lfs_files: bool = False,
    commit_dates: bool = True,
    client: Optional[HfClient] = None,
) -> int:
    """List ``hf://…`` into canonical listing shards + ``_SUCCESS.json`` at ``out_dir``.

    The marker carries the reconciliation totals (``expected`` = Σ bucket
    ``size`` + Σ repo ``usedStorage``, i.e. the billing page's total) and the
    request count. Returns the object count.
    """
    import fsspec

    from disk_tree.find.bulk import (
        ROWS_PER_SHARD, SUCCESS_MARKER, _write_shard, entries_to_frame, resolve_existing, utc_now,
    )

    target = parse_hf_uri(uri)
    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)
    reused = resolve_existing(out_fs, out_root, exists)
    if reused is not None:
        return int(reused["objects"])
    if client is None:
        token = os.environ.get("HF_TOKEN")
        if not token:
            err("WARN: no HF_TOKEN; listing public buckets/repos only")
        client = HfClient(token=token, endpoint=os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT), concurrency=threads)
    started = utc_now()
    t0 = time.monotonic()

    bucket = target.namespace
    buffer: list = []
    n_shards = total = 0
    sums = {"expected": 0, "listed": 0, "git_bytes": 0, "residual": 0, "history": 0}
    over: "list[str]" = []

    def flush():
        nonlocal n_shards, buffer
        if buffer or n_shards == 0:
            _write_shard(out_dir, f"shard-{n_shards:04d}", entries_to_frame(bucket, buffer, HF_CLASS_IDS))
            n_shards += 1
            buffer = []

    for rows, t in list_hf(client, target, lfs_files=lfs_files, commit_dates=commit_dates, threads=threads):
        buffer.extend(rows)
        total += len(rows)
        for k in sums:
            sums[k] += getattr(t, k)
        if t.listed > t.expected:
            over.append(t.unit)
        if len(buffer) >= ROWS_PER_SHARD:
            flush()
    flush()
    elapsed = time.monotonic() - t0
    out_fs.pipe(
        f"{out_root}/{SUCCESS_MARKER}",
        json.dumps({
            "bucket": bucket, "prefix": target.prefix or None, "objects": total,
            "started": started, "finished": utc_now(),
            "hf": {**sums, "over": over, "requests": client.requests, "waited_s": round(client.waited, 1)},
        }).encode(),
    )
    err(
        f"hf://{bucket}/{target.prefix}: {total:,} objects, {sums['expected']:,} B billed"
        f" ({sums['listed']:,} listed LFS/bucket + {sums['residual']:,} @unlisted; +{sums['git_bytes']:,} git B)"
        f" in {client.requests:,} requests, {elapsed:.0f}s → {out_dir}"
    )
    return total
