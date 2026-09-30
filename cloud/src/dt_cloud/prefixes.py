"""Shared attribution-prefix loading + deepest-prefix lookup.

Used by ``report``/``gaps`` (cli) and ``path-index`` (viz). Attribution parquets
store raw users as of build time; loading re-resolves them against the
*current* identities.yaml, and folds in ``prefix_owners`` manual rows (a ``*``
in the bucket position fans out over the listing's buckets), so curation
takes effect without rebuilding any parquet.
"""

from __future__ import annotations

import sys
from fnmatch import fnmatch
from functools import partial
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import duckdb

    from .identity import IdentityMap

err = partial(print, file=sys.stderr)


def load_prefix_map(
    con: "duckdb.DuckDBPyConnection",
    attributions: tuple[str, ...],
    identities: "IdentityMap",
    listing_src: str,
) -> dict[str, tuple]:
    """prefix -> (user, source); parquet rows win over manual rows."""
    by_prefix: dict[str, tuple] = {}
    for attribution in attributions:
        for prefix, user, source in con.execute(
            "SELECT prefix, user, source FROM read_parquet(?)", [attribution]
        ).fetchall():
            user = identities.resolve(user) if user else user
            by_prefix.setdefault(prefix, (user, source))
    buckets = [b for (b,) in con.execute(f"SELECT DISTINCT bucket FROM {listing_src}").fetchall()]
    n_glob = 0
    for owner in identities.prefix_owners:
        bucket, _, rest = owner.prefix.removeprefix("gs://").partition("/")
        expanded = (f"gs://{b}/{rest}" for b in buckets if fnmatch(b, bucket))
        for prefix in expanded if "*" in bucket else (owner.prefix,):
            attr = (owner.user, "manual")
            # A glob in the *path* part (gs://…/grug/swarm_*/) expands against
            # the listing's actual dirs at that depth — so the rule covers dirs
            # that appear later too (expansion reruns on every day's listing),
            # and the exact-prefix map (incl. viz.py's SQL join) needs no change.
            if "*" in prefix.removeprefix("gs://").partition("/")[2]:
                for hit in _expand_path_glob(con, listing_src, prefix):
                    by_prefix.setdefault(hit, attr)
                    n_glob += 1
            else:
                by_prefix.setdefault(prefix, attr)
    err(f"{len(by_prefix)} attribution prefixes loaded ({n_glob} from path-glob rules)")
    return by_prefix


def _expand_path_glob(con: "duckdb.DuckDBPyConnection", listing_src: str, pattern: str) -> list[str]:
    """Exact ``gs://bucket/dir/`` prefixes matching a path-glob rule.

    ``gs://marin-us-central2/grug/swarm_*/`` → every distinct level-2 dir under
    that bucket whose path fnmatches ``grug/swarm_*`` (``*`` here does NOT
    cross ``/`` — each glob segment matches one path segment). Literal leading
    segments become a LIKE pre-filter so the DISTINCT stays cheap."""
    bucket, _, rest = pattern.removeprefix("gs://").partition("/")
    segs = rest.rstrip("/").split("/")
    lead = []
    for s in segs:
        if "*" in s:
            break
        lead.append(s)
    like = "/".join(lead) + "/%" if lead else "%"
    depth = len(segs)
    # End-or-slash after the captured depth: `name` may be an object path
    # (dir + filename) or a bare dir path (viz's `listing_dirs` view) — a
    # depth-exact dir has nothing after its last segment.
    extract = "^(" + "[^/]+" + "(?:/[^/]+)" * (depth - 1) + ")(?:/|$)"
    rows = con.execute(
        f"SELECT DISTINCT regexp_extract(name, ?, 1) AS d FROM {listing_src}"
        " WHERE bucket = ? AND name LIKE ? AND d IS NOT NULL AND d != ''",
        [extract, bucket, like],
    ).fetchall()
    want = "/".join(segs)
    return [f"gs://{bucket}/{d}/" for (d,) in rows if _fnmatch_segs(d, want)]


def _fnmatch_segs(path: str, pattern: str) -> bool:
    """Per-segment fnmatch: ``*`` matches within one path segment only."""
    ps, qs = path.split("/"), pattern.split("/")
    return len(ps) == len(qs) and all(fnmatch(p, q) for p, q in zip(ps, qs))


MAX_DEEPEST_CACHE = 4_000_000


def deepest_lookup(by_prefix: dict[str, tuple]) -> Callable[[str], tuple | None]:
    """Cached ``dir_key -> attribution`` for gs-less 'bucket/a/b' dir keys:
    the attribution of the deepest attributed ancestor, or None.

    The cache (dir keys + their chopped ancestors) is epoch-cleared at
    ``MAX_DEEPEST_CACHE`` entries — unbounded it reaches tens of millions of
    string keys (several GB) on full-fleet listings."""
    cache: dict[str, tuple | None] = {}

    def deepest(dir_key: str) -> tuple | None:
        hit = cache.get(dir_key)
        if hit is not None or dir_key in cache:
            return hit
        probe = dir_key
        chopped = []
        result = None
        while True:
            row = by_prefix.get(f"gs://{probe}/")
            if row is not None:
                result = row
                break
            if "/" not in probe:
                break
            chopped.append(probe)
            probe = probe.rsplit("/", 1)[0]
        if len(cache) > MAX_DEEPEST_CACHE:
            cache.clear()
        for key in chopped:
            cache[key] = result
        cache[dir_key] = result
        return result

    return deepest
