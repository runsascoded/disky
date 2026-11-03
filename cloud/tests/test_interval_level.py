"""`dt_cloud.interval_level`: the level sort (`/api/diff/level`'s index) is opt-in (`build -l`, `backfill-level`); its base +
runs (merged or not) hold a fresh build's transitions exactly once; appends and compactions carry it; `backfill-level` adds
exactly the sort a `-l` build cuts, writes nothing else, and later appends carry it."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from test_interval_local import BASE, DATES, LAYOUT, RG, world  # noqa: F401 — the shared scans

from dt_cloud import interval_level as ilv
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist


def _cli(cmd, args: list[str]) -> dict:
    res = CliRunner().invoke(cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _common(world, store: Path, work: Path, gen: str) -> list[str]:  # noqa: F811
    return ["-g", gen, "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(work), "-r", str(RG), "-p", "2", "-M", "1GB"]


def _build(world, store: Path, work: Path, *extra: str, gen: str = "g", through: str = DATES[BASE - 1]) -> dict:  # noqa: F811
    return _cli(il.build_cmd, [*_common(world, store, work, gen), "-k", "3", "-t", through, *extra])


def _add(world, store: Path, work: Path, *extra: str, gen: str = "g") -> dict:  # noqa: F811
    return _cli(il.add_cmd, [*_common(world, store, work, gen), *extra])


def _rows(*files: Path) -> list[tuple]:
    """Every row of the level sorts `files`, in key order (a multiset: a transition twice would show)."""
    rows = [tuple(r.values()) for f in files for r in pq.read_table(f).to_pylist()]
    return sorted(rows, key=lambda r: (r[0], r[1], r[2], r[3]))


def _level(d: Path) -> Path:
    return d / "served" / f"{ilv.LEVEL}.parquet"


def _newest_runs(gen: Path) -> list[Path]:
    names = sorted(p.name for p in (gen / "manifests").iterdir())
    from dt_cloud.append_runner import latest_key

    return [gen / r["key"] for r in json.loads((gen / latest_key([f"manifests/{n}" for n in names])).read_text())["runs"]]


@pytest.fixture(scope="module")
def leveled(world, tmp_path_factory):  # noqa: F811
    """A `-l` base over the first scans, the rest added (the carries merge them); and a fresh `-l` build over every scan."""
    tmp = tmp_path_factory.mktemp("lvl")
    store = tmp / "store"
    _build(world, store, tmp / "wb", "-l")
    shutil.copytree(store, tmp / "base-only")
    _add(world, store, tmp / "wa")
    full = tmp / "full"
    _build(world, full, tmp / "wf", "-l", through=DATES[-1])
    return {"tmp": tmp, "gen": store / ist.PREFIX / "g", "base_only": tmp / "base-only", "full": full / ist.PREFIX / "g"}


def test_the_flag_and_every_runs_sort(leveled):
    gen = leveled["gen"]
    assert json.loads((gen / "scans.json").read_text())[ilv.LEVEL_FLAG] is True
    for d in DATES[BASE:]:
        assert _level(gen / "deltas" / d).is_file(), d
        assert json.loads((gen / "deltas" / d / "scans.json").read_text())[ilv.LEVEL_FLAG] is True
    assert [_level(r).is_file() for r in _newest_runs(gen)] == [True]


def test_base_and_runs_are_a_fresh_builds_transitions(leveled):
    """Base + the merged run, and base + the level-0 runs: each transition once, as a fresh build over every scan."""
    gen = leveled["gen"]
    want = _rows(_level(leveled["full"]))
    assert len(want) > 0
    assert _rows(_level(gen), *map(_level, _newest_runs(gen))) == want
    assert _rows(_level(gen), *(_level(gen / "deltas" / d) for d in DATES[BASE:])) == want


def test_a_compaction_carries_the_sort(world, leveled, tmp_path):  # noqa: F811
    store = tmp_path / "store"
    shutil.copytree(leveled["gen"].parent.parent, store)
    out = _add(world, store, tmp_path / "w", "-K")
    new = store / ist.PREFIX / out["compact"]["gen"]
    assert json.loads((new / "scans.json").read_text())[ilv.LEVEL_FLAG] is True
    assert _rows(_level(new)) == _rows(_level(leveled["full"]))


def test_backfill_adds_exactly_the_sort_and_appends_carry_it(world, leveled, tmp_path):  # noqa: F811
    """A generation built without `-l` (its runs too) gets the `-l` build's sort, byte for byte, and nothing else; its next
    append carries it; a second backfill of the base is refused."""
    store = tmp_path / "store"
    _build(world, store, tmp_path / "wb")
    _add(world, store, tmp_path / "wa", "-t", DATES[-2], "-N")
    gen = store / ist.PREFIX / "g"
    assert not _level(gen).exists()
    before = {p.relative_to(gen).as_posix(): p.read_bytes() for p in gen.rglob("*") if p.is_file()}
    doc = _cli(ilv.backfill_cmd, ["-g", "g", "-o", str(store), "-w", str(tmp_path / "wl"), "-r", str(RG), "-p", "2", "-M", "1GB"])
    after = {p.relative_to(gen).as_posix(): p.read_bytes() for p in gen.rglob("*") if p.is_file()}
    added = sorted(set(after) - set(before))
    runs = [f"deltas/{d}" for d in DATES[BASE:-1]]
    assert added == sorted(f"{d}served/{ilv.LEVEL}{x}" for d in ["", *(f"{r}/" for r in runs)] for x in (".parquet", ".groups.parquet", ".json"))
    assert {k: after[k] for k in before} == before
    assert (sorted(doc["runs"]), doc["skipped_runs"]) == (runs, [])
    # The `-l` build's base sort, byte for byte (same scans, same ranges), and each run's.
    ref = leveled["gen"]
    assert after[f"served/{ilv.LEVEL}.parquet"] == _level(leveled["base_only"] / ist.PREFIX / "g").read_bytes()
    for r in runs:
        assert after[f"{r}/served/{ilv.LEVEL}.parquet"] == _level(ref / r).read_bytes(), r
    # The next append's run carries it (the generation's record is mirrored), and the union is the fresh build's.
    _add(world, store, tmp_path / "wa2", "-N")
    assert _level(gen / "deltas" / DATES[-1]).is_file()
    assert _rows(_level(gen), *(_level(gen / "deltas" / d) for d in DATES[BASE:])) == _rows(_level(leveled["full"]))
    res = CliRunner().invoke(ilv.backfill_cmd, ["-g", "g", "-o", str(store), "-w", str(tmp_path / "wl2")])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{store}/{ist.PREFIX}/g/served/{ilv.LEVEL}.*: the generation has the level sort")


def test_without_the_opt_in_no_sort_is_cut(world, tmp_path):  # noqa: F811
    store = tmp_path / "store"
    _build(world, store, tmp_path / "wb")
    gen = store / ist.PREFIX / "g"
    assert ilv.LEVEL_FLAG not in json.loads((gen / "scans.json").read_text())
    assert sorted(p.name for p in (gen / "served").iterdir() if p.name.startswith(ilv.LEVEL)) == []
