"""Index extras: a provenance sidecar beside a scan's index tiers, written by
``path-index`` for a fresh scan and by ``dt-cloud index-extras`` as the backfill
for archived generations.

- ``attribution.parquet``: every attributing prefix as a row ``(depth, path,
                user, source, evidence)``, sorted by path, NULL where a source or
                evidence is missing: the interval store's input for its
                ``attribution`` table (specs/attr-interval-table.md), appended per
                scan, so the history isn't one full copy per scan.
- ``attr.tsv``: the same map as ``user  source  evidence`` lines — the site's
                per-scan sidecar for scans the store's table doesn't cover; it
                stops being written once the table-reading site is on prod.

``attr.tsv`` is **sorted by key, one line per entry, with a block index**
(``<name>.idx.json``: the first key and byte offset of every ~64 KB block), so
the site fetches just the byte range covering a view's subtree, far past what a
worker isolate should parse whole.

Keys are index paths: ``<bucket>/<dir>/…`` (no ``gs://``, no trailing slash).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
from utz import err

if TYPE_CHECKING:
    import pandas as pd

ATTR_FILE = "attr.tsv"
ATTRIBUTION_FILE = "attribution.parquet"
BLOCK = 64 * 1024
#: `attribution.parquet`'s rows: the attributing prefix as an index path and its path-store depth (a bucket is 1), and
#: the attribution's descriptive columns (`user` NULL: a rule attributing the prefix to nobody). Sorted by `path`.
ATTRIBUTION_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("user", pa.string()),
    pa.field("source", pa.string()),
    pa.field("evidence", pa.string()),
])


def write_blocked(path: Path, lines: list[str], key_of=lambda line: line) -> dict[str, int]:
    """Write ``lines`` (already sorted by key) as one text file plus a block
    index ``<path>.idx.json`` = ``{"v":1, "n": lines, "size": bytes,
    "keys": [first key per block], "offsets": [byte offset per block]}``.
    Blocks are cut at line boundaries at or past every ``BLOCK`` bytes."""
    keys: list[str] = []
    offsets: list[int] = []
    off = 0
    next_cut = 0
    with path.open("wb") as f:
        for line in lines:
            if off >= next_cut:
                keys.append(key_of(line))
                offsets.append(off)
                next_cut = off + BLOCK
            b = (line + "\n").encode()
            f.write(b)
            off += len(b)
    idx = {"v": 1, "n": len(lines), "size": off, "keys": keys, "offsets": offsets}
    path.with_name(path.name + ".idx.json").write_text(json.dumps(idx, separators=(",", ":")) + "\n")
    return {"lines": len(lines), "bytes": off, "blocks": len(keys)}


def attr_map(pfx_df: "pd.DataFrame") -> dict[str, list]:
    """``key → [user, source, evidence]`` from the prefix-label frame
    (``prefix_labels``; ``evidence`` when the frame carries it)."""
    has_ev = "evidence" in pfx_df.columns
    out: dict[str, list] = {}
    for row in pfx_df.itertuples(index=False):
        ev = getattr(row, "evidence", None) if has_ev else None
        out[row.key] = [row.user, getattr(row, "source", None), None if ev is None or ev != ev else ev]
    return out


def depth_of(path: str) -> int:
    """An index path's path-store depth: a bucket is 1, each `/` one more."""
    return path.count("/") + 1


def attribution_table(attr: dict[str, list]) -> pa.Table:
    """`attr_map`'s `key → [user, source, evidence]` as `ATTRIBUTION_SCHEMA` rows sorted by path; a missing value (None,
    pandas' NaN, or empty: what `attr.tsv`'s empty field reads as) is NULL — a NULL `user` is a rule attributing the
    prefix to nobody."""
    keys = sorted(attr)

    def val(v):
        return v if isinstance(v, str) and v else None

    return pa.table({
        "depth": [depth_of(k) for k in keys],
        "path": keys,
        "user": [val(attr[k][0]) for k in keys],
        "source": [val(attr[k][1]) for k in keys],
        "evidence": [val(attr[k][2]) for k in keys],
    }, schema=ATTRIBUTION_SCHEMA)


def write_attribution(t: pa.Table, path: Path) -> int:
    """`t` (`ATTRIBUTION_SCHEMA`) as `attribution.parquet` at `path` (zstd); returns its rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, path, compression="zstd")
    return t.num_rows


def attribution_from_tsv(text: str) -> pa.Table:
    """An `attr.tsv`'s lines (`key  user  source  evidence`, sorted by key) as `ATTRIBUTION_SCHEMA` rows: what
    `attribution-convert` writes beside a kept scan's sidecar, the store's backfill input for scans from before
    `attribution.parquet` (specs/attr-interval-table.md §4). A nobody rule's user, which the sidecar printed as `None`
    (or `nan`), is NULL. Refuses a line without a key, and keys out of order or repeated."""
    attr: dict[str, list] = {}
    prev = None
    for n, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        k, u, src, ev = (line.split("\t") + ["", "", ""])[:4]
        if not k:
            raise ValueError(f"line {n}: no key: {line!r}")
        # what the sidecar printed for a missing value: `None` (a nobody rule's user), pandas' `nan`
        u, src, ev = ("" if v in ("None", "nan") else v for v in (u, src, ev))
        if prev is not None and k <= prev:
            raise ValueError(f"line {n}: key {k!r} is not after {prev!r}")
        attr[k] = [u, src, ev]
        prev = k
    return attribution_table(attr)


def write_extras(pfx_df: "pd.DataFrame | None", out_dir: Path) -> dict[str, int]:
    """Given a prefix frame, write ``attribution.parquet`` and ``attr.tsv`` (+ index) into ``out_dir``; returns entry
    counts. No frame → no sidecar (the reader treats an absent sidecar as "no extras for this generation", the store's
    append as a scan its table doesn't cover)."""
    counts: dict[str, int] = {}
    if pfx_df is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        attr = attr_map(pfx_df)
        counts["attribution"] = write_attribution(attribution_table(attr), out_dir / ATTRIBUTION_FILE)
        rows = [f"{k}\t{u}\t{src or ''}\t{ev or ''}" for k, (u, src, ev) in sorted(attr.items())]
        write_blocked(out_dir / ATTR_FILE, rows, key_of=lambda line: line.split("\t", 1)[0])
        counts["attr"] = len(attr)
    err(f"extras: {out_dir}: " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))
    return counts
