"""Mine legacy ``.executor_info`` sidecars (``dt-cloud executor-mine``).

The pre-record-era executor wrote ``.executor_info`` next to outputs with
``name``/``config``/``output_path``/``deps`` — **no** ``built_by``. They still
attribute indirectly: ``name`` joins to W&B run names (→ user), and configs
sometimes embed ``gs://`` paths. This miner does the bounded targeted-GET pass
(paths come from the listing) and emits one parquet row per readable sidecar:
``(path, name, output_path, gs_paths)`` — matching happens downstream.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import fsspec
import pandas as pd

from .wandb_mine import _gs_paths

err = partial(print, file=sys.stderr)

CHECKPOINT_EVERY = 20_000  # rows per incremental part file


def mine_executor_infos(paths: list[str], out_path: Path, max_workers: int = 64) -> pd.DataFrame:
    parts_dir = out_path.parent / (out_path.stem + "-parts")
    parts_dir.mkdir(parents=True, exist_ok=True)
    done_paths: set[str] = set()
    for part in parts_dir.glob("*.parquet"):
        done_paths.update(pd.read_parquet(part, columns=["path"])["path"])
    todo = [p for p in paths if p not in done_paths]
    err(f"{len(paths)} sidecars, {len(done_paths)} already mined, {len(todo)} to fetch")

    def one(path: str) -> dict | None:
        try:
            with fsspec.open(path, "r") as f:
                rec = json.loads(f.read())
        except Exception as e:
            return {"path": path, "name": None, "output_path": None, "gs_paths": None, "error": type(e).__name__}
        if not isinstance(rec, dict):
            return {"path": path, "name": None, "output_path": None, "gs_paths": None, "error": "non-dict"}
        gs = _gs_paths(rec.get("config") or {})
        return {
            "path": path,
            "name": rec.get("name"),
            "output_path": rec.get("output_path"),
            "gs_paths": json.dumps(gs) if gs else None,
            "error": None,
        }

    buf: list[dict] = []
    part_idx = len(list(parts_dir.glob("*.parquet")))
    fetched = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for row in pool.map(one, todo):
            if row is not None:
                buf.append(row)
            fetched += 1
            if len(buf) >= CHECKPOINT_EVERY:
                pd.DataFrame(buf).to_parquet(parts_dir / f"part-{part_idx:05d}.parquet", index=False)
                err(f"checkpoint: {fetched}/{len(todo)} fetched", flush=True)
                part_idx += 1
                buf = []
    if buf:
        pd.DataFrame(buf).to_parquet(parts_dir / f"part-{part_idx:05d}.parquet", index=False)
    parts = sorted(parts_dir.glob("*.parquet"))
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df.to_parquet(out_path, index=False)
    n_err = int(df["error"].notna().sum())
    err(f"wrote {len(df)} rows to {out_path} ({n_err} unreadable)")
    return df
