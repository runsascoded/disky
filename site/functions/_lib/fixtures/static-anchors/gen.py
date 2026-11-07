"""The anchored search fixture (`staticAnchors.test.ts`): a base generation (four scans) and two per-scan runs, built by
`dt_cloud.static_anchors` itself (`cloud/tests/test_static_anchors.py` `build_world`: R = 3, K = 2, 4-row name-index
groups, 5-row suffix groups), laid out as on R2 — `scans.json`, `shards.json`, `sx/`, `sidecar.parquet`, `catalog/`,
`names/`, `anchors/`, `deltas/<scan>/{sx,names,anchors,catalog}/…`, `manifests/<last scan>.json` (both runs) — plus
`expected.json`: `dates`, `paths` (`''`, every directory and path, `nope`), `keys` (`termKey`s), and `views[key][P][d]`, brute
force from the versions: per child of `P` holding a live match root under it `[bytes, objects]` (non-empty only; a
view `P` itself matches is the plain one and left out); `start_keys` (every `^q` prefix of every name) and
`start_root[key][d]`, their fleet roots (the starts-with catalog's).

Regenerate from the repo root: `PYTHONPATH=cloud/tests .venv/bin/python site/functions/_lib/fixtures/static-anchors/gen.py`
"""
import json
import shutil
import tempfile
from pathlib import Path

import pyarrow.parquet as pq

from dt_cloud import static_anchors as an
from dt_cloud import static_names as sn
from test_static_anchors import BASE, IDS, KEYS, V, _dirs, _prefixes, build_world

HERE = Path(__file__).parent


def untimed(doc):
    if isinstance(doc, dict):
        return {k: untimed(v) for k, v in doc.items() if k not in ("s", "ms")}
    return doc


def copy_tree(src: Path, dst: Path, skip: tuple[str, ...] = ()) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*skip), dirs_exist_ok=True)
    for m in dst.rglob("meta.json"):
        m.write_text(json.dumps(untimed(json.loads(m.read_text())), indent=1) + "\n")


def catalog_meta(d: Path) -> None:
    md = pq.ParquetFile(d / "cells.parquet").metadata
    meta = {"gen": "fixture", "cells_rows": md.num_rows, "row_groups": md.num_row_groups, "bytes": (d / "cells.parquet").stat().st_size,
            "cell_rg": md.row_group(0).num_rows if md.num_row_groups else 0, "membership": {"max_rows": V}}
    (d / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")


TIER = ("sx", "names", "anchors", "shards.json", "sidecar.parquet")


def main() -> None:
    out = HERE
    for p in list(out.iterdir()):
        if p.name != "gen.py":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    with tempfile.TemporaryDirectory() as tmp:
        w = build_world(Path(tmp))
        for name in TIER:
            src = w["base"] / name
            copy_tree(src, out / name) if src.is_dir() else shutil.copy(src, out / name)
        copy_tree(w["catalog"], out / "catalog")
        catalog_meta(out / "catalog")
        runs = []
        for scan, run in zip(IDS[BASE:], w["runs"]):
            key = f"deltas/{scan}"
            for name in (*TIER, "catalog"):
                src = run / name
                copy_tree(src, out / key / name) if src.is_dir() else shutil.copy(src, out / key / name)
            runs.append({"key": key, "first": scan, "last": scan, "level": 0, "scans": [scan]})
        (out / "scans.json").write_text(json.dumps({"bucket": "b", "scans": [{"id": d} for d in IDS[:BASE]]}, indent=1) + "\n")
        (out / "manifests").mkdir()
        for k in range(1, len(runs) + 1):
            last = runs[k - 1]["last"]
            (out / "manifests" / f"{last}.json").write_text(json.dumps(
                {"gen": "fixture", "date": last, "base_scans": BASE, "scans": IDS[:BASE + k], "runs": runs[:k]}, indent=1) + "\n")
        paths = _dirs(w["paths"])
        views = {}
        for key in KEYS:
            views[key] = {}
            for P in paths:
                if an.term_in_path(key, P):
                    continue
                v = {d: x for d in IDS for x in [an.brute_view(w["versions"], key, P, sn.scan_epoch(d) * 1000)] if x}
                if v:
                    views[key][P] = v
        # The starts-with catalog's fleet root: every `^q` prefix of every name (`start_keys`), per scan.
        start_keys = _prefixes(w["paths"])
        start_root = {}
        for key in start_keys:
            v = {d: x for d in IDS for x in [an.brute_view(w["versions"], key, "", sn.scan_epoch(d) * 1000)] if x}
            if v:
                start_root[key] = v
        (out / "expected.json").write_text(json.dumps({"dates": IDS, "base": IDS[:BASE], "paths": paths, "keys": KEYS, "R": 3, "K": 2,
                                                       "views": views, "start_keys": start_keys, "start_root": start_root},
                                                      separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
