"""Per-bucket feedback pacing; retries share the HTTP lane with new deletes."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class DeleteAttempt:
    objects: int
    seconds: float
    outcome: str = "ok"
    reason: str = ""
    retry_after: float = 0


class BucketPacer:
    """AIMD with a cold-bucket envelope in guided mode.

    Guided growth never exceeds the GCS 2x/20m envelope. Adaptive mode probes
    25% higher per healthy, sufficiently loaded minute. Transient errors or
    three slow full batches halve the rate, at most once per ten seconds;
    pressure restarts the healthy window. Both modes respect Retry-After.
    Producer admission keeps the shared pool from filling with paced waits;
    the independent HTTP lane accounts for every real attempt, including
    retries. Idle time never accumulates burst credits. Stop cancels producer
    admission only: submitted requests and exact-generation retries drain.
    """

    def __init__(
        self,
        maximum: int = 8000,
        initial: int = 1000,
        mode: str = "guided",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if mode not in ("guided", "adaptive") or not 0 < initial <= maximum:
            raise ValueError("invalid bucket pacing mode/initial/maximum")
        self.maximum = maximum
        self.initial = initial
        self.minimum = min(100, initial)
        self.mode = mode
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.started = clock()
        self.rate = float(initial)
        self.next_probe = self.started + 60
        self.window_started = self.started
        self.next_slot = {"submit": self.started, "http": self.started}
        self.cooldown_until = self.started
        self.last_cut = -math.inf
        self.baseline: float | None = None
        self.slow = self.healthy = self.window_objects = 0
        self.attempts = self.attempted_objects = self.pressures = self.increases = self.decreases = 0
        self.last_reason = "initial"
        self.wait_seconds = {"submit": 0.0, "http": 0.0}

    def wait(
        self,
        objects: int,
        lane: str = "http",
        stop: threading.Event | None = None,
        on_wait: Callable[[], None] | None = None,
    ) -> bool:
        if objects <= 0 or lane not in self.next_slot:
            raise ValueError("invalid paced request")
        started = self.clock()
        while True:
            if on_wait is not None:
                on_wait()
            if stop is not None and stop.is_set():
                return False
            with self.lock:
                now = self.clock()
                delay = max(self.next_slot[lane], self.cooldown_until) - now
                if delay <= 0:
                    self.next_slot[lane] = now + objects / self.rate
                    self.wait_seconds[lane] += now - started
                    return True
            delay = min(.1, delay)
            if stop is None:
                self.sleep(delay)
            else:
                stop.wait(delay)

    def _set_rate(self, rate: float, now: float) -> None:
        old = self.rate
        self.rate = max(self.minimum, min(self.maximum, rate))
        for lane, slot in self.next_slot.items():
            self.next_slot[lane] = now + max(0, slot - now) * old / self.rate

    def observe(self, attempt: DeleteAttempt) -> None:
        with self.lock:
            now = self.clock()
            self.attempts += 1
            self.attempted_objects += attempt.objects
            reason = attempt.reason if attempt.outcome == "backpressure" else ""
            if attempt.outcome == "ok" and attempt.objects >= 500:
                if self.baseline is None:
                    self.baseline = attempt.seconds
                elif attempt.seconds > max(2, 2 * self.baseline):
                    self.slow += 1
                    if self.slow >= 3:
                        reason = "latency"
                else:
                    self.slow = 0
                    self.baseline = min(self.baseline, attempt.seconds)
            if reason:
                self.pressures += 1
                self.last_reason = reason
                if now - self.last_cut >= 10:
                    previous = self.rate
                    self._set_rate(self.rate / 2, now)
                    self.decreases += int(self.rate < previous)
                    self.last_cut = now
                self.cooldown_until = max(self.cooldown_until, now + max(5, attempt.retry_after))
                self.next_probe = max(self.next_probe, now + 60, self.cooldown_until)
                self.window_started = now
                self.healthy = self.window_objects = self.slow = 0
                return
            if attempt.outcome != "ok":
                self.healthy = self.window_objects = 0
                return
            self.healthy += 1
            self.window_objects += attempt.objects
            if now >= self.next_probe:
                elapsed = now - self.window_started
                if self.healthy >= 4 and self.window_objects >= self.rate * elapsed * .5:
                    envelope = self.maximum if self.mode == "adaptive" else min(self.maximum, self.initial * 2 ** min(30, (now - self.started) / 1200))
                    previous = self.rate
                    self._set_rate(min(self.rate * 1.25, envelope), now)
                    if self.rate > previous:
                        self.increases += 1
                        self.last_reason = "healthy"
                self.next_probe = now + 60
                self.window_started = now
                self.healthy = self.window_objects = 0

    def metrics(self) -> dict:
        with self.lock:
            return {
                "mode": self.mode, "rate": round(self.rate, 3), "maximum": self.maximum,
                "attempts": self.attempts, "attempted_objects": self.attempted_objects,
                "pressures": self.pressures, "increases": self.increases, "decreases": self.decreases,
                "last_reason": self.last_reason, "baseline_seconds": self.baseline,
                "cooldown_seconds": round(max(0, self.cooldown_until - self.clock()), 3),
                "wait_seconds": {lane: round(seconds, 3) for lane, seconds in self.wait_seconds.items()},
            }
