from subprocess import run
from types import SimpleNamespace

import duckdb
import pyarrow as pa
import pytest

from dt_cloud import static_drill as sd
from dt_cloud import static_roots as sr


def roots(rows):
    return pa.Table.from_pylist(rows, schema=sr.ROOT_SCHEMA)


@pytest.mark.parametrize("bounds,expected", [
    (("b/d/", "b/d0"), ["b/d/a", "b/d/b", "b/d/c"]),
    (("b/d/b", "b/d/c"), ["b/d/b"]),
    (("b/d/x", "b/d/z"), []),
])
def test_bulk_ranges_do_not_populate_python_string_key_cache(bounds, expected):
    tab = roots([dict(q="member", path=p, usr=None, vf=1, vt=99, size=1, n_files=1) for p in ["b/d/a", "b/d/b", "b/d/c"]])
    run = [0, 3, None]

    class Group:
        key = "path"

        def select(self, lo, hi):
            assert (lo, hi) == (("member", bounds[0]), ("member", bounds[1]))
            return [{"q_min": "member", "q_max": "member", "k_min": "b/d/a", "k_max": "b/d/c"}], 3

        def group_rows(self, g, keep=True):
            assert not keep  # a bulk read: no decoded group kept
            return tab, {"member": run}

    tier = SimpleNamespace(group_file=lambda *_: Group(), canon=lambda c, _: c)
    reads = sd.TierReads([tier], "long")
    assert [r for t in reads.tables("member", *bounds) for r in t.to_pylist()] == [
        dict(q="member", path=p, usr=None, vf=1, vt=99, size=1, n_files=1) for p in expected
    ]
    assert run == [0, 3, None]


@pytest.mark.parametrize("vcpus,budget", [(8, "30GB"), (16, "60GB"), (32, "120GB")])
def test_drill_reserves_memory_outside_duckdb(vcpus, budget):
    from dt_cloud.static_runner import drill_duckdb_args

    assert drill_duckdb_args(f"n2-highmem-{vcpus}") == ["-M", budget, "-p", str(vcpus)]


def test_duckdb_resource_counters():
    con = duckdb.connect()
    con.execute("CREATE TABLE payload AS SELECT i FROM range(100000) t(i)")
    expected = con.execute("SELECT sum(memory_usage_bytes), sum(temporary_storage_bytes) FROM duckdb_memory()").fetchone()
    assert sd.duckdb_resources(con) == {"duckdb_bytes": expected[0], "duckdb_spill_bytes": expected[1]}
    assert expected[0] > 0


def test_newly_heavy_roots_are_inserted_in_bounded_batches(monkeypatch):
    con = duckdb.connect()
    con.execute("CREATE TABLE rtr (q VARCHAR, dir VARCHAR, k INTEGER, path VARCHAR, usr VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT)")
    rows = [dict(q="old", path=f"b/d/{i}", usr=None, vf=1, vt=99, size=i, n_files=1) for i in range(7)]

    class Reads:
        def tables(self, c, lo, hi):
            assert (c, lo, hi) == ("member", "b/d/", "b/d0")
            yield roots(rows)
            yield roots([{**rows[0], "vt": 5}])

        def rows(self, *args):
            raise AssertionError("bulk history must not materialize a Python row list")

    calls = []
    insert = sd._insert_root_batch

    def record(con, tables):
        calls.append(sum(t.num_rows for t in tables))
        insert(con, tables)

    monkeypatch.setattr(sd, "_insert_root_batch", record)
    assert sd.insert_prior_roots(con, Reads(), [("member", "b/d", 2)], batch_rows=3) == 8
    assert calls == [3, 3, 2]
    assert con.execute("SELECT q, dir, k, path, usr, vf, min(vt), any_value(size), any_value(n_files) FROM rtr GROUP BY q, dir, k, path, usr, vf ORDER BY path").fetchall() == [
        ("member", "b/d", 2, f"b/d/{i}", None, 1, 5 if i == 0 else 99, i, 1) for i in range(7)
    ]


def test_newly_heavy_rollups_match_monolithic_combine(monkeypatch):
    con = duckdb.connect()
    con.execute(f"CREATE TABLE dr ({sr.ROOT_COLS}); CREATE TABLE newq (q VARCHAR); CREATE TABLE newh (q VARCHAR, dir VARCHAR, k INTEGER, rows BIGINT)")
    dirs = [("member", "b/d", 2), ("member", "b/d/sub", 3), ("new", "b/new", 2)]
    sd.insert_rows(con, "newh", [(*d, 3) for d in dirs])
    sd.insert_rows(con, "newq", [("new",)])
    sd.insert_rows(con, "dr", [
        ("member", 3, "b/d/a", None, 1, 3, 10, 1),
        ("member", 3, "b/d/a", "owner", 1, 3, 5, 1),
        ("member", 4, "b/d/sub/b", None, 3, sr.OPEN, 20, 1),
        ("member", 3, "b/d0/outside", None, 3, sr.OPEN, 999, 1),
        ("new", 3, "b/new/c", None, 1, sr.OPEN, 30, 1),
    ])
    history = [
        dict(q="old", path="b/d/a", usr=None, vf=1, vt=sr.OPEN, size=10, n_files=1),
        dict(q="old", path="b/d/a", usr="owner", vf=1, vt=sr.OPEN, size=5, n_files=1),
        dict(q="old", path="b/d/sub/b", usr=None, vf=3, vt=sr.OPEN, size=20, n_files=1),
    ]

    class Reads:
        def __init__(self):
            self.ranges = []

        def cache_stats(self):
            return {}

        def tables(self, c, lo, hi):
            assert c == "member"
            self.ranges.append((c, lo, hi))
            yield roots([r for r in history if lo <= r["path"] < hi])

        def hold(self, queries):
            if queries:
                yield 0, len(queries)

        def io(self):
            return {}

    con.execute("CREATE TABLE rtr (q VARCHAR, dir VARCHAR, k INTEGER, path VARCHAR, usr VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT)")
    sd.insert_prior_roots(con, Reads(), dirs[:2])
    con.execute("""CREATE TABLE oracle AS SELECT q, dir, any_value(k) AS k, path, usr, vf, min(vt) AS vt,
        any_value(size) AS size, any_value(n_files) AS n_files FROM (
          SELECT * FROM rtr UNION ALL
          SELECT h.q,h.dir,h.k,d.path,d.usr,d.vf,d.vt,d.size,d.n_files FROM newh h JOIN dr d ON d.q=h.q AND starts_with(d.path,h.dir||'/'))
        GROUP BY q,dir,path,usr,vf""")
    sd.full_rollups(con, "oracle", 1, "expected")
    expected = con.execute("SELECT * FROM expected ORDER BY ALL").fetchall()
    assert expected == [
        ("member", "b/d", 0, "", 1, 3, 2),
        ("member", "b/d", 1, "sub", 3, 20, 1),
        ("member", "b/d", 2, "", 1, 15, 2),
        ("member", "b/d", 2, "", 3, 0, 0),
        ("member", "b/d/sub", 0, "", 1, 1, 1),
        ("member", "b/d/sub", 1, "b", 3, 20, 1),
        ("new", "b/new", 0, "", 1, 1, 1),
        ("new", "b/new", 1, "c", 1, 30, 1),
    ]
    con.execute("DROP TABLE rtr; DROP TABLE oracle; DROP TABLE expected")
    sizes = []
    full = sd.full_rollups

    def checked(con, table, keep, into):
        sizes.append(con.execute(f"SELECT count(DISTINCT (q,dir)) FROM {table}").fetchone()[0])
        full(con, table, keep, into)

    monkeypatch.setattr(sd, "full_rollups", checked)
    # one chunk: `b/d/sub` is cut from `b/d`'s range, read once; a chunk each: each read on its own
    for chunk_rows, read, chunks, ranges in ((9, 3, [3], [("member", "b/d/", "b/d0")]),
                                             (3, 4, [1, 1, 1], [("member", "b/d/", "b/d0"), ("member", "b/d/sub/", "b/d/sub0")])):
        con.execute("DROP TABLE IF EXISTS rup")
        sizes.clear()
        reads = Reads()
        assert sd.newly_heavy_rollups(con, reads, "long", 1, log=lambda _: None, chunk_rows=chunk_rows) == read
        assert (sizes, reads.ranges) == (chunks, ranges)
        assert con.execute("SELECT * FROM rup ORDER BY ALL").fetchall() == expected


@pytest.mark.parametrize("rows,chunk_rows,expected", [
    ([], 10, []),
    ([3, 3, 3], 9, [(0, 3)]),
    ([3, 3, 3], 8, [(0, 2), (2, 3)]),
    ([20, 1, 1, 20], 10, [(0, 1), (1, 3), (3, 4)]),
])
def test_newly_heavy_chunks(rows, chunk_rows, expected):
    assert sd.newly_heavy_chunks(rows, chunk_rows) == expected


def test_chunk_tops():
    assert sd.chunk_tops([("a", "b"), ("a", "b-x"), ("a", "b/c"), ("a", "b/c/d"), ("a", "bc"), ("z", "b/c"), ("z", "b/cd")]) == [0, 1, 4, 5, 6]


def test_newly_heavy_combine_survives_budget_that_rejects_global_group(tmp_path):
    from dt_cloud.static_names import connect

    con = connect(2, "64MB", tmp_path / "spill")
    # Nested directories amplify the global join; each bounded combine sees the same 4,000 roots only once.
    con.execute("CREATE TABLE newh AS SELECT 'member' AS q, 'b/' || repeat('d/',i) || 'd' AS dir, (i+2)::INTEGER AS k, 4000::BIGINT AS rows FROM range(40) t(i)")
    con.execute("CREATE TABLE newq AS SELECT DISTINCT q FROM newh")
    con.execute(f"""CREATE TABLE dr AS SELECT 'member' AS q, 43::UTINYINT AS depth, 'b/' || repeat('d/',40) || 'x/' || repeat('p',160) || i::VARCHAR AS path,
        NULL::VARCHAR AS usr, 1::BIGINT AS vf, {sr.OPEN}::BIGINT AS vt, 1::BIGINT AS size, 1::BIGINT AS n_files
        FROM range(4000) t(i)""")
    with pytest.raises(duckdb.OutOfMemoryException):
        con.execute("""CREATE TABLE monolithic AS SELECT h.q,h.dir,any_value(h.k) AS k,d.path,d.usr,d.vf,min(d.vt) AS vt,
            any_value(d.size) AS size,any_value(d.n_files) AS n_files FROM newh h JOIN dr d ON d.q=h.q AND starts_with(d.path,h.dir||'/')
            GROUP BY h.q,h.dir,d.path,d.usr,d.vf""")

    class Reads:
        def cache_stats(self):
            return {}

        def tables(self, *args):
            raise AssertionError("New members already carry complete history in dr")

        def hold(self, queries):
            assert queries == []
            yield from ()

    # chunks of 4 nested directories (16,000 rows exploded) fit the budget the 40 at once (160,000) don't
    assert sd.newly_heavy_rollups(con, Reads(), "long", 1, log=lambda _: None, chunk_rows=16_000) == 0
    assert con.execute("SELECT * FROM rup ORDER BY ALL").fetchall() == sorted([
        row for i in range(40) for row in [
            ("member", "b/" + "d/" * i + "d", 0, "", 1, 4000, 1),
            ("member", "b/" + "d/" * i + "d", 1, "x" if i == 39 else "d", 1, 4000, 4000),
        ]
    ])


def test_memory_snapshot_reads_cgroup_events_and_process_rss(tmp_path):
    from dt_cloud.resource_monitor import memory_snapshot

    proc = tmp_path / "proc"
    proc.write_text("Name:\tpython\nVmRSS:\t123 kB\nVmHWM:\t456 kB\n")
    cg = tmp_path / "cgroup"
    cg.mkdir()
    for name, value in {"memory.current": "1000", "memory.max": "2000", "memory.peak": "1500", "memory.events": "low 0\nhigh 1\nmax 2\noom 3\noom_kill 4\n"}.items():
        (cg / name).write_text(value)
    assert memory_snapshot(proc, cg) == {
        "rss_bytes": 123 * 1024,
        "rss_peak_bytes": 456 * 1024,
        "cgroup_current_bytes": 1000,
        "cgroup_limit_bytes": 2000,
        "cgroup_peak_bytes": 1500,
        "cgroup_events": {"low": 0, "high": 1, "max": 2, "oom": 3, "oom_kill": 4},
    }


def test_memory_snapshot_without_linux_files(tmp_path):
    from dt_cloud.resource_monitor import memory_snapshot

    assert memory_snapshot(tmp_path / "proc", tmp_path / "cg") == {}


def test_killed_child_leaves_shell_exit_evidence():
    from dt_cloud.append_runner import drill_exit_diagnostics

    result = run(["bash", "-c", drill_exit_diagnostics() + "exit 137"], capture_output=True, text=True)
    # Linux additionally reports the real cgroup files; this assertion is on the invariant exit marker.
    assert (result.returncode, result.stdout.splitlines()[0], result.stderr) == (137, "DRILL_EXIT code=137", "")


def test_memory_snapshot_cgroup_v1(tmp_path):
    from dt_cloud.resource_monitor import memory_snapshot

    memory = tmp_path / "memory"
    memory.mkdir()
    for name, value in {"memory.usage_in_bytes": "1000", "memory.limit_in_bytes": "2000", "memory.max_usage_in_bytes": "1500", "memory.failcnt": "2"}.items():
        (memory / name).write_text(value)
    assert memory_snapshot(tmp_path / "no-proc", tmp_path) == {
        "cgroup_current_bytes": 1000, "cgroup_limit_bytes": 2000,
        "cgroup_peak_bytes": 1500, "cgroup_limit_hits": 2,
    }


HEAP = {"arrow_bytes": 5, "py_blocks": 7, "glibc_used_bytes": 30, "glibc_free_bytes": 10}


def _fixed(monkeypatch, rss, jemalloc=True):
    monkeypatch.setattr(sd, "memory_snapshot", lambda: {"rss_anon_bytes": next(rss)})
    monkeypatch.setattr(sd, "heap_snapshot", lambda: dict(HEAP))
    monkeypatch.setattr(sd, "duckdb_jemalloc", lambda con: jemalloc)
    con = duckdb.connect()
    con.execute("SET memory_limit = '1GiB'")
    return con


def test_chunk_memory_names_each_holder_and_what_releasing_it_gave_back(monkeypatch):
    """`chunk_memory` (specs/static-catch-up.md C): `memory_breakdown`, pyarrow's pool, the checkpoint's pending rows,
    then what releasing pyarrow's pool and the C heap gave back, measured as RSS before and after each."""
    con = _fixed(monkeypatch, iter([100, 90, 70]))
    monkeypatch.setattr(sd, "release_heap", lambda: None)
    monkeypatch.setattr(sd.pa, "default_memory_pool", lambda: SimpleNamespace(backend_name="jemalloc", max_memory=lambda: 4, release_unused=lambda: None))
    parts = SimpleNamespace(pending=[pa.table({"x": [1, 2]}), pa.table({"x": [3]})])
    assert sd.chunk_memory(con, parts) == {
        "rss_anon_bytes": 100, "duckdb_bytes": 0, "duckdb_spill_bytes": 0, "duckdb_allocator": "jemalloc", "duckdb_limit": "1.0 GiB",
        **HEAP, "unattributed_bytes": 55, "arrow_pool": "jemalloc", "arrow_peak_bytes": 4, "parts_pending_bytes": 24,
        "freed_arrow_bytes": 10, "freed_trim_bytes": 20,
    }


@pytest.mark.parametrize("jemalloc,allocator,unattributed", [(True, "jemalloc", 35), (False, "glibc", 55)])
def test_memory_breakdown_subtracts_duckdb_only_outside_glibc(monkeypatch, jemalloc, allocator, unattributed):
    """DuckDB's buffer bytes are subtracted from the unattributed RSS only when DuckDB allocates through its own jemalloc;
    through glibc they are already in glibc's in-use bytes. The reads' caches are listed, not subtracted (decoded groups
    are pyarrow's bytes, raw bytes glibc's)."""
    con = _fixed(monkeypatch, iter([100]), jemalloc)
    monkeypatch.setattr(sd, "duckdb_resources", lambda con: {"duckdb_bytes": 20, "duckdb_spill_bytes": 3})
    reads = SimpleNamespace(cache_stats=lambda: {"decoded_groups": 2, "decoded_bytes": 4, "index_groups": 1, "index_spans": 1, "raw_bytes": 6})
    assert sd.memory_breakdown(con, reads) == {
        "rss_anon_bytes": 100, "duckdb_bytes": 20, "duckdb_spill_bytes": 3, "duckdb_allocator": allocator, "duckdb_limit": "1.0 GiB", **HEAP,
        "decoded_groups": 2, "decoded_bytes": 4, "index_groups": 1, "index_spans": 1, "raw_bytes": 6, "unattributed_bytes": unattributed,
    }


def test_outside_bytes_needs_rss_and_glibc():
    from dt_cloud.resource_monitor import outside_bytes

    assert [outside_bytes(100, HEAP), outside_bytes(100, HEAP, 20), outside_bytes(None, HEAP), outside_bytes(100, {"arrow_bytes": 5, "py_blocks": 7})] == [
        55, 35, None, None]


def test_a_stages_memory_is_on_its_own_line_only(monkeypatch):
    """`ResourceMonitor.update`: the stage's line carries its measured memory; the periodic lines after it keep the stage
    and its progress, with fresh RSS and heap, not the stage's DuckDB bytes as if current."""
    from json import loads

    from dt_cloud import resource_monitor as rm

    rss = iter([100, 120])
    monkeypatch.setattr(rm, "memory_snapshot", lambda: {"rss_anon_bytes": next(rss)})
    monkeypatch.setattr(rm, "heap_snapshot", lambda: dict(HEAP))
    lines = []
    m = rm.ResourceMonitor(log=lines.append)
    m.update("long: touched", directories=3, duckdb_bytes=9, rss_anon_bytes=99, unattributed_bytes=1, py_blocks=8)
    m.sample()
    drop = ("elapsed_s", "cpu_s")
    assert [{k: v for k, v in loads(line.removeprefix("DRILL_RESOURCE ")).items() if k not in drop} for line in lines] == [
        {"stage": "long: touched", "directories": 3, **HEAP, "outside_glibc_arrow_bytes": 55,
         "duckdb_bytes": 9, "rss_anon_bytes": 99, "unattributed_bytes": 1, "py_blocks": 8},
        {"stage": "long: touched", "directories": 3, "rss_anon_bytes": 120, **HEAP, "outside_glibc_arrow_bytes": 75},
    ]
