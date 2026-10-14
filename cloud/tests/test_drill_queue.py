"""The drill's shared work queue (`static_roots.Queue`) and `index`'s input check: a task never succeeds while a shard
it is responsible for is unfinished, and `index` refuses an incomplete drill (specs/cw-static-names.md, "Drill
incident": a preempted task's fresh claim was skipped, the job succeeded, and `index` wrote meta.json over 62 of 65
shards)."""
from datetime import datetime, timezone

import pyarrow as pa
import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from dt_cloud.static_roots import Queue, QueueTimeout, canonical_shards, check_index_inputs, index_gaps


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t
        self.on_sleep = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        for f in self.on_sleep:
            f(self.t)


class Blob:
    def __init__(self, store: "Bucket", name: str):
        self.store, self.name = store, name
        self.generation = None
        self.updated = None

    def upload_from_string(self, data: str, if_generation_match=None) -> None:
        cur = self.store.objs.get(self.name)
        gen = cur[1] if cur else 0
        if if_generation_match is not None and if_generation_match != gen:
            raise PreconditionFailed("generation")
        self.store.gen += 1
        self.store.objs[self.name] = (data, self.store.gen, self.store.clock.now())

    def reload(self) -> None:
        if self.name not in self.store.objs:
            raise NotFound(self.name)
        _, self.generation, t = self.store.objs[self.name]
        self.updated = datetime.fromtimestamp(t, timezone.utc)

    def download_as_text(self, if_generation_match=None) -> str:
        if self.name not in self.store.objs:
            raise NotFound(self.name)
        data, gen, _ = self.store.objs[self.name]
        if if_generation_match is not None and if_generation_match != gen:
            raise PreconditionFailed("generation")
        return data


class Bucket:
    def __init__(self, clock: Clock):
        self.clock, self.objs, self.gen = clock, {}, 0

    def blob(self, name: str) -> Blob:
        return Blob(self, name)


LEASE = 5400


def setup(task: int = 1):
    clock = Clock()
    b = Bucket(clock)
    queue = Queue(b, "static-names/g", "drill-build", task, LEASE, poll=60, now=clock.now, sleep=clock.sleep)
    return clock, b, queue


def claim_as(b: Bucket, task: int, name: str) -> None:
    b.blob(f"static-names/g/claims/drill-build/{name}").upload_from_string(str(task), if_generation_match=0)


def test_free_shards_built_in_order_no_wait():
    clock, b, queue = setup()
    done, log = set(), []
    built = queue.drain(["s0002", "s0000", "s0001"], done.__contains__, lambda n: (log.append(n), done.add(n)))
    assert (built, log, clock.t) == (["s0002", "s0000", "s0001"], ["s0002", "s0000", "s0001"], 1_000_000.0)


def test_done_shards_skipped():
    clock, b, queue = setup()
    done, log = {"s0000"}, []
    assert queue.drain(["s0000", "s0001"], done.__contains__, lambda n: (log.append(n), done.add(n))) == ["s0001"]
    assert log == ["s0001"]


def test_preempted_claim_taken_over_after_lease():
    # Task 7 claimed s0035 and was preempted (the claim is fresh, its output never lands): task 1 waits, takes it over
    # once the lease expires, builds it, and only then succeeds.
    clock, b, queue = setup()
    claim_as(b, 7, "s0035")
    done, log = set(), []
    built = queue.drain(["s0035", "s0036"], done.__contains__, lambda n: (log.append((n, clock.t - 1_000_000)), done.add(n)))
    assert built == ["s0036", "s0035"]
    assert log == [("s0036", 0), ("s0035", 5400)]
    assert b.objs["static-names/g/claims/drill-build/s0035"][0] == "1"


def test_live_claim_finishing_during_wait_not_taken_over():
    clock, b, queue = setup()
    claim_as(b, 7, "s0035")
    done, log = set(), []
    clock.on_sleep.append(lambda t: done.add("s0035") if t - 1_000_000 >= 600 else None)
    built = queue.drain(["s0035"], done.__contains__, lambda n: (log.append(n), done.add(n)))
    assert (built, log, clock.t - 1_000_000) == ([], [], 600)


def test_own_earlier_attempt_claim_taken_over_at_once():
    # A retried task (same index) finds its own earlier attempt's claim: that attempt is gone, so no lease wait.
    clock, b, queue = setup(task=7)
    claim_as(b, 7, "s0035")
    done, log = set(), []
    assert queue.drain(["s0035"], done.__contains__, lambda n: (log.append(n), done.add(n))) == ["s0035"]
    assert (log, clock.t) == (["s0035"], 1_000_000.0)


def test_wait_times_out_and_fails():
    clock, b, _ = setup()
    queue = Queue(b, "static-names/g", "drill-build", 1, LEASE, wait=1800, poll=60, now=clock.now, sleep=clock.sleep)
    claim_as(b, 7, "s0035")
    with pytest.raises(QueueTimeout) as e:
        queue.drain(["s0035"], lambda n: False, lambda n: None)
    assert str(e.value) == "drill-build: task 1 waited 1800s; still unfinished (claimed by other tasks): s0035"
    assert e.value.code == str(e.value)


def test_index_gaps_and_refusal():
    members = pa.table({"q": ["abc", "abd", "xyz", "zzz"], "shard": [35, 35, 63, 64]})
    aliases = pa.table({"q": ["abc", "abd", "xyz", "zzz"], "canonical": ["abc", "abc", "xyz", "xyz"]})
    assert canonical_shards(members, aliases) == {35, 63}
    assert canonical_shards(members, None) == {35, 63, 64}
    gaps = index_gaps({35, 63}, 3, {"s0063.parquet", "s0001.parquet"}, {"g000.parquet", "g002.parquet"})
    assert gaps == ["long/rollups-index/s0035.parquet", "short/rollups-index/g001.parquet"]
    assert index_gaps({35}, None, {"s0035.parquet"}, set()) == ["short-plan.json"]
    assert index_gaps({35}, 1, {"s0035.parquet"}, {"g000.parquet"}) == []
    with pytest.raises(SystemExit) as e:
        check_index_inputs("2026-10-10cw", gaps)
    assert str(e.value) == ("drill index 2026-10-10cw: refusing: 2 build outputs missing under drill/: "
                            "long/rollups-index/s0035.parquet, short/rollups-index/g001.parquet")
    check_index_inputs("2026-10-10cw", [])
