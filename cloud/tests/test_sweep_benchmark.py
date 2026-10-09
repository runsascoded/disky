"""Read-only listing benchmark behavior."""

from __future__ import annotations

from dataclasses import dataclass, field

from dt_cloud.sweep_benchmark import _representative_roots, benchmark_listings


@dataclass
class Blob:
    name: str
    size: int


@dataclass
class Client:
    blobs: list[Blob]
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    def list_blobs(self, bucket: str, prefix: str, max_results: int):
        self.calls.append((bucket, prefix, max_results))
        return [blob for blob in self.blobs if blob.name.startswith(prefix)][:max_results]


def test_representative_roots_keeps_largest_and_spreads_the_rest():
    roots = [(f"r{i}", 100 - i) for i in range(10)]
    assert _representative_roots(roots, 5) == [
        ("r0", 100),
        ("r1", 99),
        ("r2", 98),
        ("r3", 97),
        ("r9", 91),
    ]


def test_benchmark_replays_one_root_cohort_at_each_concurrency(monkeypatch):
    import dt_cloud.sweep_benchmark as benchmark

    monkeypatch.setattr(
        benchmark,
        "_plan_roots",
        lambda plan, bucket, maximum: ([
            ("a", 3),
            ("b", 2),
            ("c", 1),
        ], 1.25),
    )
    clock = iter([10.0, 12.0, 20.0, 21.0])
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: next(clock))
    client = Client([
        Blob("a/1", 10),
        Blob("a/2", 20),
        Blob("b/1", 30),
        Blob("c/1", 40),
    ])
    result = benchmark_listings(
        "plan",
        "bucket",
        workers=(1, 2),
        roots=2,
        max_root_objects=7,
        max_results_per_root=1,
        client=client,
    )
    assert result == {
        "plan": "plan",
        "bucket": "bucket",
        "planning_seconds": 1.25,
        "planned_roots": 3,
        "selected_roots": 2,
        "selected_manifest_objects": 4,
        "max_root_objects": 7,
        "max_results_per_root": 1,
        "roots": [
            {"prefix": "a", "manifest_objects": 3},
            {"prefix": "c", "manifest_objects": 1},
        ],
        "trials": [
            {"workers": 1, "seconds": 2.0, "objects": 2, "bytes": 50, "objects_per_second": 1.0},
            {"workers": 2, "seconds": 1.0, "objects": 2, "bytes": 50, "objects_per_second": 2.0},
        ],
    }
    assert sorted(client.calls) == [
        ("bucket", "a/", 1),
        ("bucket", "a/", 1),
        ("bucket", "c/", 1),
        ("bucket", "c/", 1),
    ]
