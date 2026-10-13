"""Exact (single-object) plan items through the sweep (specs/file-assign.md):
plan round-trip, manifest (gcs listing shards and cw layer-2), the `as_of`
hold, execute (dry and real) and undo. An exact item matches only its own key:
never `clip.mp3.bak`, never `clip.mp3/x`."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud.staged_plan import PlanError, StagedPlan, parse_plan, split_object
from dt_cloud.sweep import Plan, SweepError, build_manifest, execute_plan as cw_execute, load_plan as cw_load_plan
from dt_cloud.sweep_exec import execute_plan, list_roots, undo_run
from dt_cloud.sweep_manifest import build_manifests, minimal_items
from test_sweep import _FakeStore, _write_l2
from test_sweep_exec import FakeBlob, FakeClient, _UndoClient, _UndoHandle

B = "b1"
DATE, AS_OF = "2026-10-07", "2026-10-06"
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
CLIPS = "pod/show"
CLIP = f"{CLIPS}/clip.mp3"

# One listing: the exact item, its `.bak` sibling, a path under a folder that
# shares its name, an unrelated sibling, and a prefix item elsewhere.
ROWS = [
    # (name, size, generation)
    (CLIP, 1, 11),
    (f"{CLIP}.bak", 2, 12),
    (f"{CLIP}/x", 4, 13),
    (f"{CLIPS}/other.mp3", 8, 14),
    ("tmp/a", 16, 15),
    ("tmp/b/c", 32, 16),
    ("tmpx/d", 64, 17),
]
MIXED = {"plan_id": 5, "name": "Staged", "sweep": [f"gs://{B}/tmp/"], "objects": [f"gs://{B}/{CLIP}"]}


def _shard(root: Path, date: str, rows: list[tuple[str, int, int]], row_group_size: int = 2) -> None:
    d = root / "listing" / date / B
    d.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows)
    pq.write_table(pa.table({
        "name": pa.array([r[0] for r in rows], pa.string()),
        "size_bytes": pa.array([r[1] for r in rows], pa.int64()),
        "storage_class_id": pa.array([1] * len(rows), pa.int8()),
        "created": pa.array([T0] * len(rows), pa.timestamp("us", tz="UTC")),
        "generation": pa.array([r[2] for r in rows], pa.int64()),
    }), d / "0.parquet", row_group_size=row_group_size)


def _names(out: Path) -> list[tuple[str, int, int]]:
    t = pq.read_table(out / "manifest" / f"{B}.parquet")
    return [(r["name"], r["size_bytes"], r["generation"]) for r in t.to_pylist()]


# ── plan round-trip ──────────────────────────────────────────────────────────

def test_parse_plan_mixed_and_objects_only() -> None:
    assert parse_plan({**MIXED, "as_of": {f"gs://{B}/{CLIP}": AS_OF}}) == StagedPlan(
        plan_id=5, name="Staged", sweep={B: ("tmp/",)}, as_of={B: {CLIP: AS_OF}}, objects={B: (CLIP,)},
    )
    only = parse_plan({"plan_id": 6, "objects": [f"gs://{B}/{CLIP}", f"gs://e2/k"]})
    assert (only.sweep, only.objects, only.buckets, only.bands(B), only.exact(B)) == (
        {}, {B: (CLIP,), "e2": ("k",)}, ("b1", "e2"), (), (f"gs://{B}/{CLIP}",),
    )


@pytest.mark.parametrize(("plan", "error"), [
    ({"plan_id": 1, "objects": [f"gs://{B}/{CLIPS}/"]}, f"bad plan object 'gs://{B}/{CLIPS}/' (an exact key must not end in '/')"),
    ({"plan_id": 1, "objects": [f"gs://{B}/"]}, f"bad plan object 'gs://{B}/' (want gs://<bucket>/<key>)"),
    ({"plan_id": 1, "objects": [f"gs://{B}/a/../b"]}, f"bad plan object 'gs://{B}/a/../b' (empty, '.', '..' or backslash segment)"),
    ({"plan_id": 1, "sweep": [], "objects": []}, "plan has no items (sweep and objects both empty)"),
    ({**MIXED, "as_of": {f"gs://{B}/{CLIP}.bak": AS_OF}}, f"as_of names 'gs://{B}/{CLIP}.bak', which is not a plan item"),
])
def test_parse_plan_rejects(plan: dict, error: str) -> None:
    with pytest.raises(PlanError) as e:
        parse_plan(plan)
    assert str(e.value) == error


def test_split_object() -> None:
    assert split_object(f"gs://{B}/{CLIP}") == (B, CLIP)


def test_minimal_items_drops_exact_keys_under_a_band() -> None:
    assert minimal_items(["tmp/", "tmp/b/"], ["tmp/b/c", CLIP, "tmpx/d"]) == (["tmp/"], [CLIP, "tmpx/d"])


# ── manifest (gcs listing shards) ────────────────────────────────────────────

@pytest.mark.parametrize("row_group_size", [1, 2, 100])
def test_manifest_exact_item_is_one_key(tmp_path: Path, row_group_size: int) -> None:
    _shard(tmp_path / "root", DATE, ROWS, row_group_size)
    sp = parse_plan(MIXED)
    summary = build_manifests(str(tmp_path / "root"), DATE, dict(sp.sweep), str(tmp_path / "out"), workers=1, objects_by_bucket=dict(sp.objects))
    assert _names(tmp_path / "out") == [(CLIP, 1, 11), ("tmp/a", 16, 15), ("tmp/b/c", 32, 16)]
    assert summary[B] == {
        "objects": 7, "dirs": 3,
        "eligible": {"bytes": 1 + 16 + 32, "objects": 3},
        "outside_bands": {"bytes": 2 + 4 + 8 + 64, "objects": 4},
    }


def test_manifest_prefix_only_unchanged(tmp_path: Path) -> None:
    _shard(tmp_path / "root", DATE, ROWS)
    summary = build_manifests(str(tmp_path / "root"), DATE, {B: (f"{CLIPS}/",)}, str(tmp_path / "out"), workers=1)
    assert _names(tmp_path / "out") == [(CLIP, 1, 11), (f"{CLIP}.bak", 2, 12), (f"{CLIP}/x", 4, 13), (f"{CLIPS}/other.mp3", 8, 14)]
    assert summary[B]["eligible"] == {"bytes": 15, "objects": 4}


def test_manifest_as_of_pins_an_exact_item(tmp_path: Path) -> None:
    # AS_OF saw the clip at generation 11 and `other.mp3` at 99: the clip is
    # unchanged (kept), `other.mp3` was rewritten since (held back).
    root = tmp_path / "root"
    _shard(root, DATE, ROWS)
    _shard(root, AS_OF, [(CLIP, 1, 11), (f"{CLIP}.bak", 2, 12), (f"{CLIPS}/other.mp3", 8, 99)])
    sp = parse_plan({"plan_id": 7, "objects": [f"gs://{B}/{CLIP}", f"gs://{B}/{CLIPS}/other.mp3"],
                     "as_of": {f"gs://{B}/{CLIP}": AS_OF, f"gs://{B}/{CLIPS}/other.mp3": AS_OF}})
    summary = build_manifests(str(root), DATE, dict(sp.sweep), str(tmp_path / "out"), workers=1, as_of=sp.as_of, objects_by_bucket=dict(sp.objects))
    assert _names(tmp_path / "out") == [(CLIP, 1, 11)]
    assert summary[B]["skipped_after_as_of"] == {"bytes": 8, "objects": 1}


def test_cli_summary_lists_approved_objects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import cli

    monkeypatch.setattr(cli, "_hard_exit", lambda: None)
    _shard(tmp_path / "root", DATE, ROWS)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({**MIXED, "as_of": {f"gs://{B}/{CLIP}": AS_OF}}))
    _shard(tmp_path / "root", AS_OF, [(CLIP, 1, 11)])
    out = tmp_path / "out"
    r = CliRunner().invoke(cli.main, ["sweep", "manifest", "-d", DATE, "-p", str(plan), "-r", str(tmp_path / "root"), "-o", str(out)])
    assert (r.exit_code, r.exception) == (0, None)
    s = json.loads((out / "plan-summary.json").read_text())
    assert {k: s[k] for k in ("approved", "approved_objects", "as_of")} == {
        "approved": [f"gs://{B}/tmp/"],
        "approved_objects": [f"gs://{B}/{CLIP}"],
        "as_of": {f"gs://{B}/{CLIP}": AS_OF},
    }
    assert _names(out) == [(CLIP, 1, 11), ("tmp/a", 16, 15), ("tmp/b/c", 32, 16)]


# ── execute + undo (gcs) ─────────────────────────────────────────────────────

def _run_dir(tmp_path: Path) -> Path:
    """A manifest built from MIXED, then `plan-summary.json` as the CLI writes it."""
    root, out = tmp_path / "root", tmp_path / "run"
    _shard(root, DATE, ROWS)
    sp = parse_plan(MIXED)
    buckets = build_manifests(str(root), DATE, dict(sp.sweep), str(out), workers=1, objects_by_bucket=dict(sp.objects))
    (out / "plan-summary.json").write_text(json.dumps({
        "date": DATE, "plan_id": 5, "approved": list(sp.bands(B)), "approved_objects": list(sp.exact(B)), "as_of": {}, "buckets": buckets,
    }))
    return out


def _live(clip_generation: int = 11) -> FakeClient:
    # Live now: every scanned object (the clip at `clip_generation`), plus a
    # NEW sibling of the clip (not drift: its dir is exact-only) and a new key
    # under `tmp/` (drift for `tmp/b`).
    gens = {CLIP: clip_generation}
    blobs = [FakeBlob(n, s, gens.get(n, g), T0) for n, s, g in ROWS]
    return FakeClient(blobs={B: [*blobs, FakeBlob(f"{CLIPS}/new.mp3", 128, 18, T0), FakeBlob("tmp/b/new", 256, 19, T0)]})


def _log(run: Path, mode: str) -> list[tuple[str, str, int]]:
    t = pq.read_table(run / mode / B)
    return sorted((r["name"], r["decision"], r["generation"]) for r in t.to_pylist())


def test_execute_dry_exact_item(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    client = _live()
    s = execute_plan(str(run), client=client, workers=1)
    assert client.handle.deletes == []
    # `tmp/b` drifted (a new key under a prefix band) → its delete is skipped
    # and unlogged; the clip's new sibling is not drift
    assert _log(run, "would-delete") == [(CLIP, "delete", 11), ("tmp/a", "delete", 15)]
    b = s["buckets"][B]
    assert b["drift_dirs"] == [{"dir": "tmp/b", "new_objects": 1, "new_bytes": 256, "skipped_deletes": 1}]
    assert b["bands"] == {
        f"gs://{B}/{CLIP}": {"bytes": 1, "objects": 1},
        f"gs://{B}/tmp/": {"bytes": 16, "objects": 1, "drift_new_objects": 1},
    }


def test_execute_real_deletes_only_the_exact_generation(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    client = _live()
    s = execute_plan(str(run), for_real=True, client=client, workers=1)
    assert sorted(client.handle.deletes) == [(CLIP, 11), ("tmp/a", 15)]
    assert s["buckets"][B]["decisions"] == {"delete": 2}
    # the clip's dir is its own listing root (not its top segment `pod/`);
    # `tmp` holds a manifest object directly, so the band itself is the root
    assert sorted(client.listed) == [f"{CLIPS}/", "tmp/"]
    assert list_roots({CLIPS, "tmp", "tmp/b"}, (f"gs://{B}/tmp/",), B, {CLIPS}) == [CLIPS, "tmp"]


def test_execute_skips_an_overwritten_exact_item(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    client = _live(clip_generation=77)
    s = execute_plan(str(run), for_real=True, client=client, workers=1)
    assert sorted(client.handle.deletes) == [("tmp/a", 15)]
    assert _log(run, "deleted") == [(CLIP, "skipped_overwritten", 77), ("tmp/a", "delete", 15)]
    assert s["buckets"][B]["bands"][f"gs://{B}/{CLIP}"] == {"overwritten": 1}


def test_undo_restores_the_exact_item(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    execute_plan(str(run), for_real=True, drift="proceed", client=_live(), workers=1)
    h = _UndoHandle()
    s = undo_run(str(run), client=_UndoClient(h), workers=1, now=int(T0.timestamp()) + 86400)
    assert sorted(h.calls) == [(CLIP, 11, 0), ("tmp/a", 15, 0), ("tmp/b/c", 16, 0)]
    assert s["buckets"][B]["bands"] == {
        f"gs://{B}/{CLIP}": {"restored": 1, "bytes": 1},
        f"gs://{B}/tmp/": {"restored": 2, "bytes": 48},
    }


# ── cw (S3, layer-2) ─────────────────────────────────────────────────────────

CW = "marin-us-east-02a"


def test_cw_load_plan_objects(tmp_path: Path) -> None:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"plan_id": 3, "name": "p", "bucket": CW, "sweep": [], "objects": [f"s3://{CW}/{CLIP}"], "as_of": {f"s3://{CW}/{CLIP}": AS_OF}}))
    assert cw_load_plan(p) == Plan(name="p", bucket=CW, sweep=[], plan_id=3, as_of={CLIP: AS_OF}, objects=[CLIP])
    with pytest.raises(SweepError):
        Plan(name="slash", bucket=CW, sweep=[], objects=[f"{CLIPS}/"]).validate()


def _cw_run(tmp_path: Path, as_of_l2: list | None = None) -> tuple[Path, dict]:
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, [(n, s, 100 + g, "file") for n, s, g in ROWS] + [(CLIPS, 15, 0, "dir")])
    held = {}
    if as_of_l2 is not None:
        _write_l2(tmp_path / "asof.parquet", as_of_l2)
        held = {CLIP: AS_OF}
    plan = Plan(name="p", bucket=CW, sweep=["tmp/"], plan_id=3, objects=[CLIP, f"{CLIPS}/other.mp3"], as_of=held)
    out = tmp_path / "run"
    summary = build_manifest(str(l2), plan, str(out), date=DATE, l2_for=lambda _scan: str(tmp_path / "asof.parquet"))
    return out, summary


def _cw_manifest(out: Path) -> list[str]:
    return [r[0] for r in duckdb.connect().execute(f"SELECT name FROM read_parquet('{out}/manifest/{CW}.parquet') ORDER BY name").fetchall()]


def test_cw_manifest_exact_items(tmp_path: Path) -> None:
    out, summary = _cw_run(tmp_path)
    assert _cw_manifest(out) == [CLIP, f"{CLIPS}/other.mp3", "tmp/a", "tmp/b/c"]
    assert (summary["objects"], summary["bytes"], summary["approved_objects"]) == (4, 1 + 8 + 16 + 32, [CLIP, f"{CLIPS}/other.mp3"])


def test_cw_manifest_as_of_exact(tmp_path: Path) -> None:
    # the clip's AS_OF mtime differs → held back; other.mp3 is as of the pinned scan
    out, summary = _cw_run(tmp_path, as_of_l2=[(CLIP, 1, 5, "file")])
    assert _cw_manifest(out) == [f"{CLIPS}/other.mp3", "tmp/a", "tmp/b/c"]
    assert summary["skipped_after_as_of"] == {"objects": 1, "bytes": 1}


def test_cw_execute_exact_items(tmp_path: Path) -> None:
    out, _ = _cw_run(tmp_path)
    live = {n: (s, 100 + g) for n, s, g in ROWS}
    live[f"{CLIPS}/new.mp3"] = (128, 200)  # new sibling of exact items: not drift
    store = _FakeStore(live, versioning="Enabled")
    s = cw_execute(str(out), for_real=True, client=store)
    assert sorted(set(live) - set(store.objects)) == [CLIP, f"{CLIPS}/other.mp3", "tmp/a", "tmp/b/c"]
    assert (s["deleted_objects"], s["drift_new"]) == (4, 0)
    assert s["bands"] == [
        {"prefix": CLIP, "bytes": 1, "objects": 1, "gone": 0, "overwritten": 0, "drift_new": 0},
        {"prefix": f"{CLIPS}/other.mp3", "bytes": 8, "objects": 1, "gone": 0, "overwritten": 0, "drift_new": 0},
        {"prefix": "tmp/", "bytes": 48, "objects": 2, "gone": 0, "overwritten": 0, "drift_new": 0},
    ]
