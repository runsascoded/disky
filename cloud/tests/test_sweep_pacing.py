"""Deterministic feedback and admission specs; no cloud requests."""

import threading

import pytest

from dt_cloud.sweep_pacing import BucketPacer, DeleteAttempt


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def healthy(pacer: BucketPacer, clock: Clock, until: int) -> None:
    for second in range(int(clock.now) + 1, until + 1):
        clock.now = second
        # Supply enough full-batch successes to exercise growth, not idle time.
        for _ in range(8):
            pacer.observe(DeleteAttempt(1000, 1))


def test_adaptive_probes_each_healthy_minute_and_never_exceeds_ceiling() -> None:
    clock = Clock()
    pacer = BucketPacer(2000, mode="adaptive", clock=clock, sleep=clock.sleep)
    rates = []
    for end in (60, 120, 180, 240, 300):
        healthy(pacer, clock, end)
        rates.append(pacer.metrics()["rate"])
    assert rates == [1250, 1562.5, 1953.125, 2000, 2000]
    assert pacer.metrics()["increases"] == 4


def test_guided_growth_stays_inside_gcs_cold_envelope() -> None:
    clock = Clock()
    pacer = BucketPacer(mode="guided", clock=clock, sleep=clock.sleep)
    healthy(pacer, clock, 1200)
    assert pacer.metrics()["rate"] == 2000
    assert pacer.metrics()["increases"] == 20
    healthy(pacer, clock, 2400)
    assert pacer.metrics()["rate"] == 4000


def test_idle_successes_do_not_ramp_and_pressure_is_bucket_local() -> None:
    clock = Clock()
    a = BucketPacer(mode="adaptive", clock=clock, sleep=clock.sleep)
    b = BucketPacer(mode="adaptive", clock=clock, sleep=clock.sleep)
    clock.now = 1200
    a.observe(DeleteAttempt(1, .1))
    b.observe(DeleteAttempt(1000, .1, "backpressure", "http-429", 120))
    assert [p.metrics()["rate"] for p in (a, b)] == [1000, 500]
    assert [p.metrics()["cooldown_seconds"] for p in (a, b)] == [0, 120]


def test_pressure_halves_once_per_ten_seconds_and_restarts_healthy_window() -> None:
    clock = Clock()
    pacer = BucketPacer(mode="adaptive", clock=clock, sleep=clock.sleep)
    healthy(pacer, clock, 60)
    for _ in range(32):
        pacer.observe(DeleteAttempt(1000, 1, "backpressure", "http-503"))
    assert [pacer.metrics()[key] for key in ("rate", "pressures", "decreases", "cooldown_seconds")] == [625, 32, 1, 5]
    healthy(pacer, clock, 119)
    assert pacer.metrics()["rate"] == 625
    healthy(pacer, clock, 120)
    assert pacer.metrics()["rate"] == 781.25
    for second in (130, 140, 150, 160, 170):
        clock.now = second
        pacer.observe(DeleteAttempt(1000, 1, "backpressure", "xml-transient"))
    assert pacer.metrics()["rate"] == 100


def test_three_slow_full_batches_reduce_rate_but_small_tail_batches_do_not() -> None:
    clock = Clock()
    pacer = BucketPacer(mode="adaptive", clock=clock, sleep=clock.sleep)
    pacer.observe(DeleteAttempt(1000, 1))
    for _ in range(3):
        pacer.observe(DeleteAttempt(5, 10))
    assert pacer.metrics()["rate"] == 1000
    pacer.observe(DeleteAttempt(1000, 3))
    pacer.observe(DeleteAttempt(1000, 3))
    assert pacer.metrics()["rate"] == 1000
    pacer.observe(DeleteAttempt(1000, 3))
    assert [pacer.metrics()[key] for key in ("rate", "last_reason", "pressures")] == [500, "latency", 1]


def test_retries_and_new_requests_share_http_slots_and_idle_has_no_burst_credit() -> None:
    clock = Clock()
    pacer = BucketPacer(clock=clock, sleep=clock.sleep)
    times = []
    for count in (1000, 1000, 500):
        assert pacer.wait(count) is True
        times.append(round(clock.now, 6))
    clock.now = 100
    assert pacer.wait(1000) is True
    times.append(clock.now)
    assert pacer.wait(1000) is True
    times.append(round(clock.now, 6))
    assert times == [0, 1, 2, 100, 101]
    assert pacer.metrics()["wait_seconds"] == {"submit": 0, "http": 3}


def test_stop_cancels_admission_but_submitted_retry_can_drain() -> None:
    clock = Clock()
    pacer = BucketPacer(clock=clock, sleep=clock.sleep)
    stop = threading.Event()
    stop.set()
    assert pacer.wait(1000, "submit", stop) is False
    assert pacer.wait(1000) is True
    pacer.observe(DeleteAttempt(1000, 1, "backpressure", "http-429", 8))
    assert pacer.wait(1000) is True
    assert round(clock.now, 6) == 8


@pytest.mark.parametrize("kwargs", [{"maximum": 0}, {"initial": 0}, {"initial": 9000}, {"mode": "unknown"}])
def test_invalid_pacing_is_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="^invalid bucket pacing mode/initial/maximum$"):
        BucketPacer(**kwargs)
