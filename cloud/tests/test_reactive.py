"""Per-CSV ingest driven by a list-based drain (`dt_cloud.reactive`).

`test_feedback_loop_*` are the load-bearing ones: the drain sweeps each
processed CSV into `ingested/` in the *same bucket*. If those names were ever
classified as ingestable, the drain would find its own outputs and grind
forever.

`test_unprocessed_*` pin the other load-bearing piece: with no watermark, lease
or queue, that set difference *is* the scheduler.
"""

from __future__ import annotations

import pytest

from dt_cloud.reactive import (
    INGESTED_PREFIX,
    LAYERS,
    MIN_WORKER_MB,
    USAGE_PREFIX,
    Skip,
    Target,
    classify,
    closed_days,
    drain,
    group_by_day,
    l0_path,
    split_memory,
    unprocessed,
)

CSV = "marin-us-central1_usage_2026_08_23_05_00_00_0dda25dbdfc3ab52ed_v0"
NAME = f"{USAGE_PREFIX}{CSV}"

TARGET = Target(
    name=NAME,
    basename=CSV,
    bucket="marin-us-central1",
    ts="2026_08_23_05_00_00",
)


# ---------- classify ----------

def test_classify_accepts_a_delivered_usage_csv():
    assert classify(NAME) == TARGET


def test_feedback_loop_ingested_prefix_is_never_ingestable():
    """The sweep destination must not classify as work, or ingest self-triggers."""
    assert classify(f"{INGESTED_PREFIX}{CSV}") == Skip("not under 'usage/'")


def test_feedback_loop_ingested_prefix_is_outside_usage_prefix():
    """Structural guarantee behind the test above — not merely a naming accident."""
    assert not INGESTED_PREFIX.startswith(USAGE_PREFIX)


def test_classify_rejects_storage_byte_hour_files():
    """`_storage_` files share the usage/ prefix but are a different schema."""
    name = f"{USAGE_PREFIX}marin-us-central1_storage_2026_08_23_00_00_00_abc_v0"
    assert classify(name) == Skip("no '_usage_' (storage-byte-hours file?)")


@pytest.mark.parametrize("name,reason", [
    ("access/raw/x.parquet", "not under 'usage/'"),
    ("usage/nested/dir_usage_2026_08_23_05_00_00_a_v0", "nested under usage/, not a delivered log"),
    ("usage/_usage_2026_08_23_05_00_00_a_v0", "empty source-bucket prefix"),
    ("usage/bucket_usage_not_a_timestamp_v0", "no parseable timestamp"),
])
def test_classify_skip_reasons(name: str, reason: str):
    assert classify(name) == Skip(reason)


# ---------- l0_path ----------

def test_l0_paths_are_a_pure_function_of_the_input_name():
    assert [l0_path(layer, TARGET.bucket, TARGET.basename) for layer in LAYERS] == [
        f"access/raw/marin-us-central1/l0/{CSV}.parquet",
        f"access/agg/marin-us-central1/l0/{CSV}.parquet",
        f"access/sizes/marin-us-central1/l0/{CSV}.parquet",
    ]


def test_l0_path_rejects_unknown_layer():
    with pytest.raises(ValueError, match="unknown layer"):
        l0_path("nope", "b", "x")


# ---------- memory budgeting ----------

@pytest.mark.parametrize("limit,workers,expected", [
    ("8GB", 4, "2048MB"),
    ("8GB", 1, "8192MB"),
    ("24GB", 6, "4096MB"),
    ("1536MB", 3, "512MB"),
    ("2gb", 2, "1024MB"),
])
def test_split_memory_divides_the_budget(limit: str, workers: int, expected: str):
    """Per-connection limit x N workers is what actually gets committed."""
    assert split_memory(limit, workers) == expected


def test_split_memory_floors_rather_than_starving_workers():
    assert split_memory("1GB", 16) == f"{MIN_WORKER_MB}MB"


def test_split_memory_rejects_an_unparseable_limit():
    with pytest.raises(ValueError, match="unparseable memory limit '8 gigs'"):
        split_memory("8 gigs", 2)


# ---------- the work list ----------

def _csv(hh: str, tag: str = "a") -> str:
    return f"marin-us-central1_usage_2026_08_23_{hh}_00_00_{tag}_v0"


FLOOR = "2026_08_23_12_00_00"


def test_unprocessed_finds_the_gap():
    """A CSV delivered before the floor with no L0 shard is the whole point."""
    usage = {_csv("08"), _csv("09"), _csv("10")}
    l0 = {_csv("08"), _csv("10")}
    assert unprocessed(usage, l0, FLOOR) == [_csv("09")]


def test_unprocessed_is_empty_when_everything_landed():
    names = {_csv("08"), _csv("09")}
    assert unprocessed(names, names, FLOOR) == []


def test_unprocessed_holds_back_names_at_or_after_the_floor():
    usage = {_csv("11"), _csv("12"), _csv("13")}
    assert unprocessed(usage, set(), FLOOR) == [_csv("11")]


def test_unprocessed_without_a_floor_takes_everything():
    """The drain's default: GCS listings only show finalized objects, so there
    is nothing to wait for and holding a window back just adds latency."""
    usage = {_csv("11"), _csv("12"), _csv("13")}
    assert unprocessed(usage, set(), "") == [_csv("11"), _csv("12"), _csv("13")]


def test_unprocessed_ignores_l0_shards_with_no_surviving_csv():
    """Swept CSVs leave usage/ but keep their L0 shard — not a gap."""
    assert unprocessed(set(), {_csv("08"), _csv("09")}, FLOOR) == []


def test_unprocessed_skips_unparseable_names_rather_than_dispatching_them():
    """No timestamp sorts as '' < floor, but such a name is not ingestable —
    classify() would Skip it, so flagging it would loop forever."""
    assert unprocessed({"garbage-name"}, set(), FLOOR) == []


def test_unprocessed_is_sorted_for_deterministic_dispatch_order():
    usage = {_csv("10"), _csv("08"), _csv("09")}
    assert unprocessed(usage, set(), FLOOR) == [_csv("08"), _csv("09"), _csv("10")]


def test_unprocessed_skips_storage_files_sitting_in_usage():
    """`_storage_` files live under usage/ forever and never get an L0 shard;
    flagging them would re-dispatch every run without bound."""
    stor = "marin-us-central1_storage_2026_08_23_00_00_00_a_v0"
    assert unprocessed({stor, _csv("09")}, set(), FLOOR) == [_csv("09")]


# ---------- compaction grouping ----------

def test_group_by_day_keys_on_log_hour_not_delivery():
    names = [_csv("23", "b"), _csv("00", "a"), "marin-us-central1_usage_2026_08_24_01_00_00_c_v0"]
    assert group_by_day(names) == {
        "2026_08_23": [_csv("00", "a"), _csv("23", "b")],
        "2026_08_24": ["marin-us-central1_usage_2026_08_24_01_00_00_c_v0"],
    }


def test_group_by_day_drops_unparseable_names():
    assert group_by_day(["garbage", _csv("05")]) == {"2026_08_23": [_csv("05")]}


def test_closed_days_excludes_today():
    by_day = {"2026_08_21": [], "2026_08_22": [], "2026_08_23": []}
    assert closed_days(by_day, "2026_08_23") == ["2026_08_21", "2026_08_22"]


def test_closed_days_is_empty_when_nothing_is_old_enough():
    assert closed_days({"2026_08_23": []}, "2026_08_23") == []


# ---------- drain ----------

class _Blob:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeClient:
    """Just enough GCS to answer the two listings `pending` makes per bucket."""

    def __init__(self, log_bucket: str, usage: list[str], l0: list[str]) -> None:
        self.log_bucket, self.usage, self.l0 = log_bucket, usage, l0

    def list_blobs(self, bucket: str, prefix: str = "", **kw):
        names = self.usage if bucket == self.log_bucket else self.l0
        return [_Blob(n) for n in names if n.startswith(prefix)]


def _other(hh: str) -> str:
    return f"marin-us-west4_usage_2026_08_23_{hh}_00_00_a_v0"


def _client(usage: list[str], l0: list[str]) -> _FakeClient:
    return _FakeClient(
        "logs",
        [f"{USAGE_PREFIX}{n}" for n in usage],
        [f"access/raw/{n.split('_usage_')[0]}/l0/{n}.parquet" for n in l0],
    )


def _run(monkeypatch, client, buckets, seen: list, fail: set = frozenset(), **kw) -> dict:
    def fake_ingest_one(_client, target, **kwargs):
        seen.append((target.basename, kwargs["memory_limit"]))
        if target.basename in fail:
            raise RuntimeError("bad CSV")
        return {"bucket": target.bucket, "basename": target.basename, "rows": 10}

    monkeypatch.setattr("dt_cloud.reactive.ingest_one", fake_ingest_one)
    return drain(
        client, buckets, log_bucket="logs", data_bucket="data",
        stage_dir="/stage", workers=kw.pop("workers", 1), **kw,
    )


def test_drain_ingests_exactly_the_gap(monkeypatch):
    client = _client(
        usage=[_csv("08"), _csv("09"), _other("08")],
        l0=[_csv("08")],
    )
    seen: list = []
    stats = _run(monkeypatch, client, ["marin-us-central1", "marin-us-west4"], seen)
    assert [n for n, _ in seen] == [_other("08"), _csv("09")]
    assert stats == {
        "found": 2, "ingested": 2, "failed": 0, "rows": 20,
        "buckets": {"marin-us-west4": 1, "marin-us-central1": 1},
    }


def test_drain_orders_by_log_hour_across_buckets(monkeypatch):
    """Not by basename — that would drain bucket-by-bucket, so an interrupted
    run would leave the last buckets untouched instead of the newest hours."""
    client = _client(usage=[_csv("09"), _other("07"), _csv("11"), _other("10")], l0=[])
    seen: list = []
    _run(monkeypatch, client, ["marin-us-central1", "marin-us-west4"], seen)
    assert [n for n, _ in seen] == [_other("07"), _csv("09"), _other("10"), _csv("11")]


def test_drain_survives_one_bad_csv(monkeypatch):
    """A single unparseable CSV must not strand the rest of the run; the next
    drain retries it, since the work list is recomputed, not resumed."""
    client = _client(usage=[_csv("08"), _csv("09"), _csv("10")], l0=[])
    seen: list = []
    stats = _run(monkeypatch, client, ["marin-us-central1"], seen, fail={_csv("09")})
    assert [n for n, _ in seen] == [_csv("08"), _csv("09"), _csv("10")]
    assert stats == {
        "found": 3, "ingested": 2, "failed": 1, "rows": 20,
        "buckets": {"marin-us-central1": 2},
    }


def test_drain_splits_its_memory_budget_across_workers(monkeypatch):
    client = _client(usage=[_csv("08"), _csv("09"), _csv("10"), _csv("11")], l0=[])
    seen: list = []
    _run(monkeypatch, client, ["marin-us-central1"], seen, workers=4, memory_limit="8GB")
    assert sorted({m for _, m in seen}) == ["2048MB"]


def test_drain_never_over_commits_when_work_is_thinner_than_workers(monkeypatch):
    """Two files at -w 8 should get 4GB each, not 1GB each — the budget divides
    by the connections actually opened."""
    client = _client(usage=[_csv("08"), _csv("09")], l0=[])
    seen: list = []
    _run(monkeypatch, client, ["marin-us-central1"], seen, workers=8, memory_limit="8GB")
    assert sorted({m for _, m in seen}) == ["4096MB"]


def test_drain_with_nothing_pending(monkeypatch):
    client = _client(usage=[_csv("08")], l0=[_csv("08")])
    seen: list = []
    stats = _run(monkeypatch, client, ["marin-us-central1"], seen)
    assert seen == []
    assert stats == {"found": 0, "ingested": 0, "failed": 0, "rows": 0, "buckets": {}}
