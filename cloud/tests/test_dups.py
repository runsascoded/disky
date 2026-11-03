"""`dt-cloud dups` (specs/dups.md §1): groups, tiers, maximality, `nested`, `suspect`, `keep`, the spot check, and
the summary, over small synthetic path indexes."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from dt_cloud.cli import main
from dt_cloud.dups import Listing, compare, find_dups

G = 1 << 30
DAY = 86400


def node(path: str, size: int, n_files: int = 1, kind: str = "file", usr: str = "u", created: int = 0, last_read: int | None = None,
         nl: int = 0) -> dict:
    return {"path": path, "usr": usr, "size": size, "depth": path.count("/") + 1, "kind": kind, "n_files": n_files,
            "mtime": created + 10, "created": created, "last_read": last_read, "sum_storage_class_id_2": nl}


def tree(*rows: dict) -> list[dict]:
    """`rows` plus a bucket-root row per bucket (depth 1: never a copy)."""
    buckets = sorted({r["path"].split("/")[0] for r in rows})
    return [node(b, 0, kind="dir") for b in buckets] + list(rows)


SCHEMA = pa.schema([("path", pa.string()), ("usr", pa.string()), ("size", pa.int64()), ("depth", pa.int32()),
                    ("kind", pa.string()), ("n_files", pa.int64()), ("mtime", pa.int64()), ("created", pa.int64()),
                    ("last_read", pa.int32()), ("sum_storage_class_id_2", pa.int64())])


def write(tmp: Path, rows: list[dict], tot: bool = False) -> str:
    t = pa.Table.from_pylist(rows, schema=SCHEMA)
    if tot:
        totals: dict[str, int] = {}
        for r in rows:
            totals[r["path"]] = totals.get(r["path"], 0) + r["size"]
        t = t.append_column("tot", pa.array([totals[r["path"]] for r in rows], pa.int64()))
    p = tmp / "path-index.parquet"
    pq.write_table(t, p)
    return str(p)


COLS = ["tier", "path", "size", "n_files", "copies", "reclaimable", "n_buckets", "nested", "suspect", "keep", "check"]


def table(out: Path) -> list[tuple]:
    t = pq.read_table(out / "dups.parquet").to_pylist()
    return [tuple(r[c] for c in COLS) for r in t]


def groups(out: Path) -> list[list[str]]:
    """Each group's copy paths, in file order (groups contiguous)."""
    seen: dict[str, list[str]] = {}
    for r in pq.read_table(out / "dups.parquet").to_pylist():
        seen.setdefault(r["group"], []).append(r["path"])
    return list(seen.values())


def fleet() -> list[dict]:
    """Two buckets holding the same dataset (`ds/`, two shards) under the same name, a third with it renamed, plus a
    lone object and an under-floor copy."""
    return tree(
        node("a/ds", 4 * G, 2, "dir", created=100, last_read=20),
        node("a/ds/s0", 2 * G, created=100), node("a/ds/s1", 2 * G, created=100),
        node("b/ds", 4 * G, 2, "dir", created=50, last_read=20),
        node("b/ds/s0", 2 * G, created=50), node("b/ds/s1", 2 * G, created=50),
        node("c/old", 4 * G, 1, "dir"), node("c/old/ds", 4 * G, 2, "dir", created=10),
        node("c/old/ds/s0", 2 * G, created=10), node("c/old/ds/s1", 2 * G, created=10),
        node("a/lone", 3 * G), node("a/small", G // 2), node("b/small", G // 2),
    )


def test_tree_and_moved_groups_maximal(tmp_path):
    src = write(tmp_path, fleet())
    out = tmp_path / "out"
    s = find_dups(src, out)
    # `ds` in a and b: one tree group (its shards' groups are dropped: their parents are the group's copies). The
    # moved group of all three `ds` dirs spans the tree group; `c/old` matches nothing (no other 4 GiB, 1-object dir).
    # The shards' moved groups (s0 in three dirs) drop too: their parents are the moved group's copies.
    assert table(out) == [
        ("moved", "a/ds", 4 * G, 2, 3, 8 * G, 3, False, False, False, None),
        ("moved", "b/ds", 4 * G, 2, 3, 8 * G, 3, False, False, True, None),
        ("moved", "c/old/ds", 4 * G, 2, 3, 8 * G, 3, False, False, False, None),
        ("tree", "a/ds", 4 * G, 2, 2, 4 * G, 2, False, False, False, None),
        ("tree", "b/ds", 4 * G, 2, 2, 4 * G, 2, False, False, True, None),
    ]
    assert {k: s[k] for k in ("v", "floor", "suspect_copies", "classes", "checked_groups", "groups", "rows")} == {
        "v": 2, "floor": G, "suspect_copies": 7, "classes": ["sum_storage_class_id_2"], "checked_groups": 0, "groups": 2, "rows": 5,
    }
    assert (s["verified_copies"], s["exact"]) == (0, [])
    assert s["net_by_tier"] == [
        {"tier": "moved", "groups": 1, "copies": 3, "bytes": 12 * G, "reclaimable": 8 * G},
        {"tier": "tree", "groups": 1, "copies": 2, "bytes": 8 * G, "reclaimable": 4 * G},
    ]
    assert s["by_pair"] == [
        {"tier": "moved", "b1": "a", "b2": "b", "groups": 1, "bytes": 4 * G},
        {"tier": "moved", "b1": "a", "b2": "c", "groups": 1, "bytes": 4 * G},
        {"tier": "moved", "b1": "b", "b2": "c", "groups": 1, "bytes": 4 * G},
        {"tier": "tree", "b1": "a", "b2": "b", "groups": 1, "bytes": 4 * G},
    ]
    assert s["suspect"] == []
    assert s["copies_hist"] == [{"tier": "moved", "copies": 3, "groups": 1}, {"tier": "tree", "copies": 2, "groups": 1}]


def test_keep_is_the_latest_read_then_the_oldest(tmp_path):
    rows = tree(node("a/x", 2 * G, created=300, last_read=5), node("b/x", 2 * G, created=100, last_read=9),
                node("c/x", 2 * G, created=50, last_read=9), node("a/y", 2 * G, n_files=3, kind="dir", created=7),
                node("b/y", 2 * G, n_files=3, kind="dir", created=3))
    out = tmp_path / "out"
    find_dups(write(tmp_path, rows), out)
    keeps = [(r["path"], r["keep"]) for r in pq.read_table(out / "dups.parquet").to_pylist()]
    assert keeps == [("a/x", False), ("b/x", False), ("c/x", True), ("a/y", False), ("b/y", True)]


def test_slices_sum_before_the_floor(tmp_path):
    """A path's size is its slices' sum: two owners' 0.6 GiB each make a 1.2 GiB copy (a per-slice floor would drop
    it); the same with a `tot` column (the size sort's)."""
    half = 6 * G // 10
    rows = tree(node("a/x", half, usr="p"), node("a/x", half, usr="q"), node("b/x", 2 * half, n_files=2, usr="p"))
    for tot in (False, True):
        d = tmp_path / str(tot)
        d.mkdir()
        out = d / "out"
        find_dups(write(d, rows, tot=tot), out)
        assert table(out) == [
            ("tree", "a/x", 2 * half, 2, 2, 2 * half, 2, False, False, True, None),
            ("tree", "b/x", 2 * half, 2, 2, 2 * half, 2, False, False, False, None),
        ]


def test_nested_copies_and_same_bucket_moves(tmp_path):
    """A child dir with a third copy elsewhere is its own group (a different copy set) whose copies under the parent
    group's are `nested`; two names for one dataset in one bucket are a `moved` group (a diagonal pair)."""
    rows = tree(
        node("a/p", 3 * G, 3, "dir"), node("a/p/q", 2 * G, 2, "dir"), node("a/p/r", G, 1),
        node("b/p", 3 * G, 3, "dir"), node("b/p/q", 2 * G, 2, "dir"), node("b/p/r", G, 1),
        node("c/q", 2 * G, 2, "dir"),
        node("d/en/data", 5 * G, 4, "dir"), node("d/fr/data", 5 * G, 4, "dir"),
    )
    out = tmp_path / "out"
    s = find_dups(write(tmp_path, rows), out)
    assert table(out) == [
        ("moved", "d/en/data", 5 * G, 4, 2, 5 * G, 1, False, False, True, None),
        ("moved", "d/fr/data", 5 * G, 4, 2, 5 * G, 1, False, False, False, None),
        ("moved", "a/p/q", 2 * G, 2, 3, 4 * G, 3, True, False, True, None),
        ("moved", "b/p/q", 2 * G, 2, 3, 4 * G, 3, True, False, False, None),
        ("moved", "c/q", 2 * G, 2, 3, 4 * G, 3, False, False, False, None),
        ("tree", "a/p", 3 * G, 3, 2, 3 * G, 2, False, False, True, None),
        ("tree", "b/p", 3 * G, 3, 2, 3 * G, 2, False, False, False, None),
    ]
    assert s["net_by_tier"] == [
        {"tier": "moved", "groups": 2, "copies": 3, "bytes": 12 * G, "reclaimable": 5 * G + 2 * G * 2 // 3},
        {"tier": "tree", "groups": 1, "copies": 2, "bytes": 6 * G, "reclaimable": 3 * G},
    ]
    assert s["by_pair"] == [
        {"tier": "moved", "b1": "d", "b2": "d", "groups": 1, "bytes": 5 * G},
        {"tier": "tree", "b1": "a", "b2": "b", "groups": 1, "bytes": 3 * G},
    ]


def test_suspect_groups_are_totaled_apart(tmp_path):
    rows = tree(*(node(f"b{i}/run{i}/part-0.parquet", 2 * G) for i in range(7)))
    out = tmp_path / "out"
    s = find_dups(write(tmp_path, rows), out, suspect_copies=7)
    assert [(r[0], r[1], r[4], r[8]) for r in table(out)] == [("moved", f"b{i}/run{i}/part-0.parquet", 7, True) for i in range(7)]
    assert (s["net_by_tier"], s["suspect"], s["by_pair"]) == (
        [], [{"tier": "moved", "groups": 1, "copies": 7, "bytes": 14 * G, "reclaimable": 12 * G}], [],
    )


def test_spot_check_against_the_keep_copy(tmp_path):
    listings = {
        "a/ds": Listing({"s0": (1, "x"), "s1": (1, "y")}, True),
        "b/ds": Listing({"s0": (1, "x"), "s1": (1, "y")}, True),
        "c/old/ds": Listing({"s0": (1, "x"), "s1": (1, "z")}, True),
    }
    asked: list[str] = []

    def lister(path: str, kind: str) -> Listing:
        asked.append(f"{kind}:{path}")
        return listings[path]

    out = tmp_path / "out"
    s = find_dups(write(tmp_path, fleet()), out, check=1, lister=lister, workers=1)
    rows = [(r["tier"], r["path"], r["keep"], r["check"], r["check_n"], r["check_eq"]) for r in pq.read_table(out / "dups.parquet").to_pylist()]
    assert rows == [
        ("moved", "a/ds", False, "equal", 2, 2),
        ("moved", "b/ds", True, None, None, None),
        ("moved", "c/old/ds", False, "differs", 2, 1),
        ("tree", "a/ds", False, None, None, None),
        ("tree", "b/ds", True, None, None, None),
    ]
    assert asked == ["dir:b/ds", "dir:a/ds", "dir:c/old/ds"]
    assert (s["checked_groups"], s["checks"]) == (1, [
        {"tier": "moved", "check": "differs", "groups": 1, "copies": 1},
        {"tier": "moved", "check": "equal", "groups": 1, "copies": 1},
    ])


def test_compare_verdicts():
    a = Listing({"x": (1, "c1"), "y": (2, "c2"), "z": (3, "c3")}, True)
    assert compare(a, Listing(dict(a.objects), True)) == ("equal", 3, 3)
    assert compare(a, Listing({"x": (1, "c1"), "y": (2, "c2")}, True)) == ("differs", 2, 2)
    assert compare(a, Listing({"x": (1, "c1"), "y": (2, "XX"), "z": (3, "c3")}, True)) == ("differs", 3, 2)
    # capped: compare only up to the smaller listing's last name
    assert compare(a, Listing({"x": (1, "c1"), "y": (2, "c2")}, False)) == ("partial", 2, 2)
    assert compare(Listing({"x": (1, "c1"), "z": (3, "c3")}, False), Listing({"x": (1, "c1"), "y": (2, "c2"), "z": (3, "c3")}, False)) == ("differs", 2, 2)


def test_cli_writes_both_files(tmp_path):
    src = write(tmp_path, fleet())
    out = tmp_path / "o"
    r = CliRunner().invoke(main, ["dups", "-d", "2026-10-18T0701", "-o", str(out), src])
    assert r.exit_code == 0, r.output
    s = json.loads(r.stdout)
    assert (s["scan"], s["groups"], s["rows"]) == ("2026-10-18T0701", 2, 5)
    assert json.loads((out / "dups.json").read_text()) == s
    assert sorted(p.name for p in out.iterdir()) == ["dups.json", "dups.parquet"]


LISTING = pa.schema([("bucket", pa.string()), ("name", pa.string()), ("size_bytes", pa.int64()), ("crc32c", pa.int64())])


def listing(tmp: Path, objects: dict[str, tuple[int, int | None]], crc: bool = True) -> str:
    """A raw listing shard (`bulk-list -c`'s columns) of `{"<bucket>/<key>": (size, crc32c)}`, plus an unrelated
    object (another top-level dir: pruned) and one in a bucket no copy is in."""
    objects = {**objects, "a/zzz/other": (5, 1), "q/ds/s0": (2 * G, 7)}
    rows = [{"bucket": k.split("/", 1)[0], "name": k.split("/", 1)[1], "size_bytes": v[0], "crc32c": v[1]} for k, v in objects.items()]
    t = pa.Table.from_pylist(rows, schema=LISTING)
    if not crc:
        t = t.drop_columns(["crc32c"])
    p = tmp / "shard-00-0000.parquet"
    pq.write_table(t, p)
    return str(p)


def fleet_objects(**over: tuple[int, int | None]) -> dict[str, tuple[int, int | None]]:
    base = {f"{d}/s{i}": (2 * G, 100 + i) for d in ("a/ds", "b/ds", "c/old/ds") for i in (0, 1)}
    return {**base, "a/f.bin": (3 * G, 9), "b/f.bin": (3 * G, 9), **over}


def fleet_with_files() -> list[dict]:
    return fleet() + [node("a/f.bin", 3 * G, created=5), node("b/f.bin", 3 * G, created=6)]


VCOLS = ["tier", "path", "copies", "n_buckets", "keep", "exact", "check", "check_n", "check_eq"]


def vtable(out: Path) -> list[tuple]:
    return [tuple(r[c] for c in VCOLS) for r in pq.read_table(out / "dups.parquet").to_pylist()]


def test_listing_proves_copies_exact(tmp_path):
    """Every object equal by relative name, size and crc32c: the tree groups (dirs and an object) are `exact`, their
    non-keep copies `equal`; the `moved` group's renamed copy (`c/old/ds`) differs in one object, so it is dropped,
    which leaves the moved group with the tree group's copies: that group, gone."""
    out = tmp_path / "out"
    s = find_dups(write(tmp_path, fleet_with_files()), out, listings=[listing(tmp_path, fleet_objects(**{"c/old/ds/s1": (2 * G, 999)}))])
    assert vtable(out) == [
        ("tree", "a/ds", 2, 2, False, True, "equal", 2, 2),
        ("tree", "b/ds", 2, 2, True, True, None, None, None),
        ("tree", "a/f.bin", 2, 2, True, True, None, None, None),
        ("tree", "b/f.bin", 2, 2, False, True, "equal", 1, 1),
    ]
    assert (s["verified_copies"], s["groups"], s["exact"]) == (4, 2, [
        {"tier": "tree", "groups": 2, "copies": 4, "bytes": 14 * G, "reclaimable": 7 * G},
    ])


def test_listing_differs_marks_tree_and_drops_moved(tmp_path):
    """`b/ds/s1`'s contents differ: the tree group stays, its other copy `differs` (not exact); every moved copy
    differs from the keep copy (`b/ds`), so the moved group goes."""
    out = tmp_path / "out"
    find_dups(write(tmp_path, fleet()), out, listings=[listing(tmp_path, fleet_objects(**{"b/ds/s1": (2 * G, 999)}))])
    assert vtable(out) == [
        ("tree", "a/ds", 2, 2, False, False, "differs", 2, None),
        ("tree", "b/ds", 2, 2, True, False, None, None, None),
    ]


def test_listing_without_checksums_gives_no_verdict(tmp_path):
    """An object without a crc32c leaves its copy unverified (and its group for `-g`); a listing without the column
    (no `bulk-list -c`) changes nothing: the output is the no-listing one."""
    plain, unverified, nocol = (tmp_path / n for n in ("plain", "unverified", "nocol"))
    for d in (plain, unverified, nocol):
        d.mkdir()
    find_dups(write(plain, fleet()), plain / "out")
    find_dups(write(unverified, fleet()), unverified / "out", listings=[listing(unverified, fleet_objects(**{"a/ds/s0": (2 * G, None)}))])
    find_dups(write(nocol, fleet()), nocol / "out", listings=[listing(nocol, fleet_objects(), crc=False)])
    assert vtable(unverified / "out") == [
        ("moved", "a/ds", 3, 3, False, False, None, None, None),
        ("moved", "b/ds", 3, 3, True, False, None, None, None),
        ("moved", "c/old/ds", 3, 3, False, False, "equal", 2, 2),
        ("tree", "a/ds", 2, 2, False, False, None, None, None),
        ("tree", "b/ds", 2, 2, True, False, None, None, None),
    ]
    assert pq.read_table(nocol / "out" / "dups.parquet") == pq.read_table(plain / "out" / "dups.parquet")


def test_spot_check_skips_groups_the_listing_settled(tmp_path):
    asked: list[str] = []

    def lister(path: str, kind: str) -> Listing:
        asked.append(path)
        return Listing({}, True)

    out = tmp_path / "out"
    s = find_dups(write(tmp_path, fleet()), out, check=5, lister=lister, workers=1, listings=[listing(tmp_path, fleet_objects())])
    assert (asked, s["checked_groups"], [r[5] for r in vtable(out)]) == ([], 0, [True] * 5)


def test_cli_listing_flag(tmp_path):
    src = write(tmp_path, fleet())
    out = tmp_path / "o"
    r = CliRunner().invoke(main, ["dups", "-o", str(out), "-l", listing(tmp_path, fleet_objects()), src])
    assert r.exit_code == 0, r.output
    assert [r[:6] for r in vtable(out)] == [
        ("moved", "a/ds", 3, 3, False, True), ("moved", "b/ds", 3, 3, True, True), ("moved", "c/old/ds", 3, 3, False, True),
        ("tree", "a/ds", 2, 2, False, True), ("tree", "b/ds", 2, 2, True, True),
    ]

