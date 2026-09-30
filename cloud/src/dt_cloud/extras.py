"""Index extras: a provenance sidecar beside a scan's index tiers, written by
``path-index`` for a fresh scan and by ``dt-cloud index-extras`` as the backfill
for archived generations.

- ``attr.tsv``: every attributing prefix → ``user  source  evidence`` —
                the provenance of inferred ownership, which the path index
                drops on the way to its ``usr`` column.

It is **sorted by key, one line per entry, with a block index**
(``<name>.idx.json``: the first key and byte offset of every ~64 KB block), so
the site fetches just the byte range covering a view's subtree, far past what a
worker isolate should parse whole.

Keys are index paths: ``<bucket>/<dir>/…`` (no ``gs://``, no trailing slash).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from utz import err

if TYPE_CHECKING:
    import pandas as pd

ATTR_FILE = "attr.tsv"
BLOCK = 64 * 1024


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


def write_extras(pfx_df: "pd.DataFrame | None", out_dir: Path) -> dict[str, int]:
    """Given a prefix frame, write ``attr.tsv`` (+ index) into ``out_dir``;
    returns entry counts. No frame → no sidecar (the reader treats an absent
    sidecar as "no extras for this generation")."""
    counts: dict[str, int] = {}
    if pfx_df is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        attr = attr_map(pfx_df)
        rows = [f"{k}\t{u}\t{src or ''}\t{ev or ''}" for k, (u, src, ev) in sorted(attr.items())]
        write_blocked(out_dir / ATTR_FILE, rows, key_of=lambda line: line.split("\t", 1)[0])
        counts["attr"] = len(attr)
    err(f"extras: {out_dir}: " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))
    return counts
