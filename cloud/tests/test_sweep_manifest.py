"""Parallel, row-group-pruned staged-manifest construction."""

from __future__ import annotations

import datetime as dt
import json
import random
import threading
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from pyarrow import fs as pafs

from dt_cloud.staged_plan import CATEGORIES, StagedPlan, parse_plan
from dt_cloud.sweep_manifest import MANIFEST_SCHEMA, ManifestProgress, build_manifests, minimal_bands, scan_shard

DATE = "2026-09-01"
B1, B2, B3 = "data-us-central2", "data-eu-west4", "data-us-west4"
PLAN = {
    "plan_id": 7,
    "name": "Staged",
    "sweep": [
        f"gs://{B1}/ckpt/old/",
        f"gs://{B1}/ckpt/old/run3/",
        f"gs://{B1}/tmp/",
        f"gs://{B1}/données/é/",
        f"gs://{B2}/x/",
        f"gs://{B3}/nothing-here/",
    ],
}


def legacy_manifest(root: Path, date: str, plan: StagedPlan, buckets: list[str], out: Path) -> dict:
    """The serial pandas implementation replaced by ``build_manifests``."""
    result = {}
    for bucket in buckets:
        shards = sorted((root / "listing" / date / bucket).glob("*.parquet"))
        cache: dict[str, str] = {}
        categories = {category: [0, 0] for category in CATEGORIES}
        bands = plan.sweep[bucket]
        writer = None
        (out / "manifest").mkdir(parents=True, exist_ok=True)
        objects = 0
        for shard in shards:
            parquet = pq.ParquetFile(shard)
            columns = ["name", "size_bytes", "storage_class_id", "created"]
            if "generation" in parquet.schema.names:
                columns.append("generation")
            for batch in parquet.iter_batches(columns=columns, batch_size=5):
                frame = batch.to_pandas()
                objects += len(frame)
                in_band = frame["name"].str.startswith(bands)
                if not in_band.all():
                    categories["outside_bands"][0] += int(frame["size_bytes"][~in_band].sum())
                    categories["outside_bands"][1] += int((~in_band).sum())
                    frame = frame[in_band]
                    if frame.empty:
                        continue
                dirs = frame["name"].str.rpartition("/")[0]
                for dirname in dirs.unique():
                    if dirname not in cache:
                        cache[dirname] = plan.classify(bucket, dirname)
                category = dirs.map(lambda dirname: cache[dirname])
                sizes = frame["size_bytes"]
                for name, group in sizes.groupby(category):
                    categories[name][0] += int(group.sum())
                    categories[name][1] += len(group)
                eligible = category == "eligible"
                if eligible.any():
                    selected = frame[eligible].copy()
                    selected["dir"] = dirs[eligible]
                    if "generation" not in selected:
                        selected["generation"] = None
                    table = pa.Table.from_pandas(selected, preserve_index=False).select(MANIFEST_SCHEMA.names).cast(MANIFEST_SCHEMA)
                    if writer is None:
                        writer = pq.ParquetWriter(out / "manifest" / f"{bucket}.parquet", MANIFEST_SCHEMA)
                    writer.write_table(table)
        if writer is not None:
            writer.close()
        result[bucket] = {
            "objects": objects,
            "dirs": len(cache),
            **{
                category: {"bytes": size, "objects": count}
                for category, (size, count) in categories.items()
                if count
            },
        }
    return result


def _write(path: Path, names: list[str], rng: random.Random, stats: bool = True) -> None:
    instant = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    table = pa.table({
        "bucket": pa.array(["b"] * len(names), pa.large_string()),
        "name": pa.array(names, pa.large_string()),
        "size_bytes": pa.array([rng.randrange(0, 10_000) for _ in names], pa.int64()),
        "created": pa.array(
            [instant + dt.timedelta(seconds=rng.randrange(10**6)) for _ in names],
            pa.timestamp("us", tz="UTC"),
        ),
        "storage_class_id": pa.array([rng.randrange(0, 5) for _ in names], pa.int64()),
        "generation": pa.array([rng.randrange(1, 10**15) for _ in names], pa.int64()),
    })
    pq.write_table(table, path, row_group_size=4, write_statistics=stats)


def _keys(rng: random.Random, tops: list[str], n: int) -> list[str]:
    segments = ["a", "b", "run3", "run30", "é", "z~", "0"]
    out = []
    for _ in range(n):
        top = rng.choice(tops)
        depth = rng.randrange(0, 3)
        name = top + "".join(rng.choice(segments) + "/" for _ in range(depth))
        out.append(name if rng.random() < 0.1 else name + f"f{rng.randrange(1000)}")
    return out


@pytest.fixture
def listing(tmp_path: Path) -> Path:
    rng = random.Random(42)
    root = tmp_path / "root"
    tops = {
        B1: ["ckpt/old/", "ckpt/older/", "ckpt/old/run3/", "ckpt/", "tmp/", "tmp", "tmpx/", "données/é/", "données/", "", "zz/"],
        B2: ["x/", "x", "y/", "", "w/x/"],
        B3: ["a/", "b/"],
    }
    for bucket, bucket_tops in tops.items():
        directory = root / "listing" / DATE / bucket
        directory.mkdir(parents=True)
        for i in range(5):
            names = sorted(_keys(rng, bucket_tops, 40)) + sorted(_keys(rng, bucket_tops, 25))
            _write(directory / f"shard-{i:02d}.parquet", [name for name in names if name], rng, stats=i != 3)
    return root


def test_minimal_bands_drops_nested() -> None:
    assert minimal_bands(("a/b/", "a/", "c/", "a/c/", "c/")) == ["a/", "c/"]


def test_progress_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import sweep_manifest

    monkeypatch.setattr(sweep_manifest.time, "monotonic", lambda: 10.0)
    progress = ManifestProgress(4, 8)
    progress.set_phase("writing", B1)
    progress.total = 20
    progress.scanned = 12
    progress.written = 9
    progress.objects = 100_000
    progress.eligible = 20_000
    progress.active = {"new.parquet": 15.0, "old.parquet": 11.0}
    monkeypatch.setattr(sweep_manifest.time, "monotonic", lambda: 40.0)
    assert progress.message() == (
        "manifest progress: 30s elapsed · phase=writing (30s) data-us-central2"
        " · shards scanned=12/20, written=9/20 · 100,000 input objects, 20,000 eligible"
        " · active readers=2/4, window=8 · oldest reader=old.parquet (29s)"
        " · scan worker-seconds=0.0, write-seconds=0.0"
    )


@pytest.mark.parametrize("blocked", ["scan", "write"])
def test_progress_reports_while_io_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked: str,
) -> None:
    from dt_cloud import sweep_manifest

    root = tmp_path / "input"
    shard = root / "listing" / DATE / B2 / "shard-00.parquet"
    shard.parent.mkdir(parents=True)
    _write(shard, ["x/a"], random.Random(42))
    phase = "waiting-for-shard" if blocked == "scan" else "writing"
    release = threading.Event()
    observed = threading.Event()
    messages: list[str] = []
    failures: list[BaseException] = []
    reporters: list[ManifestProgress] = []
    original_scan = sweep_manifest.scan_shard
    original_write = pq.ParquetWriter.write_table

    def wait_scan(*args, **kwargs) -> object:
        assert release.wait(5)
        return original_scan(*args, **kwargs)

    def wait_write(self, *args, **kwargs) -> None:
        assert release.wait(5)
        original_write(self, *args, **kwargs)

    def report(self: ManifestProgress) -> None:
        reporters.append(self)
        with self.lock:
            matches = self.phase == phase and (blocked != "scan" or len(self.active) == 1)
        if matches and not observed.is_set():
            messages.append(self.message())
            observed.set()

    def run() -> None:
        try:
            build_manifests(str(root), DATE, {B2: ("x/",)}, str(tmp_path / "out"), workers=1, window=1)
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(sweep_manifest.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(sweep_manifest, "PROGRESS_EVERY", 0.01)
    monkeypatch.setattr(ManifestProgress, "report", report)
    if blocked == "scan":
        monkeypatch.setattr(sweep_manifest, "scan_shard", wait_scan)
    else:
        monkeypatch.setattr(pq.ParquetWriter, "write_table", wait_write)
    build_thread = threading.Thread(target=run)
    build_thread.start()
    try:
        assert observed.wait(3)
        scanned = 0 if blocked == "scan" else 1
        current = str(shard) if blocked == "scan" else B2
        oldest = f"{shard} (0s)" if blocked == "scan" else "-"
        active = 1 if blocked == "scan" else 0
        assert messages == [
            f"manifest progress: 0s elapsed · phase={phase} (0s) {current}"
            f" · shards scanned={scanned}/1, written=0/1 · {scanned} input objects, {scanned} eligible"
            f" · active readers={active}/1, window=1 · oldest reader={oldest}"
            " · scan worker-seconds=0.0, write-seconds=0.0"
        ]
    finally:
        release.set()
        build_thread.join(5)
    assert build_thread.is_alive() is False
    assert failures == []
    assert reporters[-1].thread.is_alive() is False
    assert reporters[-1].phase == "done"


def test_progress_stops_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ManifestProgress, "report", lambda self: None)
    progress = ManifestProgress(1, 1)
    with pytest.raises(RuntimeError, match="^failed read$"):
        with progress:
            raise RuntimeError("failed read")
    assert progress.thread.is_alive() is False
    assert progress.phase == "failed"


def test_parallel_matches_legacy(listing: Path, tmp_path: Path) -> None:
    plan = parse_plan(PLAN)
    buckets = list(plan.buckets)
    old = legacy_manifest(listing, DATE, plan, buckets, tmp_path / "old")
    new = build_manifests(
        str(listing),
        DATE,
        {bucket: plan.sweep[bucket] for bucket in buckets},
        str(tmp_path / "new"),
        workers=4,
        window=3,
    )
    assert list(new) == buckets
    assert new == old
    assert {
        bucket: sorted(set(value) - {"objects", "dirs"})
        for bucket, value in new.items()
    } == {
        B2: ["eligible", "outside_bands"],
        B1: ["eligible", "outside_bands"],
        B3: ["outside_bands"],
    }
    expected_files = [f"{B2}.parquet", f"{B1}.parquet"]
    assert sorted(path.name for path in (tmp_path / "new" / "manifest").iterdir()) == expected_files
    assert sorted(path.name for path in (tmp_path / "old" / "manifest").iterdir()) == expected_files
    for bucket in (B1, B2):
        old_table = pq.read_table(tmp_path / "old" / "manifest" / f"{bucket}.parquet")
        new_table = pq.read_table(tmp_path / "new" / "manifest" / f"{bucket}.parquet")
        assert new_table.schema == old_table.schema == MANIFEST_SCHEMA
        assert new_table.to_pylist() == old_table.to_pylist()


@pytest.mark.parametrize("fs_type", ["gcs", "s3"])
def test_object_store_output_needs_no_directory_creation(
    listing: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fs_type: str,
) -> None:
    from dt_cloud import sweep_manifest

    plan = parse_plan(PLAN)
    buckets = list(plan.buckets)
    expected = legacy_manifest(listing, DATE, plan, buckets, tmp_path / "expected")
    out = f"{fs_type}://artifacts/run"
    out_path = "artifacts/run"
    outputs: dict[str, pa.BufferOutputStream] = {}
    resolve_fs = sweep_manifest.resolve_fs
    parquet_writer = pq.ParquetWriter

    def create_dir(path: str, recursive: bool) -> None:
        raise PermissionError("object-only account cannot read or create buckets")

    cloud_fs = SimpleNamespace(type_name=fs_type, create_dir=create_dir)

    def resolve(url: str) -> tuple[object, str]:
        return (cloud_fs, out_path) if url == out else resolve_fs(url)

    def writer(
        path: str,
        schema: pa.Schema,
        filesystem: object,
    ) -> pq.ParquetWriter:
        assert filesystem is cloud_fs
        outputs[path] = pa.BufferOutputStream()
        return parquet_writer(outputs[path], schema)

    monkeypatch.setattr(sweep_manifest, "resolve_fs", resolve)
    monkeypatch.setattr(sweep_manifest.pq, "ParquetWriter", writer)
    actual = build_manifests(
        str(listing),
        DATE,
        {bucket: plan.sweep[bucket] for bucket in buckets},
        out,
        workers=4,
    )
    assert actual == expected
    assert sorted(outputs) == [
        f"{out_path}/manifest/{B2}.parquet",
        f"{out_path}/manifest/{B1}.parquet",
    ]
    for bucket in (B1, B2):
        table = pq.read_table(pa.BufferReader(outputs[f"{out_path}/manifest/{bucket}.parquet"].getvalue()))
        expected_table = pq.read_table(tmp_path / "expected" / "manifest" / f"{bucket}.parquet")
        assert table.schema == expected_table.schema == MANIFEST_SCHEMA
        assert table.to_pylist() == expected_table.to_pylist()


def test_pruning_reads_only_sizes_outside_bands(listing: Path) -> None:
    directory = listing / "listing" / DATE / B1
    fs = pafs.LocalFileSystem()
    bands = minimal_bands(parse_plan(PLAN).sweep[B1])
    pruned = {}
    for path in sorted(directory.glob("*.parquet")):
        result = scan_shard(fs, str(path), bands)
        metadata = pq.ParquetFile(path).metadata
        assert (result.objects, result.elig_objects + result.out_objects) == (metadata.num_rows, metadata.num_rows)
        assert result.elig_bytes + result.out_bytes == sum(pq.read_table(path, columns=["size_bytes"])["size_bytes"].to_pylist())
        pruned[path.name] = result.pruned_groups > 0
    assert pruned == {
        "shard-00.parquet": True,
        "shard-01.parquet": True,
        "shard-02.parquet": True,
        "shard-03.parquet": False,
        "shard-04.parquet": True,
    }


def test_cli_writes_summary(listing: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import cli

    monkeypatch.setattr(cli, "_hard_exit", lambda: None)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(PLAN))
    out = tmp_path / "out"
    result = CliRunner().invoke(
        cli.main,
        ["sweep", "manifest", "-d", DATE, "--plan", str(plan_path), "-o", str(out), "-r", str(listing), "-j", "3"],
    )
    assert (result.exit_code, result.exception) == (0, None)
    plan = parse_plan(PLAN)
    old = legacy_manifest(listing, DATE, plan, list(plan.buckets), tmp_path / "old")
    totals = {
        category: {
            "bytes": sum(value[category]["bytes"] for value in old.values() if category in value),
            "objects": sum(value[category]["objects"] for value in old.values() if category in value),
        }
        for category in CATEGORIES
        if any(category in value for value in old.values())
    }
    assert json.loads((out / "plan-summary.json").read_text()) == {
        "date": DATE,
        "plan_id": 7,
        "plan_name": "Staged",
        "approved": [approved for bucket in plan.buckets for approved in plan.bands(bucket)],
        "as_of": {},
        "buckets": old,
        "total": totals,
    }


# ── `as_of`: an item staged against an earlier scan holds back what changed since ──

AS_OF, NOW = "2026-10-06", "2026-10-07"
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


def _shard(path: Path, rows: list[tuple[str, int, int | None, float]], generation: bool = True) -> None:
    """One listing shard of ``(name, size, generation, created offset s)`` rows
    (``generation=False``: an old listing without the column)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows)
    cols = {
        "bucket": pa.array([B1] * len(rows), pa.large_string()),
        "name": pa.array([r[0] for r in rows], pa.large_string()),
        "size_bytes": pa.array([r[1] for r in rows], pa.int64()),
        "created": pa.array([T0 + dt.timedelta(seconds=r[3]) for r in rows], pa.timestamp("us", tz="UTC")),
        "storage_class_id": pa.array([1] * len(rows), pa.int64()),
    }
    if generation:
        cols["generation"] = pa.array([r[2] for r in rows], pa.int64())
    pq.write_table(pa.table(cols), path, row_group_size=2)


# The dispatch scan: `ckpt/a/` was staged as of AS_OF, `ckpt/b/` as of NOW,
# `tmp/` before `as_of` existed (none).
NOW_ROWS = [
    ("ckpt/a/kept", 1, 11, 0),         # same generation in AS_OF → deleted
    ("ckpt/a/rewritten", 2, 99, 50),   # AS_OF had generation 12 → held back
    ("ckpt/a/new", 4, 31, 60),         # not in AS_OF → held back
    ("ckpt/a/sub/kept", 8, 14, 0),     # same generation → deleted
    ("ckpt/b/new", 16, 41, 60),        # `ckpt/b/` is as of NOW → deleted
    ("ckpt/bb/x", 32, 42, 0),          # outside every band
    ("tmp/new", 64, 51, 60),           # no `as_of` → deleted
]
AS_OF_ROWS = [
    ("ckpt/a/kept", 1, 11, 0),
    ("ckpt/a/rewritten", 2, 12, 0),
    ("ckpt/a/gone", 128, 13, 0),       # deleted since: not in NOW, so not in the manifest
    ("ckpt/a/sub/kept", 8, 14, 0),
    ("ckpt/b/old", 256, 21, 0),
]
AS_OF_PLAN = {
    "plan_id": 9,
    "name": "Staged",
    "sweep": [f"gs://{B1}/ckpt/a/", f"gs://{B1}/ckpt/b/", f"gs://{B1}/tmp/"],
    "as_of": {f"gs://{B1}/ckpt/a/": AS_OF, f"gs://{B1}/ckpt/b/": NOW},
}


def _as_of_root(tmp_path: Path, as_of_rows: list, generation: bool = True) -> Path:
    root = tmp_path / "root"
    _shard(root / "listing" / NOW / B1 / "shard-00.parquet", NOW_ROWS[:4])
    _shard(root / "listing" / NOW / B1 / "shard-01.parquet", NOW_ROWS[4:])
    _shard(root / "listing" / AS_OF / B1 / "shard-00.parquet", as_of_rows, generation=generation)
    return root


def _built(root: Path, out: Path, plan: dict = AS_OF_PLAN) -> tuple[dict, list[tuple[str, int | None]]]:
    sp = parse_plan(plan)
    summary = build_manifests(str(root), NOW, dict(sp.sweep), str(out), workers=2, window=2, as_of=sp.as_of)
    table = pq.read_table(out / "manifest" / f"{B1}.parquet")
    assert table.schema == MANIFEST_SCHEMA
    return summary, [(r["name"], r["generation"]) for r in table.to_pylist()]


def test_as_of_holds_back_what_changed_after_the_staging_scan(tmp_path: Path) -> None:
    summary, rows = _built(_as_of_root(tmp_path, AS_OF_ROWS), tmp_path / "out")
    assert rows == [("ckpt/a/kept", 11), ("ckpt/a/sub/kept", 14), ("ckpt/b/new", 41), ("tmp/new", 51)]
    assert summary == {B1: {
        "objects": 7,
        "dirs": 4,
        "eligible": {"bytes": 1 + 8 + 16 + 64, "objects": 4},
        "outside_bands": {"bytes": 32, "objects": 1},
        "skipped_after_as_of": {"bytes": 2 + 4, "objects": 2},
    }}


def test_as_of_without_generations_falls_back_to_created(tmp_path: Path) -> None:
    # An old `as_of` listing: no generation column, so identity is `created`
    # within 1 s (`ckpt/a/rewritten` was created 50 s later in NOW).
    as_of_rows = [("ckpt/a/kept", 1, None, 0.5), ("ckpt/a/rewritten", 2, None, 0), ("ckpt/a/sub/kept", 8, None, 1.5)]
    summary, rows = _built(_as_of_root(tmp_path, as_of_rows, generation=False), tmp_path / "out")
    assert rows == [("ckpt/a/kept", 11), ("ckpt/b/new", 41), ("tmp/new", 51)]
    assert summary[B1]["skipped_after_as_of"] == {"bytes": 2 + 4 + 8, "objects": 3}


def test_as_of_the_dispatch_scan_reads_no_other_listing(tmp_path: Path) -> None:
    # Every item as of NOW (or none): no AS_OF listing exists, and none is needed.
    root = _as_of_root(tmp_path, AS_OF_ROWS)
    for shard in (root / "listing" / AS_OF / B1).iterdir():
        shard.unlink()
    plan = {**AS_OF_PLAN, "as_of": {f"gs://{B1}/ckpt/a/": NOW}}
    summary, rows = _built(root, tmp_path / "out", plan)
    assert rows == [("ckpt/a/kept", 11), ("ckpt/a/new", 31), ("ckpt/a/rewritten", 99), ("ckpt/a/sub/kept", 14), ("ckpt/b/new", 41), ("tmp/new", 51)]
    assert sorted(summary[B1]) == ["dirs", "eligible", "objects", "outside_bands"]


def test_as_of_listing_missing_is_an_error(tmp_path: Path) -> None:
    root = _as_of_root(tmp_path, AS_OF_ROWS)
    plan = {**AS_OF_PLAN, "as_of": {f"gs://{B1}/ckpt/a/": "2026-10-01"}}
    with pytest.raises(SystemExit, match=rf"^no listing shards for {B1} under {root}/listing/2026-10-01/$"):
        _built(root, tmp_path / "out", plan)


def test_as_of_nested_items_are_a_union(tmp_path: Path) -> None:
    # `ckpt/` as of NOW covers `ckpt/a/` (as of AS_OF): everything under it stays.
    plan = {**AS_OF_PLAN, "sweep": [*AS_OF_PLAN["sweep"], f"gs://{B1}/ckpt/"], "as_of": {**AS_OF_PLAN["as_of"], f"gs://{B1}/ckpt/": NOW}}
    summary, rows = _built(_as_of_root(tmp_path, AS_OF_ROWS), tmp_path / "out", plan)
    assert [name for name, _ in rows] == ["ckpt/a/kept", "ckpt/a/new", "ckpt/a/rewritten", "ckpt/a/sub/kept", "ckpt/b/new", "ckpt/bb/x", "tmp/new"]
    assert sorted(summary[B1]) == ["dirs", "eligible", "objects"]


def test_cli_summary_names_held_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import cli

    monkeypatch.setattr(cli, "_hard_exit", lambda: None)
    root = _as_of_root(tmp_path, AS_OF_ROWS)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(AS_OF_PLAN))
    out = tmp_path / "out"
    result = CliRunner().invoke(cli.main, ["sweep", "manifest", "-d", NOW, "--plan", str(plan_path), "-o", str(out), "-r", str(root), "-j", "2"])
    assert (result.exit_code, result.exception) == (0, None)
    summary = json.loads((out / "plan-summary.json").read_text())
    assert {k: summary[k] for k in ("as_of", "total")} == {
        "as_of": {f"gs://{B1}/ckpt/a/": AS_OF},
        "total": {
            "eligible": {"bytes": 89, "objects": 4},
            "outside_bands": {"bytes": 32, "objects": 1},
            "skipped_after_as_of": {"bytes": 6, "objects": 2},
        },
    }
