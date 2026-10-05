"""Delete index generation dirs no D1 pointer names (`index-gc -F`).

Every index run writes its tiers under a fresh `<layer-2>/index/<gen>/` and
flips the D1 pointer (`index_schema.dir`) to it last (`index_footer`'s
generation protocol). `index-gc` sweeps the superseded generation's D1 rows,
but its files stay — in the GCS scan store and, once `publish-r2` copied them,
in the R2 serving bucket — so every reindex leaves a generation behind in both
(specs/storage-consolidation.md phase 1: 26 GB in each bucket on cw).

The site reads generations only through their pointers, so an unpointed one is
unreachable. A generation dir is deleted only when all three hold:

1. no `index_schema` row (any variant, any store) names it;
2. its scan has a pointed generation: the gen roots swept are exactly the
   parents of the scan's `path` pointers, so a scan whose `path` pointer is
   missing (a failed sync, or never synced) is never touched, and the
   pointed generation beside the deleted ones always survives;
3. its newest object is older than ``min_age`` — a reindex in flight writes
   its new generation before the pointer flips, and is never raced.

Each store is listed under each root (a gen = the first path segment below
`…/index/`, with objects below it); loose files directly under the root (the
base's pre-generation layout) are not generations and are never touched. The
pointers are re-read right before deleting, so a flip that landed during the
listing keeps its target.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Protocol

err = partial(print, file=sys.stderr)

# A pointer dir that is a generation: `<root>/index/<gen>` (the root keeps its
# trailing `index/`). The base's legacy pointers (`listing/<date>`, gen
# `legacy`) don't match, so they never make a root.
GEN_DIR = re.compile(r"^(?P<root>.+/index/)(?P<gen>[^/]+)$")

_AGE = re.compile(r"^(?P<n>\d+(?:\.\d+)?)(?P<u>[smhdw])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_age(s: str) -> float:
    """`90m`, `36h`, `2d`, `1w` (or `0`) → seconds."""
    s = s.strip()
    if s == "0":
        return 0.0
    m = _AGE.match(s)
    if not m:
        raise ValueError(f"bad age {s!r} (want <number><s|m|h|d|w>, e.g. 2d)")
    return float(m["n"]) * _UNIT[m["u"]]


@dataclass(frozen=True)
class Blob:
    key: str
    size: int
    mtime: float  # epoch seconds


class GenStore(Protocol):
    """A bucket the generation dirs live in (GCS scan store, R2 serving bucket)."""

    name: str  # display + identity, e.g. `gs://<bucket>`

    def ls(self, prefix: str) -> list[Blob]: ...

    def delete(self, keys: list[str]) -> None: ...


@dataclass(frozen=True)
class Gen:
    """One generation dir in one store."""
    store: str
    scan: str
    root: str  # `cw-l2/<scan>/index/`
    gen: str
    objects: int
    bytes: int
    newest: float  # epoch seconds of its newest object
    keys: tuple[str, ...]

    @property
    def dir(self) -> str:
        return f"{self.root}{self.gen}"


@dataclass
class Plan:
    doomed: list[Gen]
    young: list[Gen]  # unpointed, but inside the grace period


def norm(d: str) -> str:
    return d.strip().rstrip("/")


def roots(ptrs: Iterable[tuple[str, str, str]], path_variant: str = "path", dates: Iterable[str] | None = None) -> dict[str, str]:
    """`{root: scan}`: the gen roots to sweep — the parent of every
    ``path_variant`` pointer (a secondary store's is `<store>:path`) of
    ``dates`` (default: every scan)."""
    want = set(dates) if dates else None
    out: dict[str, str] = {}
    for date, variant, d in ptrs:
        if variant != path_variant or (want is not None and date not in want):
            continue
        m = GEN_DIR.match(norm(d))
        if m:
            out[m["root"]] = date
    return out


def gens_under(store: GenStore, root: str, scan: str) -> list[Gen]:
    """The generation dirs under ``root`` in ``store``, with their sizes."""
    by: dict[str, list[Blob]] = {}
    for b in store.ls(root):
        rest = b.key[len(root):]
        if "/" not in rest:  # a loose file under the root: not a generation
            continue
        by.setdefault(rest.split("/", 1)[0], []).append(b)
    return [
        Gen(
            store=store.name, scan=scan, root=root, gen=g,
            objects=len(bs), bytes=sum(b.size for b in bs), newest=max(b.mtime for b in bs),
            keys=tuple(sorted(b.key for b in bs)),
        )
        for g, bs in sorted(by.items())
    ]


def plan(
    ptrs: list[tuple[str, str, str]],
    stores: list[GenStore],
    *,
    now: float,
    min_age: float,
    path_variant: str = "path",
    dates: Iterable[str] | None = None,
    workers: int = 8,
) -> Plan:
    """What `sweep` would delete (``doomed``) and what the grace period keeps
    (``young``), ordered by (store, dir)."""
    keep = {norm(d) for _, _, d in ptrs}
    todo = [(s, r, scan) for s in stores for r, scan in sorted(roots(ptrs, path_variant, dates).items())]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        found = [g for gs in ex.map(lambda t: gens_under(*t), todo) for g in gs]
    doomed: list[Gen] = []
    young: list[Gen] = []
    for g in found:
        if g.dir in keep:
            continue
        (young if now - g.newest < min_age else doomed).append(g)
    return Plan(doomed=doomed, young=young)


def fmt_bytes(n: int) -> str:
    return f"{n:,} B ({n / 1e9:.1f} GB)"


def sweep(
    ptrs: list[tuple[str, str, str]],
    stores: list[GenStore],
    *,
    now: float,
    min_age: float,
    dry_run: bool,
    reread: Callable[[], list[tuple[str, str, str]]] | None = None,
    path_variant: str = "path",
    dates: Iterable[str] | None = None,
    workers: int = 8,
    log: Callable[[str], None] | None = None,
) -> Plan:
    """Plan, log each generation and per-store totals, and (unless
    ``dry_run``) delete. ``reread`` re-fetches the pointers just before the
    first delete; a generation pointed by then is kept. Returns the plan of
    what was (or would be) deleted. Idempotent: a re-run finds nothing left.
    ``log`` defaults to stderr."""
    log = log or err
    p = plan(ptrs, stores, now=now, min_age=min_age, path_variant=path_variant, dates=dates, workers=workers)
    if p.doomed and not dry_run and reread is not None:
        keep = {norm(d) for _, _, d in reread()}
        flipped = [g for g in p.doomed if g.dir in keep]
        for g in flipped:
            log(f"index-gc: kept {g.store}/{g.dir}/ — pointed since the listing")
        p.doomed = [g for g in p.doomed if g.dir not in keep]
    verb = "would delete" if dry_run else "deleted"
    by_name = {s.name: s for s in stores}
    for g in p.doomed:
        if not dry_run:
            by_name[g.store].delete(list(g.keys))
        log(f"index-gc: {verb} {g.store}/{g.dir}/ (scan {g.scan}, gen {g.gen}): {g.objects} objects, {fmt_bytes(g.bytes)}")
    for g in p.young:
        log(f"index-gc: kept {g.store}/{g.dir}/ (scan {g.scan}, gen {g.gen}): unpointed but younger than the grace period, {fmt_bytes(g.bytes)}")
    for s in stores:
        mine = [g for g in p.doomed if g.store == s.name]
        young = [g for g in p.young if g.store == s.name]
        log(
            f"index-gc: {s.name}: {verb} {len(mine)} generations in {len({g.scan for g in mine})} scans, "
            f"{sum(g.objects for g in mine)} objects, {fmt_bytes(sum(g.bytes for g in mine))}; "
            f"kept {len(young)} too young"
        )
    return p


class GcsStore:
    def __init__(self, bucket: str):
        from google.cloud import storage

        self.bucket = bucket
        self.name = f"gs://{bucket}"
        self._client = storage.Client()

    def ls(self, prefix: str) -> list[Blob]:
        return [
            Blob(key=b.name, size=int(b.size or 0), mtime=b.updated.timestamp())
            for b in self._client.list_blobs(self.bucket, prefix=prefix)
            if not b.name.endswith("/")
        ]

    def delete(self, keys: list[str]) -> None:
        bucket = self._client.bucket(self.bucket)
        for i in range(0, len(keys), 100):  # a batch request holds ≤ 100 calls
            with self._client.batch(raise_exception=True):
                for k in keys[i : i + 100]:
                    bucket.delete_blob(k)


class R2Store:
    """The R2 serving bucket, reached the way `publish-r2` reaches it
    (`R2_ENDPOINT` + `R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY`)."""

    def __init__(self, bucket: str | None = None):
        from .publish import r2_bucket, r2_client

        self.bucket = bucket or r2_bucket()
        self.name = f"r2://{self.bucket}"
        self._s3 = r2_client()

    def ls(self, prefix: str) -> list[Blob]:
        out: list[Blob] = []
        for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                if not o["Key"].endswith("/"):
                    out.append(Blob(key=o["Key"], size=int(o["Size"]), mtime=o["LastModified"].timestamp()))
        return out

    def delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):  # DeleteObjects takes ≤ 1000 keys
            resp = self._s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]], "Quiet": True})
            if resp.get("Errors"):
                raise RuntimeError(f"{self.name}: delete failed for {len(resp['Errors'])} keys, e.g. {resp['Errors'][0]}")


def open_store(target: str) -> GenStore:
    """`gs://<bucket>` → GCS; `r2` → the R2 serving bucket (`$R2_BUCKET`);
    `r2://<bucket>` → that R2 bucket (same endpoint + creds)."""
    if target.startswith("gs://"):
        return GcsStore(target[len("gs://"):].strip("/"))
    if target == "r2":
        return R2Store()
    if target.startswith("r2://"):
        return R2Store(target[len("r2://"):].strip("/"))
    raise ValueError(f"bad --files target {target!r} (want gs://<bucket>, r2, or r2://<bucket>)")
