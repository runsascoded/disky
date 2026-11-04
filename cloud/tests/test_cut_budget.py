"""Every entry that ends in a served-sort cut takes `-M` as its whole process tree's budget (`tree_mem`): at a small
scale, with a fixed `Budget` (this process 200 MiB, a worker 80 MiB), each entry's DuckDB connections, each cut's plan
(`interval_store.plan_cut`: this process + the workers + their Arrow buffers + DuckDB) and each `write_served`'s workers
stay within the `-M` it was given. And `TreeWatch`, the real-scale check, logs the tree's peak and exits non-zero past it."""
from __future__ import annotations

import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor as Pool
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import duckdb
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from test_interval_local import BASE, DATES, LAYOUT, RG, _parquets, world  # noqa: F401 (the fixture)

from dt_cloud import interval_append as ia
from dt_cloud import interval_churn as ich
from dt_cloud import interval_compact as ic
from dt_cloud import interval_identity as iid
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from dt_cloud import tree_mem as tm

MIB = tm.MIB
MEM = "1GB"
TOTAL = sn.mem_bytes(MEM)
BASE_B, PROC_B = 200 * MIB, 80 * MIB


@pytest.fixture
def spy(monkeypatch):
    """Every budget measured, connection opened, cut planned and sort written, in order."""
    log: dict[str, list] = {"budgets": [], "connects": [], "cuts": [], "writes": [], "appends": []}

    def measure(cls, total):
        b = cls(total, BASE_B, PROC_B)
        log["budgets"].append(b)
        return b

    monkeypatch.setattr(tm.Budget, "measure", classmethod(measure))
    real_cut = tm.Budget.cut

    def cut(self, *a, **kw):
        plan = real_cut(self, *a, **kw)
        log["cuts"].append({"tree": self, "plan": plan})
        return plan

    monkeypatch.setattr(tm.Budget, "cut", cut)
    real_append = tm.Budget.append

    def append(self, *a, **kw):
        got = real_append(self, *a, **kw)
        log["appends"].append(got)
        return got

    monkeypatch.setattr(tm.Budget, "append", append)
    real_connect = sn.connect

    def connect(threads, mem, tmp):
        log["connects"].append(sn.mem_bytes(mem))
        return real_connect(threads, mem, tmp)

    monkeypatch.setattr(sn, "connect", connect)
    monkeypatch.setattr(ist, "connect", connect)
    real_plan = ist.plan_cut
    active: list[int] = []

    @contextmanager
    def plan_cut(con, tree, files, **kw):
        with real_plan(con, tree, files, **kw) as got:
            assert tree is not None, f"{kw.get('what', 'cut')}: planned without the tree's budget"
            assert log["cuts"][-1]["plan"].procs == got[0]
            active.append(got[0])
            try:
                yield got
            finally:
                active.pop()

    monkeypatch.setattr(ist, "plan_cut", plan_cut)
    real_write = ist.write_served

    def write_served(con, src, sort, out, schema, **kw):
        log["writes"].append({"sort": sort, "procs": kw.get("procs"), "planned": active[-1] if active else None})
        return real_write(con, src, sort, out, schema, **kw)

    monkeypatch.setattr(ist, "write_served", write_served)
    return log


def within(log: dict) -> None:
    """The entry's budgets were all `-M`; each cut's parts sum to ≤ `-M`; each sort's workers ≤ its plan's; each DuckDB
    connection's limit ≤ what this process leaves."""
    assert log["budgets"] and {b.total for b in log["budgets"]} == {TOTAL}
    assert log["cuts"]
    for c in log["cuts"]:
        t, p = c["tree"], c["plan"]
        assert t.base + p.at_once * p.procs * t.proc_base + p.arrow + p.duck <= TOTAL
        assert p.duck >= tm.DUCK_MIN
    assert log["writes"]
    assert [w["procs"] is not None and w["planned"] is not None and w["procs"] <= w["planned"] for w in log["writes"]] == [True] * len(log["writes"])
    assert log["connects"] and max(log["connects"]) <= TOTAL - BASE_B


def _invoke(cmd, args: list[str]) -> str:
    res = CliRunner().invoke(cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return res.stdout


@pytest.fixture(scope="module")
def built(world, tmp_path_factory):  # noqa: F811 (the fixture)
    """A base over the first `BASE` scans, built without the spy (the entries under test start from it)."""
    tmp = tmp_path_factory.mktemp("cut-budget")
    store = tmp / "store"
    _invoke(il.build_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(tmp / "w"), "-k", "3", "-r", str(RG),
                           "-p", "2", "-M", "4GB", "-t", DATES[BASE - 1]])
    return store


def _copy(store: Path, tmp_path: Path) -> Path:
    out = tmp_path / "store"
    shutil.copytree(store, out)
    return out


def test_build(world, spy, tmp_path):  # noqa: F811
    _invoke(il.build_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(tmp_path / "store"), "-w", str(tmp_path / "w"), "-k", "3",
                           "-r", str(RG), "-p", "2", "-M", MEM, "-j", "2", "-t", DATES[BASE - 1]])
    within(spy)
    assert {c["plan"].at_once for c in spy["cuts"]} == {2}


def test_add_runs_carries_and_compaction(world, built, spy, tmp_path):  # noqa: F811
    """Two scans appended (each run's cut), the carry they make due (a merged run's cut), then `-K`'s compaction."""
    store = _copy(built, tmp_path)
    doc = json.loads(_invoke(il.add_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(tmp_path / "w"),
                                          "-r", str(RG), "-p", "2", "-M", MEM, "-t", DATES[BASE + 1]]))
    assert (len(doc["added"]), [m["inputs"] for m in doc["merge"]["merged"]]) == (2, [[f"deltas/{d}" for d in DATES[BASE:BASE + 2]]])
    within(spy)
    # The append's DuckDB leaves room for its writer's Arrow side (rows as wide as the range files': some real bytes).
    [(arrow, duck)] = spy["appends"]
    assert (arrow > tm.APPEND_GROUPS * ist.RANGE_RG, spy["connects"][0]) == (True, duck // MIB * MIB)
    runs = len(spy["writes"])
    _invoke(il.add_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(tmp_path / "w2"), "-r", str(RG), "-p", "2",
                         "-M", MEM, "-j", "2", "-K", "-t", DATES[BASE + 1]])
    within(spy)
    assert len(spy["writes"]) > runs


def test_cut_whole_generation_and_one_sort(built, spy, tmp_path):
    common = ["-b", str(built), "-g", "g", "-m", str(built), "-T", str(tmp_path / "t"), "-p", "2", "-M", MEM, "-U", "-r", str(RG)]
    doc = json.loads(_invoke(ist.cli, ["cut", *common, "-o", str(tmp_path / "all"), "-a", "-j", "2"]))
    gen = built / ist.PREFIX / "g"
    assert sorted(doc["sorts"]) == sorted(p.stem for p in (gen / "served").glob("*.json") if p.stem != "stats")
    for sort in doc["sorts"]:
        assert (tmp_path / "all" / "served" / f"{sort}.parquet").read_bytes() == (gen / "served" / f"{sort}.parquet").read_bytes()
    within(spy)
    _invoke(ist.cli, ["cut", *common, "-o", str(tmp_path / "one"), "-s", "path", "-J", "2", "-w", "1"])
    within(spy)
    assert spy["writes"][-1] == {"sort": "path", "procs": 1, "planned": 1}


def test_migrate_object_version(built, spy, tmp_path):
    store = _copy(built, tmp_path)
    _invoke(iid.migrate_cmd, ["-g", "g", "-O", "g2", "-o", str(store), "-w", str(tmp_path / "w"), "-r", str(RG), "-p", "2", "-M", MEM])
    within(spy)


def test_tree_watch_logs_and_asserts(capsys):
    """The peak over the block against the budget, logged; past it, `SystemExit` when asked to assert (and logged only
    when not). Without procfs, asserting is refused up front."""
    if not tm.has_procfs():
        with pytest.raises(SystemExit, match=r"procfs"):
            with tm.TreeWatch(TOTAL, "x", check=True):
                pass
        with tm.TreeWatch(TOTAL, "x", check=False):
            pass
        assert capsys.readouterr().err == "x: peak tree PSS not measured (no procfs); budget 954 MiB\n"
        return
    line = r"x: peak tree PSS ([0-9,]+) MiB of budget {} MiB \(\d+%; \d+ samples over \ds\){}\n"
    with tm.TreeWatch(1 << 40, "x", every=0.01, check=True) as w:
        pass
    m = re.fullmatch(line.format("1,048,576", ""), capsys.readouterr().err)
    assert m and int(m.group(1).replace(",", "")) == round(w.peak / MIB) and w.peak > 0
    with pytest.raises(SystemExit, match=r"^x: peak tree PSS [0-9,]+ MiB over its budget 1 MiB \(\$DT_TREE_MEM_ASSERT\)$"):
        with tm.TreeWatch(MIB, "x", check=True):
            pass
    assert re.fullmatch(line.format("1", " — OVER BUDGET"), capsys.readouterr().err)
    with tm.TreeWatch(MIB, "x", check=False):
        pass
    assert re.fullmatch(line.format("1", " — OVER BUDGET"), capsys.readouterr().err)


@pytest.fixture
def pss(monkeypatch):
    """A machine whose tree reads as `pss["mib"]` MiB, whatever its OS."""
    now = {"mib": 1}
    monkeypatch.setattr(tm, "has_procfs", lambda: True)
    monkeypatch.setattr(tm, "tree_pss", lambda root=None: now["mib"] * MIB)
    return now


def _normal(stderr: str) -> list[str]:
    return re.sub(r"over \d+s", "over <s>", stderr).splitlines()


def test_tree_watch_peak_per_phase(pss, capsys):
    """A sample counts towards every phase running (nested, overlapping, from any thread), each phase is sampled at its
    start and end, a name run again keeps its largest peak; the phases' lines come before the overall one, which alone
    is asserted on."""
    pss["mib"] = 100
    with tm.TreeWatch(1000 * MIB, "x", every=3600, check=True) as w:
        with tm.phase("a"):  # sampled at 100
            pss["mib"] = 300
            w._sample()
            with tm.phase("b"):  # 300, and 500 at its end
                pss["mib"] = 500
            pss["mib"] = 200
        begun, sampled = Event(), Event()

        def other_thread():
            with tm.phase("c"):  # 200, and at its end
                begun.set()
                sampled.wait()

        with Pool(1) as ex:
            ran = ex.submit(other_thread)
            begun.wait()
            w._sample()  # c still running: 200
            sampled.set()
            ran.result()
        pss["mib"] = 400
        w._sample()  # no phase running
        with tm.phase("b"):  # again: 400 < its 500
            pass
        pss["mib"] = 50
    assert (w.phases, w.untagged) == ({"a": tm.PhasePeak(500 * MIB, 5), "b": tm.PhasePeak(500 * MIB, 4), "c": tm.PhasePeak(200 * MIB, 3)}, tm.PhasePeak(400 * MIB, 3))
    assert _normal(capsys.readouterr().err) == [
        "x: phase a: peak tree PSS 500 MiB (50%; 5 samples)",
        "x: phase b: peak tree PSS 500 MiB (50%; 4 samples)",
        "x: phase c: peak tree PSS 200 MiB (20%; 3 samples)",
        "x: phase (untagged): peak tree PSS 400 MiB (40%; 3 samples)",
        "x: peak tree PSS 500 MiB of budget 1,000 MiB (50%; 13 samples over <s>)",
    ]
    # No watch running: a phase is nothing.
    with tm.phase("nobody"):
        pass
    assert (tm._WATCHES, capsys.readouterr().err) == ([], "")


def test_tree_watch_phase_over_budget_and_the_assert_on_the_overall_peak(pss, capsys):
    pss["mib"] = 90
    with pytest.raises(SystemExit, match=r"^x: peak tree PSS 120 MiB over its budget 100 MiB \(\$DT_TREE_MEM_ASSERT\)$"):
        with tm.TreeWatch(100 * MIB, "x", every=3600, check=True):
            with tm.phase("a"):
                pss["mib"] = 120
    assert _normal(capsys.readouterr().err) == [
        "x: phase a: peak tree PSS 120 MiB (120%; 2 samples) — OVER BUDGET",
        "x: phase (untagged): peak tree PSS 120 MiB (120%; 2 samples) — OVER BUDGET",
        "x: peak tree PSS 120 MiB of budget 100 MiB (120%; 4 samples over <s>) — OVER BUDGET",
    ]


#: `append_range`'s steps, in the order an owner-sliced, churn-carrying generation's range first runs them.
RANGE_PHASES = [
    "range load",
    "range intervals pvl", "range delta pvl", "range state pvl", "range reconstructs pvl", "range churn",
    "range intervals sv", "range delta sv", "range state sv", "range reconstructs sv",
    "range intervals svt", "range delta svt", "range state svt", "range reconstructs svt",
    "range check",
]


def test_tree_watch_says_where_each_peak_was_and_what_held_it(pss, capsys):
    """Each phase's line names the label and the gauges of its peak's sample (not the phase's last), the watch's end the
    labels' largest peaks; outside `at` and `gauges`, a line stays as it was."""
    held = {"DuckDB": 0, "Arrow": 0}

    def step(label: str, mib: int, duck: int, arrow: int) -> None:
        pss["mib"], held["DuckDB"], held["Arrow"] = mib, duck * MIB, arrow * MIB
        with tm.at(label), tm.phase("step"):
            pass

    w = tm.TreeWatch(1000 * MIB, "x", every=3600, check=False)
    with w:
        with tm.phase("outside"):
            pass
        with tm.gauges(DuckDB=lambda: held["DuckDB"], Arrow=lambda: held["Arrow"]):
            for label, mib, duck, arrow in [("r0", 300, 200, 10), ("r1", 700, 500, 90), ("r2", 400, 300, 20)]:
                step(label, mib, duck, arrow)
            for i in range(3, 7):
                step(f"r{i}", 100 + i, 1, 1)
        assert (w.at_label, w.gauge_fns) == (None, {})
        pss["mib"] = 50
    assert _normal(capsys.readouterr().err) == [
        "x: phase outside: peak tree PSS 1 MiB (0%; 2 samples)",
        "x: phase step: peak tree PSS 700 MiB (70%; 14 samples; at r1; DuckDB 500 MiB; Arrow 90 MiB)",
        "x: phase (untagged): peak tree PSS 50 MiB (5%; 2 samples)",
        "x: largest of 7: r1 700 MiB, r2 400 MiB, r0 300 MiB, r6 106 MiB, r5 105 MiB",
        "x: peak tree PSS 700 MiB of budget 1,000 MiB (70%; 18 samples over <s>)",
    ]
    # No watch running: both are nothing.
    with tm.at("r"), tm.gauges(a=lambda: 1 / 0):
        pass
    assert (tm._WATCHES, capsys.readouterr().err) == ([], "")


def _boom() -> int:
    raise RuntimeError("no gauge")


def test_a_failing_gauge_is_said_and_leaves_a_phases_block_alone(pss, capsys):
    """On the thread doing the work: the block completes, and the peak's line says the read failed, beside the gauge that read."""
    pss["mib"] = 100
    with tm.TreeWatch(1000 * MIB, "x", every=3600, check=False):
        with tm.gauges(DuckDB=_boom, Arrow=lambda: 5 * MIB), tm.phase("a"):
            pass
    assert _normal(capsys.readouterr().err) == [
        "x: phase a: peak tree PSS 100 MiB (10%; 2 samples; DuckDB unread (2 failed); Arrow 5 MiB)",
        "x: phase (untagged): peak tree PSS 100 MiB (10%; 2 samples)",
        "x: peak tree PSS 100 MiB of budget 1,000 MiB (10%; 4 samples over <s>)",
    ]


def test_a_failing_gauge_leaves_the_sampler_running(pss, capsys):
    """On the watch's own thread: it keeps sampling, so the peak still sees what the tree does after the failures."""
    w = tm.TreeWatch(1000 * MIB, "x", every=0.001, check=False)
    with w, tm.gauges(DuckDB=_boom):
        deadline = monotonic() + 30
        while w.gauge_fails["DuckDB"] < 3 and monotonic() < deadline:
            sleep(0.001)
        assert w.gauge_fails["DuckDB"] >= 3
        pss["mib"] = 700
        seen = w.samples
        while w.samples == seen and monotonic() < deadline:
            sleep(0.001)
        assert w._thread.is_alive()
        assert w.peak == 700 * MIB
    capsys.readouterr()


def test_duck_bytes_reads_the_buffer_manager_from_another_thread():
    """The gauge is DuckDB's own total: it grows with a table `con` holds, read on its cursor while `con` itself is busy."""
    con = duckdb.connect()
    con.execute("SET threads=2")
    gauge = tm.duck_bytes(con)
    empty = gauge()
    con.execute("CREATE TABLE t AS SELECT i, repeat('x', 100) || i AS s FROM range(2000000) r(i)")
    held = gauge()
    assert held > empty + 64 * MIB
    with Pool(1) as ex:
        busy = ex.submit(con.execute, "SELECT count(DISTINCT s) FROM t")
        during = gauge()
        assert busy.result().fetchone() == (2000000,)
    assert during >= held


def _phases(stderr: str, what: str) -> list[str]:
    return re.findall(rf"^{re.escape(what)}: phase (.+?): peak tree PSS", stderr, re.M)


def test_cut_all_names_its_phases(built, pss, tmp_path):
    res = CliRunner().invoke(ist.cli, ["cut", "-b", str(built), "-g", "g", "-m", str(built), "-T", str(tmp_path / "t"), "-p", "2", "-M", MEM, "-U", "-r", str(RG),
                                       "-o", str(tmp_path / "all"), "-a"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    gen = built / ist.PREFIX / "g"
    sorts = [s for s in ist.served_sorts(*(json.loads((gen / "scans.json").read_text()).get(k, False) for k in ("slices", ist.ATTRIBUTION)))]
    got = _phases(res.stderr, "cut g")
    assert (got[0], got[-2:], sorted(got[1:-2])) == ("cut", ["churn_stats", "(untagged)"], sorted(f"sort {s}" for s in sorts))


def test_add_names_its_stages(world, built, pss, tmp_path):  # noqa: F811
    store = _copy(built, tmp_path)
    res = CliRunner().invoke(il.add_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(tmp_path / "w"),
                                          "-r", str(RG), "-p", "2", "-M", MEM, "-t", DATES[BASE]], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert _phases(res.stderr, "add g") == [
        "read", "append", "append ranges", *RANGE_PHASES, "append churn",
        "cut path", "cut bysize", "cut slices", "cut slices-bytotal", "cut slices-bysize-user",
        "append check", "merge", "upload", "(untagged)",
    ]
    res = CliRunner().invoke(il.add_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(tmp_path / "w2"),
                                          "-r", str(RG), "-p", "2", "-M", MEM, "-K", "-t", DATES[BASE]], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    got = _phases(res.stderr, "add g")
    # The sorts run largest source first; their order isn't the point.
    assert (got[:7], sorted(got[7:-2]), got[-2:]) == (
        ["read", "append", "merge", "upload", "compact", "fold ranges", "cut"],
        sorted(f"sort {s}" for s in ("path", "bysize", "slices", "slices-bytotal", "slices-bysize-user", "reads")),
        ["churn_stats", "(untagged)"],
    )


# ── The staged per-range task, run ─────────────────────────────────────────


def _no_seconds(doc):
    """A range's doc without its timings (`s`, here and in its `check`)."""
    if isinstance(doc, dict):
        return {k: _no_seconds(v) for k, v in doc.items() if k != "s"}
    return doc


def _range_docs(d: Path) -> dict[str, dict]:
    return {p.name: _no_seconds(json.loads(p.read_text())) for p in sorted(d.glob("*.json"))}


def _rows(path: Path) -> list[tuple]:
    t = pq.read_table(path)
    return sorted(zip(*(c.to_pylist() for c in t.columns)), key=repr)


def test_ranges_task_opens_at_its_budget_under_the_watch(world, built, spy, pss, monkeypatch, tmp_path):  # noqa: F811
    """The staged `ranges` task (`ranges_cmd`), both of a scan's 2 tasks over the 3 ranges: each task's one DuckDB opens
    at what `-M` leaves past this process and the writer's Arrow side, rows as wide as that task's own range files
    (`append_mem`); it and every `append_range` run under `-M`'s `TreeWatch`, whose peak is logged; and the deltas, open
    state and range docs are `add`'s, as `add` holds them before the run's churn (`apply_run`, which the staged chain
    leaves to `publish`): byte for byte, but for the scan's churn and prior, which are the same rows."""
    d = DATES[BASE]
    rk = ia.run_key(d, d)
    prefix = f"{ist.PREFIX}/g"

    # The existing append, stopped short of the churn by a copy of the run as it stands there.
    (tmp_path / "add").mkdir()
    added = _copy(built, tmp_path / "add")
    snap = tmp_path / "add" / "before-churn"
    real_apply = ich.apply_run

    def apply_run(con, run, *a, **kw):
        shutil.copytree(run, snap)
        return real_apply(con, run, *a, **kw)

    monkeypatch.setattr(ich, "apply_run", apply_run)
    _invoke(il.add_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(added), "-w", str(tmp_path / "add" / "w"),
                         "-r", str(RG), "-p", "2", "-M", MEM, "-t", d])
    for got in spy.values():
        got.clear()

    # The staged chain: `prepare` pins the scan, then the tasks run over the store's mount.
    (tmp_path / "staged").mkdir()
    store = _copy(built, tmp_path / "staged")
    shutil.copytree(world["root"] / LAYOUT.replace("{id}", d).rsplit("/", 1)[0], store / LAYOUT.replace("{id}", d).rsplit("/", 1)[0])
    scratch = tmp_path / "staged" / "scratch"
    scratch.mkdir()
    ia.prepare_scan(str(store), "g", d, (LAYOUT,))

    opened, appended, said = [], [], []
    real_connect, real_range = sn.connect, ia.append_range

    def watches() -> list[tuple[str, int]]:
        return [(w.what, w.total) for w in tm._WATCHES]

    def connect(threads, mem, tmp):
        con = real_connect(threads, mem, tmp)
        opened.append({"mem": mem, "duckdb": con.execute("SELECT current_setting('memory_limit')").fetchone()[0], "watches": watches()})
        return con

    def append_range(con, prev, scan, r, name, out, **kw):
        appended.append((name, watches()))
        return real_range(con, prev, scan, r, name, out, **kw)

    monkeypatch.setattr(sn, "connect", connect)
    monkeypatch.setattr(ia, "append_range", append_range)
    monkeypatch.setattr(ia, "err", said.append)
    watch = [(f"ranges {d}", TOTAL)]
    tasks = [(0, ["r0000", "r0002"]), (1, ["r0001"])]
    docs = []
    for t, names in tasks:
        opened.clear()
        appended.clear()
        said.clear()
        for got in spy.values():
            got.clear()
        res = CliRunner().invoke(ia.ranges_cmd, ["-b", str(store), "-g", "g", "-R", "g", "-S", str(scratch), "-d", d, "-i", str(t), "-m", str(store), "-M", MEM,
                                                 "-n", "2", "-o", str(tmp_path / "staged" / "out"), "-p", "2", "-T", str(tmp_path / "staged" / "tmp")], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        rb = ic.row_bytes([str(store / prefix / tb / f"{n}.parquet") for tb in ia.tables() for n in names])
        arrow = tm.APPEND_GROUPS * ist.RANGE_RG * rb
        limit = (TOTAL - BASE_B - arrow) // MIB
        assert spy["appends"] == [(arrow, TOTAL - BASE_B - arrow)]
        assert opened == [{"mem": f"{limit}MiB", "duckdb": f"{limit}.0 MiB", "watches": watch}]
        assert appended == [(n, watch) for n in names]
        assert [line for line in said if line.startswith(f"ranges {d}: ")] == [
            f"ranges {d}: memory {TOTAL / MIB:,.0f} MiB = this process {BASE_B / MIB:,.0f} + Arrow {arrow / MIB:,.0f} ({rb} B a row) + DuckDB {(TOTAL - BASE_B - arrow) / MIB:,.0f}",
        ]
        assert _phases(res.stderr, f"ranges {d}") == [*RANGE_PHASES, "(untagged)"]
        assert re.sub(r"\d+ samples", "<n> samples", "\n".join(_normal(res.stderr))).splitlines()[-2:] == [
            f"ranges {d}: largest of {len(names)}: " + ", ".join(f"{n} 1 MiB" for n in sorted(names)),
            f"ranges {d}: peak tree PSS 1 MiB of budget {TOTAL / MIB:,.0f} MiB (0%; <n> samples over <s>)",
        ]
        docs += [json.loads(line) for line in res.stdout.splitlines()]
    assert tm._WATCHES == []

    run = store / prefix / rk
    state = scratch / prefix / "state" / d
    staged, local = {**_parquets(run), **{f"state/{k}": v for k, v in _parquets(state).items()}}, _parquets(snap)
    # The scan's churn and prior: unordered `COPY`s of an aggregate, which `apply_run` aggregates again: the same rows.
    unordered = {k for k in local if k.startswith((f"state/{ich.SCAN_CHURN}/", f"state/{ich.PRIOR}/"))}
    assert (sorted(staged), len(unordered)) == (sorted(local), 6)
    assert {k: v for k, v in staged.items() if k not in unordered} == {k: v for k, v in local.items() if k not in unordered}
    assert {k: _rows(state / k.removeprefix("state/")) for k in unordered} == {k: _rows(snap / k) for k in unordered}
    assert _range_docs(run / "ranges") == _range_docs(snap / "ranges") == {f"r{i:04d}.json": _no_seconds(doc) for i, doc in zip((0, 2, 1), docs)}
