"""`dups.parquet` + `dups.json` for `functions/api/dups.test.ts`, cut by `dt_cloud.dups.find_dups` over a small
synthetic path index (3-row row groups, so a read spans several and a group can straddle two):

- `east|west|eu/ds/web` (4 GiB, 2 objects): a `tree` group of 3; `west`'s copy read last (the keep)
- `east/tok/en|fr/data` (5 GiB, 4 objects): a same-bucket `moved` group
- `east|west/p` (3 GiB, 3 objects) holding `q/` (2 GiB), and `eu/q`: a `tree` group, and a `moved` group whose
  `east`/`west` copies are `nested`
- `east/run0..6/part-0.parquet` (2 GiB each): a `suspect` `moved` group of 7
- `east|west/m/model.bin` (1.5 GiB): a `tree` group of objects

The spot check (a stub lister): the `ds` copies equal, `eu/q` differs from the keep copy.

Run from the repo root with the worktree's packages: `python site/functions/_lib/fixtures/dups/gen.py`.
"""
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from dt_cloud.dups import Listing, find_dups

G = 1 << 30
DAY = 86400
HERE = Path(__file__).parent


def node(path: str, size: int, n_files: int = 1, kind: str = "file", created: int = 0, last_read: int | None = None,
         cold: int = 0) -> dict:
    return {"path": path, "usr": "u", "size": size, "depth": path.count("/") + 1, "kind": kind, "n_files": n_files,
            "mtime": created + DAY, "created": created, "last_read": last_read, "sum_storage_class_id_3": cold}


def rows() -> list[dict]:
    t0 = 1_760_000_000
    out = [node(b, 0, kind="dir") for b in ("east", "eu", "west")]
    out += [node("east/ds/web", 4 * G, 2, "dir", t0, 20_300, cold=G), node("west/ds/web", 4 * G, 2, "dir", t0 + DAY, 20_370),
            node("eu/ds/web", 4 * G, 2, "dir", t0 - DAY)]
    out += [node("east/tok/en/data", 5 * G, 4, "dir", t0), node("east/tok/fr/data", 5 * G, 4, "dir", t0 + DAY)]
    out += [node(f"{b}/p", 3 * G, 3, "dir", t0) for b in ("east", "west")]
    out += [node(f"{b}/p/q", 2 * G, 2, "dir", t0) for b in ("east", "west")] + [node("eu/q", 2 * G, 2, "dir", t0)]
    out += [node(f"east/run{i}/part-0.parquet", 2 * G, created=t0 + i) for i in range(7)]
    out += [node(f"{b}/m/model.bin", 3 * G // 2, created=t0) for b in ("east", "west")]
    return out


SCHEMA = pa.schema([("path", pa.string()), ("usr", pa.string()), ("size", pa.int64()), ("depth", pa.int32()),
                    ("kind", pa.string()), ("n_files", pa.int64()), ("mtime", pa.int64()), ("created", pa.int64()),
                    ("last_read", pa.int32()), ("sum_storage_class_id_3", pa.int64())])

SAME = Listing({"a": (1, "x"), "b": (1, "y")}, True)


def lister(path: str, kind: str) -> Listing:
    return Listing({"a": (1, "x"), "b": (1, "z")}, True) if path == "eu/q" else SAME


def main() -> None:
    with TemporaryDirectory() as d:
        src = Path(d) / "path-index.parquet"
        pq.write_table(pa.Table.from_pylist(rows(), schema=SCHEMA), src)
        find_dups(str(src), HERE, check=10, lister=lister, workers=1, scan="2026-10-18T0701")
    # DuckDB's row groups are at least a vector (2048 rows): re-cut at 3 rows
    out = HERE / "dups.parquet"
    pq.write_table(pq.read_table(out), out, row_group_size=3, compression="zstd")


if __name__ == "__main__":
    main()
