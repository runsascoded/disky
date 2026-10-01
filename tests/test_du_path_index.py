"""`disk-tree du -p`: the same top-N-per-level view, read from the site's path
index (`dt-cloud path-index`'s `path-index.parquet`: absolute paths sans the
leading `/`, `depth` = segment count, sorted `(depth, path)`) instead of a
scan blob — the newest generation under a `<date>/index/<gen>/` root."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from disk_tree.cli.base import cli
from disk_tree.path_index import latest_path_index

ROWS = [
    # path, kind, size, n_desc
    ("Applications", "dir", 30, 1),
    ("Users", "dir", 700, 6),
    ("Applications/X.app", "file", 30, 0),
    ("Users/ryan", "dir", 700, 5),
    ("Users/ryan/c", "dir", 600, 3),
    ("Users/ryan/big.iso", "file", 80, 0),
    ("Users/ryan/notes", "dir", 20, 1),
    ("Users/ryan/c/disky", "dir", 500, 1),
    ("Users/ryan/c/old", "dir", 100, 0),
    ("Users/ryan/notes/a.txt", "file", 20, 0),
    ("Users/ryan/c/disky/x.bin", "file", 500, 0),
]


def _write(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r[0].count("/") + 1, r[0]))
    pq.write_table(pa.table({
        "path": [r[0] for r in rows],
        "usr": pa.array([None] * len(rows), pa.string()),
        "size": [r[2] for r in rows],
        "depth": pa.array([r[0].count("/") + 1 for r in rows], pa.int32()),
        "kind": [r[1] for r in rows],
        "n_files": [1] * len(rows),
        "n_children": [0] * len(rows),
        "n_desc": [r[3] for r in rows],
        "mtime": [1_790_000_000] * len(rows),
    }), path, row_group_size=4)


def test_latest_path_index_picks_the_newest_date_then_generation(tmp_path: Path):
    for date, gen in [("2026-09-30", "202609302223"), ("2026-10-01", "202610010040"), ("2026-10-01", "202610011155")]:
        _write(tmp_path / date / "index" / gen / "path-index.parquet", ROWS)
    (tmp_path / "2026-10-02" / "index").mkdir(parents=True)  # a date with no generation yet
    assert latest_path_index(str(tmp_path)) == str(tmp_path / "2026-10-01" / "index" / "202610011155" / "path-index.parquet")


def test_du_reads_a_subtree_from_the_path_index(tmp_path: Path):
    _write(tmp_path / "2026-10-01" / "index" / "202610011155" / "path-index.parquet", ROWS)
    res = CliRunner().invoke(cli, ["du", "-p", str(tmp_path), "-d", "2", "-j", "/Users/ryan"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    tree = lambda rows: [(r["path"], r["size"], tree(r["children"])) for r in rows]  # noqa: E731
    assert (out["uri"], out["size"], out["source"]) == ("/Users/ryan", 700, "2026-10-01/index/202610011155")
    assert tree(out["rows"]) == [
        ("c", 600, [("c/disky", 500, []), ("c/old", 100, [])]),
        ("notes", 20, []),
    ]


def test_du_files_too_and_the_whole_tree(tmp_path: Path):
    _write(tmp_path / "2026-10-01" / "index" / "202610011155" / "path-index.parquet", ROWS)
    res = CliRunner().invoke(cli, ["du", "-p", str(tmp_path), "-a", "-j", "/"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert (out["size"], [(r["path"], r["size"], r["kind"]) for r in out["rows"]]) == (730, [("Users", 700, "dir"), ("Applications", 30, "dir")])
    res = CliRunner().invoke(cli, ["du", "-p", str(tmp_path), "-a", "-j", "/Users/ryan"])
    assert [(r["path"], r["kind"]) for r in json.loads(res.output)["rows"]] == [("c", "dir"), ("big.iso", "file"), ("notes", "dir")]
