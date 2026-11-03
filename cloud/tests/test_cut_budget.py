"""Every entry that ends in a served-sort cut takes `-M` as its whole process tree's budget (`tree_mem`): at a small
scale, with a fixed `Budget` (this process 200 MiB, a worker 80 MiB), each entry's DuckDB connections, each cut's plan
(`interval_store.plan_cut`: this process + the workers + their Arrow buffers + DuckDB) and each `write_served`'s workers
stay within the `-M` it was given. And `TreeWatch`, the real-scale check, logs the tree's peak and exits non-zero past it."""
from __future__ import annotations

import json
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_interval_local import BASE, DATES, LAYOUT, RG, world  # noqa: F401 (the fixture)

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
