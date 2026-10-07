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

    def wait_scan(*args) -> object:
        assert release.wait(5)
        return original_scan(*args)

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
    }
    assert json.loads((out / "plan-summary.json").read_text()) == {
        "date": DATE,
        "plan_id": 7,
        "plan_name": "Staged",
        "approved": [approved for bucket in plan.buckets for approved in plan.bands(bucket)],
        "buckets": old,
        "total": totals,
    }
