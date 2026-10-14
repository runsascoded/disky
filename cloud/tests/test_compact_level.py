"""The binary counter's compaction level, per deployment profile (`Profile.compact_level`): carries stop below it in the
plan (`plan_carries`), the merge job (`merge_pending`, `-L` on both stores' `carry`) and the merge stage's "level due"
log; `none` (unbounded) carries at every level and never asks for a compaction; the profiles' env overrides and
validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import interval_append as ia
from dt_cloud import static_merge as sm
from dt_cloud import static_runner as sd
from dt_cloud.static_profile import COMPACT_LEVEL, Profile, from_mapping, load_profile
from dt_cloud.static_profile_examples import CW, GCS

from test_interval_append import G1, Fake as IvFake
from test_static_runner import CFG, GEN, _py


def _days(n: int) -> list[str]:
    return [f"2026-{1 + (i - 1) // 28:02d}-{1 + (i - 1) % 28:02d}" for i in range(1, n + 1)]


def _run(d: str) -> dict:
    return {"key": f"deltas/{d}", "first": d, "last": d, "level": 0, "scans": [d]}


def _merged(scans: list[str], level: int) -> dict:
    return {"key": ar.run_key(scans[0], scans[-1]), "first": scans[0], "last": scans[-1], "level": level, "scans": scans}


EIGHT = [_run(d) for d in _days(8)]
L2A, L2B, L3 = _merged(_days(8)[:4], 2), _merged(_days(8)[4:], 2), _merged(_days(8), 3)


# ── The plan ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("level, after, merges", [
    # Carries stop below L3: two L2s, each one 4-way merge; the L3 they'd make is a compaction's.
    (3, [L2A, L2B], [(EIGHT[:4], L2A), (EIGHT[4:], L2B)]),
    # Below L5 the eight fold into one L3: one 8-way merge.
    (5, [L3], [(EIGHT, L3)]),
    # Unbounded: as at 5 (nothing here reaches 5).
    (None, [L3], [(EIGHT, L3)]),
    # L1: nothing ever carries.
    (1, EIGHT, []),
])
def test_plan_carries_stops_below_the_level(level, after, merges):
    assert ar.plan_carries(EIGHT, max_level=level) == (after, merges)


def test_plan_carries_defaults_to_the_fallback():
    assert ar.plan_carries(EIGHT) == ar.plan_carries(EIGHT, max_level=COMPACT_LEVEL) == ([L3], [(EIGHT, L3)])


def test_unbounded_carries_past_any_level():
    """Two L4s carry into an L5 only when unbounded. Unbounded, 365 scans leave one run per set bit of 365 (0b101101101:
    6 runs, levels 8 down to 0); capped at 5, 22 L4s and then 13's bits."""
    l4s = [_merged(_days(32)[:16], 4), _merged(_days(32)[16:], 4)]
    assert ar.plan_carries(l4s) == (l4s, [])
    assert ar.plan_carries(l4s, max_level=None) == ([_merged(_days(32), 5)], [(l4s, _merged(_days(32), 5))])
    days = _days(365)
    after, _ = ar.plan_carries([_run(d) for d in days], max_level=None)
    assert [(r["level"], len(r["scans"])) for r in after] == [(8, 256), (6, 64), (5, 32), (3, 8), (2, 4), (0, 1)]
    assert [s for r in after for s in r["scans"]] == days
    capped, _ = ar.plan_carries([_run(d) for d in days], max_level=5)
    assert [r["level"] for r in capped] == [4] * 22 + [3, 2, 0]


# ── `merge_pending`: every replanned carry stops below the level ───────────


def _stub_build(dirs: list[Path], run: dict, outp: Path, *, drilled: bool, tmp: Path, log) -> dict:
    outp.mkdir(parents=True)
    (outp / "meta.json").write_text(json.dumps({"scans": run["scans"]}) + "\n")
    return {"rows": None, "bytes": None, "s": {}}


@pytest.mark.parametrize("level, merged, runs", [
    (3, [(_days(8)[:4], L2A["key"], 2), (_days(8)[4:], L2B["key"], 2)], [L2A, L2B]),
    (5, [(_days(8), L3["key"], 3)], [L3]),
    (None, [(_days(8), L3["key"], 3)], [L3]),
])
def test_merge_pending_merges_up_to_the_level(tmp_path, level, merged, runs):
    store = ar.LocalRunStore(tmp_path / "gen", tmp_path / "scratch", gen="g")
    store.create("manifests/2026-01-08.json", json.dumps({"date": "2026-01-08", "runs": EIGHT}) + "\n")
    carry = ar.Carry(build=_stub_build, missing=lambda store, runs: [])
    plan = ar.merge_pending(store, carry, tmp_path / "mount", tmp=tmp_path / "tmp", dry_run=True, max_level=level)
    assert plan == {"manifest": "manifests/2026-01-08.json",
                    "plan": [{"inputs": [f"deltas/{d}" for d in ins], "output": out, "level": lvl} for ins, out, lvl in merged]}
    doc = ar.merge_pending(store, carry, tmp_path / "mount", tmp=tmp_path / "tmp", max_level=level, log=lambda m: None)
    assert [(d["inputs"], d["output"], d["level"]) for d in doc["merged"]] == [([f"deltas/{d}" for d in ins], out, lvl) for ins, out, lvl in merged]
    assert store.read_json(doc["manifest"])["runs"] == runs


@pytest.mark.parametrize("args, level", [([], 5), (["-L", "3"], 3), (["-L", "none"], None), (["-L", "NONE"], None)])
def test_carry_clis_parse_the_level(monkeypatch, args, level):
    seen = []
    monkeypatch.setattr(sm, "merge_pending", lambda *a, **kw: seen.append(kw["max_level"]) or {})
    monkeypatch.setattr(sm, "gcs_store", lambda *a: None)
    monkeypatch.setattr("dt_cloud.static_names.gen_rule_at", lambda bucket, gen: None)
    r = CliRunner().invoke(sm.cli, ["carry", "-b", "b", "-S", "s", "-g", "g", "-m", "/m", *args])
    assert (r.exit_code, seen) == (0, [level])
    monkeypatch.setattr(ar, "merge_pending", lambda *a, **kw: seen.append(kw["max_level"]) or {})
    monkeypatch.setattr(ia, "GcsRunStore", lambda *a, **kw: None)
    monkeypatch.setattr(ia, "read_json", lambda url: {"k": 4})
    r = CliRunner().invoke(ia.cli, ["carry", "-b", "b", "-g", "g", "-R", "g", "-S", "s", "-m", "/m", *args])
    assert (r.exit_code, seen) == (0, [level, level])


@pytest.mark.parametrize("raw", ["0", "x"])
def test_carry_cli_refuses_a_bad_level(raw):
    r = CliRunner().invoke(sm.cli, ["carry", "-b", "b", "-S", "s", "-g", "g", "-m", "/m", "-L", raw])
    assert (r.exit_code, r.output.splitlines()[-1]) == (2, f"Error: Invalid value for '-L' / '--compact-level': -L: {raw!r} is not an integer ≥ 1 or none")


# ── The runners: the merge job's `-L`, the "level due" log ─────────────────


def _two(level: int) -> list[dict]:
    mk = lambda a, b: {"key": f"deltas/{a}_{b}", "first": a, "last": b, "level": level, "scans": [a, b]}  # noqa: E731
    return [mk("2026-08-04", "2026-08-05"), mk("2026-08-06", "2026-08-07")]


def test_interval_level_3_is_left_to_a_compaction():
    """Two L2s at level 3: the merge stage submits nothing and says a level-3 compaction is due."""
    f = IvFake(["2026-08-03"], compact_level=3)
    f.keys[f"{G1}/manifests/2026-08-07.json"] = {"date": "2026-08-07", "runs": _two(2)}
    f.runner(merge_wait=None).carries()
    assert (f.jobs, [m for m in f.log if m.startswith("merge")]) == (
        [], ["merge: level 3 is due: compact into a new base generation (carries stop below it)"])


def test_interval_nothing_due_is_logged():
    """An L1 above an L0: no carry is due, and the merge stage says so (with the stack's levels)."""
    f = IvFake(["2026-08-03"], compact_level=5)
    runs = [{**_two(1)[0]}, {"key": "deltas/2026-08-06", "first": "2026-08-06", "last": "2026-08-06", "level": 0, "scans": ["2026-08-06"]}]
    f.keys[f"{G1}/manifests/2026-08-06.json"] = {"date": "2026-08-06", "runs": runs}
    f.runner(merge_wait=None).carries()
    assert (f.jobs, [m for m in f.log if m.startswith("merge")]) == ([], ["merge: nothing due (L1 L0)"])


@pytest.mark.parametrize("level, runs_level, arg, top", [(5, 2, "5", 3), (None, 4, "none", 5), (None, 9, "none", 10)])
def test_interval_carries_below_the_level_or_unbounded(level, runs_level, arg, top):
    """Two L2s at level 5: one merge job, `carry -L 5`, into an L3; unbounded, two L4s (or L9s) carry on up, and no
    compaction is ever due."""
    f = IvFake(["2026-08-03"], compact_level=level)
    f.keys[f"{G1}/manifests/2026-08-07.json"] = {"date": "2026-08-07", "runs": _two(runs_level)}
    f.runner(merge_wait=None).carries()
    assert [c for c in f.calls() if c[0] == "merge"] == [("merge", f"carry -L {arg} -M 90GB -p 16 -b data -g g1 -R g0 -S scr -m /gcs/data")]
    assert f.stack("2026-08-07.m001.json") == [("deltas/2026-08-04_2026-08-07", top)]
    assert [m for m in f.log if m.startswith("merge: level")] == []


def _static_runner(level) -> sd.Runner:
    return sd.Runner(cfg=Profile(**{**CFG.__dict__, "compact_level": level}), exists=lambda k: False, count=lambda p, s: 0,
                     read_json=lambda k: {}, published=lambda layouts, start: [], run_job=lambda *a, **kw: None,
                     prepare=lambda d: None, prune=lambda d: None, publish=lambda d: None)


@pytest.mark.parametrize("level, arg", [(5, "5"), (3, "3"), (None, "none")])
def test_merge_jobs_pass_the_level(level, arg):
    _, spec = _static_runner(level).merge_job("2026-10-10")
    assert spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1] == _py("static_merge", "carry", "-g", GEN, "-L", arg)
    _, spec = IvFake(["2026-08-03"], compact_level=level).runner().merge_job("2026-08-04")
    assert spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1] == (
        f"set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.interval_append carry -L {arg} -M 90GB -p 16 "
        "-b data -g g1 -R g0 -S scr -m /gcs/data")


# ── Profiles: explicit in the examples, env overrides, validation ──────────


def test_examples_set_the_level_explicitly():
    assert [GCS.compact_level, CW.compact_level, ia.load_config("gcs", {"INTERVAL_STORE_IMAGE": "img"}).p.compact_level] == [5, 5, 5]
    # Unset: the documented fallback.
    assert (Profile().compact_level, from_mapping({}).compact_level, from_mapping({"compact_level": None}).compact_level) == (5, 5, None)


@pytest.mark.parametrize("raw, want", [("3", 3), (" 6 ", 6), ("none", None), ("None", None)])
def test_static_env_overrides_the_level(raw, want):
    assert load_profile({"STATIC_NAMES_PROFILE": "gcs", "STATIC_NAMES_COMPACT_LEVEL": raw}) == Profile(**{**GCS.__dict__, "compact_level": want})


@pytest.mark.parametrize("raw", ["0", "-1", "x", "2.5", "null"])
def test_static_env_level_is_validated(raw):
    with pytest.raises(SystemExit) as e:
        load_profile({"STATIC_NAMES_PROFILE": "cw", "STATIC_NAMES_COMPACT_LEVEL": raw})
    assert str(e.value) == f"STATIC_NAMES_COMPACT_LEVEL: {raw!r} is not an integer ≥ 1 or none"


@pytest.mark.parametrize("v", [0, -2, "5", "none", 2.0, True])
def test_profile_level_is_validated(v):
    with pytest.raises(SystemExit) as e:
        from_mapping({"compact_level": v})
    assert str(e.value) == f"compact_level must be an integer ≥ 1 or none, not {v!r}"


def _append(**kw) -> dict:
    return {"bucket": "data", "append": {"gen": "g1", "scratch": "scr", "layouts": ["l/{id}/p.parquet"], "region": "r", "image": "img", "sa": "sa",
                                         "r2_bucket": "r2b", **kw}}


def test_interval_env_overrides_and_validates_the_level(tmp_path):
    env = {"INTERVAL_STORE_IMAGE": "img"}
    assert [ia.load_config("gcs", {**env, "INTERVAL_STORE_COMPACT_LEVEL": v}).p.compact_level for v in ("3", "none")] == [3, None]
    with pytest.raises(SystemExit) as e:
        ia.load_config("gcs", {**env, "INTERVAL_STORE_COMPACT_LEVEL": "0"})
    assert str(e.value) == "INTERVAL_STORE_COMPACT_LEVEL: '0' is not an integer ≥ 1 or none"
    p = tmp_path / "x.json"
    p.write_text(json.dumps(_append(compact_level=None)))  # JSON null: unbounded
    assert ia.load_config(str(p), {}).p.compact_level is None
    p.write_text(json.dumps(_append()))  # unset: the fallback
    assert ia.load_config(str(p), {}).p.compact_level == 5
    p.write_text(json.dumps(_append(compact_level=0)))
    with pytest.raises(SystemExit) as e:
        ia.load_config(str(p), {})
    assert str(e.value) == "compact_level must be an integer ≥ 1 or none, not 0"
