"""The shared compaction policy (`append_runner.Policy`, specs/static-compaction.md "Compaction policy"): its forms
(profile JSON, env, CLI), `compaction_why` per rule (the reason names the rule that fired), the profiles' `compact`
field over the `compact_level` shorthand, the staged runners' merge stage under each rule, and `interval-store add`'s
`-C every:…` / `-K`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import interval_append as ia
from dt_cloud import interval_compact as ic
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud.append_runner import Policy, compaction_why, parse_policy
from dt_cloud.static_profile import Profile, from_mapping, load_profile, to_mapping
from dt_cloud.static_profile_examples import GCS

from test_interval_append import G1, Fake as IvFake
from test_interval_compact import DATES, LAYOUT, RG, build, make_world


def _run(d: str) -> dict:
    return {"key": f"deltas/{d}", "first": d, "last": d, "level": 0, "scans": [d]}


def _merged(scans: list[str], level: int) -> dict:
    return {"key": ar.run_key(scans[0], scans[-1]), "first": scans[0], "last": scans[-1], "level": level, "scans": scans}


# ── Forms ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, want, text, js", [
    ("5", Policy("level", level=5), "level:5", {"level": 5}),
    (5, Policy("level", level=5), "level:5", {"level": 5}),
    ("level:3", Policy("level", level=3), "level:3", {"level": 3}),
    ({"level": 2}, Policy("level", level=2), "level:2", {"level": 2}),
    ("every:7d", Policy("every", every=7, unit="d"), "every:7d", {"every": "7d"}),
    ({"every": "30s"}, Policy("every", every=30, unit="s"), "every:30s", {"every": "30s"}),
    (" Every:2S ", Policy("every", every=2, unit="s"), "every:2s", {"every": "2s"}),
    ("geometric:1", Policy("geometric", ratio=1), "geometric:1", {"geometric": 1}),
    ({"geometric": 1.5}, Policy("geometric", ratio=1.5), "geometric:1.5", {"geometric": 1.5}),
    ("none", ar.NEVER, "none", "none"),
    ("NONE", ar.NEVER, "none", "none"),
])
def test_parse_policy(raw, want, text, js):
    got = parse_policy("x", raw)
    assert (got, str(got), got.to_json(), parse_policy("x", got.to_json()), parse_policy("x", str(got))) == (want, text, js, want, want)


@pytest.mark.parametrize("raw", ["0", "level:0", "every:7", "every:0d", "every:7w", "every:d", "geometric:0", "geometric:-1", "geometric:x",
                                 "none:3", "", "level:", {"level": 2, "every": "1d"}, True, 2.5, None])
def test_parse_policy_refuses(raw):
    with pytest.raises(SystemExit) as e:
        parse_policy("STATIC_NAMES_COMPACT", raw)
    assert str(e.value) == f"STATIC_NAMES_COMPACT: {raw!r} is not a compaction policy ({ar.POLICY_FORMS})"


def test_carry_level():
    """Carries stop below L under a level; every other rule leaves them unbounded below its trigger."""
    assert [parse_policy("x", r).carry_level for r in ("4", "every:7d", "every:9s", "geometric:1", "none")] == [4, None, None, None, None]


# ── Which rule fires ───────────────────────────────────────────────────────


DAYS = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-08T1200"]
BASE = ["2026-09-28", "2026-09-29", "2026-09-30"]


@pytest.mark.parametrize("policy, runs, want", [
    # level: as `compaction_due` always was — two runs at L − 1 once carried
    ("2", [_run(d) for d in DAYS[:3]], None),
    ("2", [_run(d) for d in DAYS[:4]], f"level 2: two runs at level 1 ({ar.run_key(DAYS[0], DAYS[1])}, {ar.run_key(DAYS[2], DAYS[3])})"),
    ("none", [_run(d) for d in DAYS], None),
    # every N scans: the scans past the base
    ("every:4s", [_run(d) for d in DAYS[:3]], None),
    ("every:4s", [_merged(DAYS[:2], 1), _run(DAYS[2]), _run(DAYS[3])], "every 4s: 4 scans past the base"),
    # every N days: the newest scan against the base's newest (2026-09-30)
    ("every:7d", [_run(d) for d in DAYS[:6]], None),
    ("every:7d", [_run(d) for d in DAYS[:7]], "every 7d: the newest scan 2026-10-07 is 7 days after the base's newest 2026-09-30"),
    ("every:8d", [_run(d) for d in DAYS], "every 8d: the newest scan 2026-10-08T1200 is 8.5 days after the base's newest 2026-09-30"),
    # geometric: scans past the base against r × the base's 3
    ("geometric:1", [_run(d) for d in DAYS[:2]], None),
    ("geometric:1", [_run(d) for d in DAYS[:3]], "geometric 1: 3 scans past the base ≥ 1 × its 3"),
    ("geometric:2", [_run(d) for d in DAYS[:5]], None),
    ("geometric:2", [_run(d) for d in DAYS[:6]], "geometric 2: 6 scans past the base ≥ 2 × its 3"),
])
def test_compaction_why(policy, runs, want):
    p = parse_policy("x", policy)
    assert (compaction_why(runs, p, base=BASE), ar.compaction_due(runs, p, base=BASE)) == (want, want is not None)


def test_no_runs_never_due():
    assert [compaction_why([], parse_policy("x", r), base=BASE) for r in ("1", "every:1s", "every:1d", "geometric:0.1")] == [None] * 4


def test_levels_stand_for_their_policy():
    """A bare level (or None) is its `level` policy (`none` unbounded): `compaction_due`'s old callers keep working."""
    runs = [_run(d) for d in DAYS[:4]]
    assert [compaction_why(runs, x) for x in (2, None)] == [compaction_why(runs, Policy("level", level=2)), None]


def test_rules_reading_the_base_need_it():
    with pytest.raises(ValueError) as e:
        compaction_why([_run(DAYS[0])], parse_policy("x", "geometric:1"))
    assert str(e.value) == "compaction policy geometric:1: the base's scans are needed"


# ── Profiles ───────────────────────────────────────────────────────────────


def test_profile_compact_over_the_level_shorthand():
    assert [Profile().policy, Profile(compact_level=3).policy, Profile(compact_level=None).policy] == [
        Policy("level", level=5), Policy("level", level=3), ar.NEVER]
    p = from_mapping({"compact_level": 3, "compact": {"every": "7d"}})
    assert (p.compact, p.policy, to_mapping(p)["compact"], from_mapping(to_mapping(p)) == p) == (
        Policy("every", every=7, unit="d"), Policy("every", every=7, unit="d"), {"every": "7d"}, True)
    assert to_mapping(Profile())["compact"] is None


@pytest.mark.parametrize("raw, want", [("geometric:1", Policy("geometric", ratio=1)), ("every:14d", Policy("every", every=14, unit="d")),
                                       ("4", Policy("level", level=4)), ("none", ar.NEVER)])
def test_static_env_sets_the_policy(raw, want):
    p = load_profile({"STATIC_NAMES_PROFILE": "gcs", "STATIC_NAMES_COMPACT": raw})
    assert (p.policy, p.compact_level) == (want, GCS.compact_level)


def test_profile_refuses_a_bad_policy():
    with pytest.raises(SystemExit) as e:
        from_mapping({"compact": {"every": "7"}})
    assert str(e.value) == f"compact: {{'every': '7'}} is not a compaction policy ({ar.POLICY_FORMS})"


def test_interval_profile_policy(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"bucket": "data", "append": {"gen": "g1", "scratch": "scr", "layouts": ["l/{id}/p.parquet"], "region": "r", "image": "img",
                                                          "sa": "sa", "r2_bucket": "r2b", "compact": {"geometric": 1}}}))
    assert [ia.load_config(str(p), env).p.policy for env in ({}, {"INTERVAL_STORE_COMPACT": "every:3s"})] == [
        Policy("geometric", ratio=1), Policy("every", every=3, unit="s")]


# ── The staged runners' merge stage ────────────────────────────────────────


def _l(scans: list[str], level: int) -> dict:
    return _merged(scans, level) if len(scans) > 1 else _run(scans[0])


@pytest.mark.parametrize("compact, log", [
    ({"every": "4s"}, "merge: compaction due under every:4s (every 4s: 4 scans past the base): compact into a new base generation"),
    ({"geometric": 4}, "merge: compaction due under geometric:4 (geometric 4: 4 scans past the base ≥ 4 × its 1): compact into a new base generation"),
    ({"every": "5s"}, None),
])
def test_runner_merge_stage_names_the_rule(compact, log):
    """Under `every` / `geometric` the carries are unbounded (`carry -L none`: two L1s carry into an L2), and the stage
    names the rule that fired."""
    f = IvFake(["2026-08-03"], compact=compact)
    runs = [_merged(["2026-08-04", "2026-08-05"], 1), _merged(["2026-08-06", "2026-08-07"], 1)]
    f.keys[f"{G1}/manifests/2026-08-07.json"] = {"date": "2026-08-07", "runs": runs}
    r = f.runner(merge_wait=None)
    r.carries()
    assert [c for c in f.calls() if c[0] == "merge"] == [("merge", "carry -L none -M 90GB -p 16 -b data -g g1 -R g0 -S scr -m /gcs/data")]
    assert f.stack("2026-08-07.m001.json") == [("deltas/2026-08-04_2026-08-07", 2)]
    assert ([m for m in f.log if m.startswith("merge: compaction")], r.compaction) == (
        ([log], log.split(" (", 1)[1].rsplit("): compact", 1)[0]) if log else ([], None))


# ── `interval-store add -C every:…` / `-K` ─────────────────────────────────


def _add(world, store: Path, work: Path, *extra: str) -> dict:
    args = ["-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(work), "-r", str(RG), "-p", "2", "-M", "1GB", *extra]
    res = CliRunner().invoke(il.add_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def test_interval_add_every_scans_and_forced(tmp_path):
    """`-C every:3s`: carries unbounded, the third scan past the base compacts; `-K` compacts at once (one scan past
    `g.c001`); each `compaction.json` records the rule and why."""
    world = make_world(tmp_path / "scans", False)
    store = tmp_path / "store"
    build(world, store, tmp_path / "wb", "g", DATES[1])
    outs = [_add(world, store, tmp_path / "w", "-C", "every:3s", "-t", d) for d in DATES[2:5]]
    outs.append(_add(world, store, tmp_path / "w", "-C", "every:3s", "-K", "-t", DATES[5]))
    assert [(o["gen"], o["compact"] and (o["compact"]["gen"], o["compact"]["why"])) for o in outs] == [
        ("g", None), ("g", None), ("g", ("g.c001", "every 3s: 3 scans past the base")), ("g.c001", ("g.c002", "forced (-K)"))]
    root = store / ist.PREFIX
    assert [json.loads((root / g / ic.COMPACTION).read_text()) for g in ("g.c001", "g.c002")] == [
        {"from": "g", "manifest": f"manifests/{DATES[4]}.json", "runs": [ar.run_key(DATES[2], DATES[3]), ar.run_key(DATES[4], DATES[4])],
         "base_scans": 2, "scans": 5, "level": None, "rule": "every:3s", "why": "every 3s: 3 scans past the base"},
        {"from": "g.c001", "manifest": f"manifests/{DATES[5]}.json", "runs": [ar.run_key(DATES[5], DATES[5])], "base_scans": 5, "scans": 6,
         "level": None, "rule": "every:3s", "why": "forced (-K)"},
    ]
