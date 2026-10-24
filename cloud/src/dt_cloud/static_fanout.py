"""A compaction's drill measurement fanned out over helper Batch tasks (specs/static-measure-fanout.md).

The fold VM (`static_compact.fold_drill` → `static_local.finish_drill`) saves its spills as the checkpoint's `spilled`
step, as it always has, then (`Fanout.start`) deals every `q` range of both kinds by spilled bytes over `tasks` shares
(`static_merge.deal`): share 0 is its own, measured from its local spills; the rest go to a helper job of `tasks − 1`
tasks (`static_merge measure-shard`), listed in `measured/plan-<attempt>.json` under the fold's checkpoint. A helper
fetches only its ranges' spills, measures them (`measure_ranges`) and uploads each range's sorted roots, measured
directories and rollup rows, then its `done.json`, to `measured/<kind>/g<g>/`; a range whose `done.json` is there is
skipped, so a reclaimed task's retry resumes. Meanwhile the fold VM, holding the compaction lease (renewed by its own
heartbeat), measures its share, then (`Fanout.collect`) waits for every other range's `done.json` and downloads them
where its own measurement's would be: the tables and files, so the drill's bytes, are a single VM's. Ranges still
missing when the helper job ends are dealt again to a new helper job (at most `retries` times), then the compaction
fails naming them. Helpers run the fold's own code (`code_version`), else refuse.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .static_names import err

#: Under the fold's checkpoint: the helpers' outputs, per range, and the plans that deal them.
MEASURED = "measured"


def range_key(kind: str, g: int) -> str:
    return f"{MEASURED}/{kind}/g{g:06d}"


def spill_sizes(work: Path, kind: str, n: int) -> dict[int, int]:
    """Each of `kind`'s spilled `q` ranges' bytes (`work/<kind>/*/grp=<g>/`), ranges without roots left out."""
    out = {}
    for g in range(n):
        size = sum(f.stat().st_size for f in (work / kind).glob(f"*/grp={g}/*.parquet"))
        if size:
            out[g] = size
    return out


class Fanout:
    """The fold VM's side: `start` deals and submits, `groups` names a kind's ranges the helpers measure, `collect` waits
    for and fetches them, `finish` waits for the helper job's end. `ck`: the fold's checkpoint (`spilled` saved);
    `submit(checkpoint_url, plan_key, tasks, code)`: runs a helper job of `tasks` tasks to its end (raising on its
    failure); `exists`/`get`/`read_json`/`write_text` go through `ck.store`."""

    def __init__(self, ck, tasks: int, submit: Callable[[str, str, int, str], None], *, code: str, poll_s: float = 30,
                 retries: int = 2, log: Callable[[str], None] = err):
        self.ck, self.tasks, self.submit, self.code, self.poll_s, self.retries, self.log = ck, tasks, submit, code, poll_s, retries, log
        self.away: dict[str, set[int]] = {}
        self.params: dict = {}
        self.attempt = 0
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def done(self, kind: str, g: int) -> bool:
        return self.ck.store.exists(self.ck.key(f"{range_key(kind, g)}/done.json"))

    def start(self, work: Path, groups: dict[str, int], *, R: int, K: int, floor: int, measure: bool = True) -> None:
        """Deal `groups` (`{kind: ranges}`) over the shares by spilled bytes, keep share 0, and submit the helpers for
        the rest (ranges already measured by an earlier attempt's helpers aren't dealt again); the plan carries the
        measurement's `R`, `K`, `floor`, and `measure` (false: the helpers only sort each range's roots, an incremental
        compaction's: `static_local.finish_drill`)."""
        self.params = {"R": R, "K": K, "floor": floor, "measure": measure}
        from .static_merge import deal

        weights = {(kind, g): size for kind, n in groups.items() for g, size in spill_sizes(work, kind, n).items()}
        shares = deal(weights, self.tasks)
        mine = set(shares[0])
        self.away = {kind: {g for (k, g) in weights if k == kind and (k, g) not in mine} for kind in groups}
        todo = [[[k, g] for k, g in share if not self.done(k, g)] for share in shares[1:]]
        self.log(f"measure fan-out: {len(mine)} ranges here ({sum(weights[r] for r in mine):,} B), "
                 f"{sum(map(len, self.away.values()))} on {self.tasks - 1} helper tasks ({sum(len(t) for t in todo)} to measure)")
        self._submit([t for t in todo if t])

    def _submit(self, shares: list[list]) -> None:
        if not shares:
            return
        self.attempt += 1
        plan = f"{MEASURED}/plan-{self.attempt}.json"
        self.ck.store.write_text(self.ck.key(plan), json.dumps({"tasks": shares, "code": self.code, **self.params}) + "\n")
        url = self.ck.store.uri(self.ck.prefix)

        def run() -> None:
            try:
                self.submit(url, plan, len(shares), self.code)
            except BaseException as e:  # noqa: BLE001 — the waiting side reports it, with the ranges it left
                self.error = e

        self.error = None
        self.thread = threading.Thread(target=run, name=f"measure-helpers-{self.attempt}", daemon=True)
        self.thread.start()

    def groups(self, kind: str) -> set[int]:
        return self.away.get(kind, set())

    def collect(self, kind: str, paths: dict[int, tuple[str, str, str]]) -> dict[int, dict]:
        """Wait until every range of `paths` (`{g: (sorted, dirs, rup)}` local targets) is measured by a helper, dealing
        the missing ones again when a helper job ends without them; then download each to its targets. Each range's
        stats (its `done.json`)."""
        while True:
            missing = sorted(g for g in paths if not self.done(kind, g))
            if not missing:
                break
            if self.thread is None or not self.thread.is_alive():
                why = f" ({type(self.error).__name__}: {self.error})" if self.error else ""
                if self.attempt > self.retries:
                    raise RuntimeError(f"measure fan-out: {kind} ranges {missing} unmeasured after {self.attempt} helper jobs{why}")
                self.log(f"measure fan-out: helper job {self.attempt} ended{why}; dealing {len(missing)} {kind} ranges again")
                from .static_merge import deal

                pend = {(k, g): 1 for k, gs in self.away.items() for g in gs if not self.done(k, g)}
                self._submit([[[k, g] for k, g in share] for share in deal(pend, self.tasks - 1) if share])
            time.sleep(self.poll_s)
        out = {}
        for g, (so, do, ro) in paths.items():
            base = range_key(kind, g)
            for name, dst in (("sorted", so), ("dirs", do), ("rup", ro)):
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                self.ck.store.get(self.ck.key(f"{base}/{name}.parquet"), Path(dst))
            out[g] = self.ck.store.read_json(self.ck.key(f"{base}/done.json"))
        self.log(f"measure fan-out: {len(out)} {kind} ranges from the helpers")
        return out

    def finish(self) -> None:
        """Wait for the helper job's end (every range is measured by now; its tasks are exiting)."""
        if self.thread is not None:
            self.thread.join()
