"""`interval_level.rows_bound`: the level rows a plan charges, a true upper bound on what `rows_sql` makes, counted by hand on
a fixture that holds each shape a path's versions take; the cut's plan (`Budget.cut`) charges exactly it."""
from __future__ import annotations

import json
from pathlib import Path
from random import Random

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import interval_append as ia
from dt_cloud import interval_level as ilv
from dt_cloud import interval_store as ist
from dt_cloud import tree_mem as tm
from dt_cloud.static_names import OPEN

SCHEMA = pa.schema([
    ("depth", pa.uint8()), ("path", pa.string()), ("vf", pa.int64()), ("vt", pa.int64()), ("kind", pa.string()),
    ("size", pa.int64()), ("n_files", pa.int64()), ("churn_cum", pa.int64()), ("churn_end", pa.int64()),
])
#: `(path, versions)`, each version `(vf, vt, kind, size)`, and the `rows_sql` rows / the bound's charge, by hand: an opening
#: is 1 row (2 if it, or the version it replaces, is a directory), a closing 2; the bound charges a version 1 (a directory
#: 2), and 2 more if it ended.
SHAPES = [
    ("f", [(10, OPEN, "file", 1)], 1, 1),  # an open file
    ("d", [(10, OPEN, "dir", 1)], 2, 2),  # an open directory
    ("g", [(10, 20, "file", 1)], 1 + 2, 1 + 2),  # a file that went away
    ("h", [(10, 20, "dir", 1)], 2 + 2, 2 + 2),  # a directory that went away
    ("s", [(10, 20, "file", 1), (20, OPEN, "file", 1)], 1, (1 + 2) + 1),  # a split moving nothing: no transition at 20
    ("c", [(10, 20, "file", 1), (20, OPEN, "file", 2)], 1 + 1, (1 + 2) + 1),  # a changed file
    ("x", [(10, 20, "dir", 1), (20, OPEN, "file", 2)], 2 + 2, (2 + 2) + 1),  # a directory replaced by a file: `kind_prev = 'dir'`
    ("y", [(10, 20, "file", 1), (20, OPEN, "dir", 2)], 1 + 2, (1 + 2) + 2),  # a file replaced by a directory
    ("z", [(10, 20, "file", 1), (30, OPEN, "file", 1)], (1 + 2) + 1, (1 + 2) + 1),  # gone, then back
]
ROWS = sum(s[2] for s in SHAPES)
BOUND = sum(s[3] for s in SHAPES)
#: The root: no `level` row of its own, and none charged.
ROOT = {"depth": 0, "path": "", "vf": 10, "vt": OPEN, "kind": "dir", "size": 1, "n_files": 1, "churn_cum": 1, "churn_end": 1}


def _table(versions: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(sorted(versions, key=lambda v: (v["depth"], v["path"], v["vf"])), schema=SCHEMA)


def _shapes() -> list[dict]:
    return [ROOT, *({"depth": 1, "path": p, "vf": vf, "vt": vt, "kind": k, "size": z, "n_files": 1, "churn_cum": z, "churn_end": z}
                    for p, versions, _, _ in SHAPES for vf, vt, k, z in versions)]


def _write(d: Path, t: pa.Table) -> str:
    d.mkdir(parents=True, exist_ok=True)
    f = d / "r0000.parquet"
    pq.write_table(t, f)
    return str(f)


def _made(f: str, opened: str = "true") -> int:
    src = f"read_parquet('{f}')"
    return duckdb.connect().execute(f"SELECT count(*) FROM ({ilv.rows_sql(src, opened)})").fetchone()[0]


def test_the_bound_is_the_hand_count(tmp_path):
    f = _write(tmp_path, _table(_shapes()))
    con = duckdb.connect()
    assert (_made(f), ilv.rows_bound(con, [f]), ilv.extra_rows(con, [f])) == (ROWS, BOUND, BOUND - pq.read_metadata(f).num_rows)
    assert (ROWS, BOUND) == (24, 32)


def test_the_bound_is_met_exactly_without_replacements(tmp_path):
    """Versions that each open and go away with nothing continuing them: every charge is a real row."""
    keep = [v for v in _shapes() if v["path"] in ("f", "d", "g", "h", "z")]
    f = _write(tmp_path, _table(keep))
    assert (_made(f), ilv.rows_bound(duckdb.connect(), [f])) == (1 + 2 + 3 + 4 + 4, 1 + 2 + 3 + 4 + 4)


def test_no_files_no_rows():
    assert (ilv.rows_bound(duckdb.connect(), []), ilv.extra_rows(duckdb.connect(), [])) == (0, 0)


@pytest.mark.parametrize("seed", range(6))
def test_the_bound_holds_on_any_version_chain(tmp_path, seed):
    """Random chains of a path's versions (gaps, splits, kinds flipping, directories replacing files and back): the rows made
    never pass the bound, whichever versions opened in the file."""
    r = Random(seed)
    rows = []
    for i in range(300):
        t = r.randrange(1, 5)
        for _ in range(r.randrange(1, 7)):
            end = t + r.randrange(1, 4)
            gone = r.random() < 0.2
            rows.append({"depth": 3, "path": f"a/b/p{i}", "vf": t, "vt": end, "kind": r.choice(["dir", "file"]),
                         "size": r.randrange(3), "n_files": 1, "churn_cum": 0, "churn_end": 0})
            t = end + (r.randrange(1, 3) if gone else 0)
        rows[-1]["vt"] = r.choice([rows[-1]["vt"], OPEN])
    f = _write(tmp_path, _table(rows))
    bound = ilv.rows_bound(duckdb.connect(), [f])
    made = _made(f)
    assert (made <= bound, _made(f, "vf % 2 = 0") <= bound) == (True, True), f"made {made}, bound {bound}"


@pytest.fixture
def charged(monkeypatch):
    """The `sort_rows` each `Budget.cut` was asked to charge."""
    got: list[int] = []
    real = tm.Budget.cut

    def cut(self, *a, **kw):
        got.append(kw["sort_rows"])
        return real(self, *a, **kw)

    monkeypatch.setattr(tm.Budget, "cut", cut)
    return got


TREE = tm.Budget(total=1 << 30, base=100 * tm.MIB, proc_base=50 * tm.MIB)


def test_a_generations_cut_charges_the_bound(tmp_path, charged):
    gen = tmp_path / "g"
    _write(gen / "pvl", _table(_shapes()))
    (gen / "ranges.json").write_text(json.dumps({"ranges": [{"lo": [0, ""]}]}))
    scans = {"scans": [{"ts": 10}], "slices": False, "churn": True, ilv.LEVEL_FLAG: True}
    con = duckdb.connect()
    sorts, _ = ist.cut_gen(con, gen, scans, rg_rows=8, tree=TREE, sorts=[ilv.LEVEL], served=tmp_path / "out")
    assert (charged, sorts[ilv.LEVEL]["rows"]) == ([BOUND], ROWS)


def test_a_runs_cut_charges_the_bound(tmp_path, charged):
    """A run's deltas: its opened versions and close records, each a row of the same bound."""
    t = _table(_shapes()).append_column("op", pa.array([1] * (len(_shapes())), pa.int8()))
    root = tmp_path / "run"
    _write(root / "pvl", t)
    doc = ia.cut_level(duckdb.connect(), str(root), tmp_path / "out", rg_rows=8, tree=TREE)
    assert (charged, doc["rows"]) == ([BOUND], ROWS)


def _paths(f: Path, n: int, width: int) -> str:
    """`n` rows whose `path` is `width` bytes (plain, uncompressed), in a file of `SCHEMA`'s columns."""
    pq.write_table(pa.Table.from_pylist([{**ROOT, "path": "p" * width}] * n, schema=SCHEMA), f, use_dictionary=False, compression="none")
    return str(f)


def test_writer_meta_is_the_sorts_columns_and_the_files_mean_width(tmp_path):
    """What a sort's writers keep over all its groups: its own columns, not its sources', and the string statistics' bytes a
    value over every source row: a small file of long paths is a few of its groups, not all of them."""
    long = _paths(tmp_path / "long.parquet", 100, 1000)
    short = _paths(tmp_path / "short.parquet", 9900, 10)
    size = sum(pq.read_metadata(f).row_group(0).column(1).total_uncompressed_size for f in (long, short))
    stat = -(-size // 10_000)
    assert ist.writer_meta([long, short], 8192, 256, cols=13) == tm.group_meta(13, 32, stat)
    assert ist.writer_meta([long, short], 8192, 256) == tm.group_meta(len(SCHEMA), 32, stat)


def test_the_level_plan_charges_the_level_sorts_columns(tmp_path, monkeypatch):
    got: list[int] = []
    real = tm.Budget.cut

    def cut(self, *a, **kw):
        got.append(kw["meta"])
        return real(self, *a, **kw)

    monkeypatch.setattr(tm.Budget, "cut", cut)
    root = tmp_path / "run"
    f = _write(root / "pvl", _table(_shapes()).append_column("op", pa.array([1] * len(_shapes()), pa.int8())))
    ia.cut_level(duckdb.connect(), str(root), tmp_path / "out", rg_rows=8192, tree=TREE)
    assert got == [ist.writer_meta([f], 8192, cols=len(ilv.LEVEL_SCHEMA))]


@pytest.mark.parametrize("split", [False, True])
def test_a_key_shared_by_two_rows_has_one_order(tmp_path, split):
    """An object named `a/` is a child of `a` named '', as `a`'s own row is, at the same stamp: the same `LEVEL_KEY` twice.
    `a`'s own first, whichever the other's values, so the bytes don't depend on how the cut was split (one run, or pieces)."""
    v = lambda d, p, k, z: {"depth": d, "path": p, "vf": 10, "vt": OPEN, "kind": k, "size": z, "n_files": 1, "churn_cum": z, "churn_end": z}  # noqa: E731
    low, high = [v(1, "a", "dir", 5)], [v(2, "a/", "file", 3)]
    gen = tmp_path / "g"
    (gen / "pvl").mkdir(parents=True)
    if split:
        pq.write_table(_table(low), gen / "pvl" / "r0000.parquet")
        pq.write_table(_table(high), gen / "pvl" / "r0001.parquet")
    else:
        pq.write_table(_table([*low, *high]), gen / "pvl" / "r0000.parquet")
    (gen / "ranges.json").write_text(json.dumps({"ranges": [{"lo": [0, ""]}, *([{"lo": [2, ""]}] if split else [])]}))
    scans = {"scans": [{"ts": 10}], "slices": False, "churn": True, ilv.LEVEL_FLAG: True}
    ist.cut_gen(duckdb.connect(), gen, scans, rg_rows=8, sorts=[ilv.LEVEL], served=tmp_path / "out")
    got = pq.read_table(tmp_path / "out" / f"{ilv.LEVEL}.parquet").to_pylist()
    assert [(r["depth"], r["path"], r["ts"], r["name"], r["kind"], r["size"]) for r in got] == [
        (0, "", 10, "a", "dir", 5),
        (1, "a", 10, "", "dir", 5),
        (1, "a", 10, "", "file", 3),
    ]


def test_the_arm_is_only_the_sorts():
    """`rows_sql`'s rows are `LEVEL_SCHEMA`'s, unless the sort asks for what orders them."""
    src = "(SELECT 1::UTINYINT AS depth, 'a' AS path, 10::BIGINT AS vf, 20::BIGINT AS vt, 'dir' AS kind, 1::BIGINT AS size, 1::BIGINT AS n_files, 1::BIGINT AS churn_cum, 1::BIGINT AS churn_end)"
    cols = lambda **kw: [d[0] for d in duckdb.connect().execute(f"SELECT * FROM ({ilv.rows_sql(src, **kw)})").description]  # noqa: E731
    assert (cols(), cols(arm=True)) == (ilv.LEVEL_SCHEMA.names, [*ilv.LEVEL_SCHEMA.names, ilv.LEVEL_ARM])
