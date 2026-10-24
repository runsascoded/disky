"""A compaction's heavy parts fanned out over helper Batch tasks: the drill measurement (specs/static-measure-fanout.md)
and the base anchors' rollups (specs/static-anchors-fanout.md).

`Helpers` is what both share: the fold VM deals its units of work (`(kind, index)` pairs) by bytes over `tasks` shares
(`static_merge.deal`), keeps share 0, lists the rest in `<root>/plan-<attempt>.json` under its checkpoint and runs a
helper job of their tasks on a thread; each helper uploads a unit's outputs, then its `done.json`, under
`<root>/<kind>/…`, skipping a unit whose `done.json` is there (a reclaimed task's retry resumes). The fold VM, holding
the compaction lease (renewed by its own heartbeat), does its share, then waits for every other unit's `done.json`;
units still missing when the helper job ends are dealt again to a new helper job (at most `retries` times), then the
compaction fails naming them. Helpers run the fold's own code (`code_version`), else refuse.

`Fanout` (the drill measurement): the fold VM (`static_compact.fold_drill` → `static_local.finish_drill`) saves its
spills as the checkpoint's `spilled` step, as it always has, then (`Fanout.start`) deals every `q` range of both kinds by
spilled bytes: share 0 is measured from its local spills; a helper (`static_merge measure-shard`) fetches only its
ranges' spills, measures them (`measure_ranges`) and uploads each range's sorted roots, measured directories and rollup
rows to `measured/<kind>/g<g>/`. `Fanout.collect` downloads them where its own measurement's would be: the tables and
files, so the drill's bytes, are a single VM's.

`AnchorFanout` (the base anchors, `static_anchors._tier_parallel`): each `(kind, shard)` rollup task (`end` over the
suffix shards, `exact` and `start` over the name shards) dealt by input bytes; the inputs of the tasks dealt away
uploaded under `anchors-in/` (once each, skipped when there); a helper (`static_merge anchor-shard`) runs
`_shard_task` on each of its tasks and uploads its sorted cells to `anchored/<kind>/s<i>/cells.parquet`.
`AnchorFanout.collect` downloads them where the local pool's would be, so `write_rollup_parts` streams a single VM's
cells.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .static_names import err

#: Under the fold's checkpoint: the measurement helpers' outputs, per range, and the plans that deal them.
MEASURED = "measured"
#: Under the compaction's checkpoint: the anchors helpers' sorted cells, per task, and the plans that deal them.
ANCHORED = "anchored"
#: Under the compaction's checkpoint: the shard files the anchors helpers read (`anchor_input_key`).
ANCHORS_IN = "anchors-in"


def range_key(kind: str, g: int) -> str:
    return f"{MEASURED}/{kind}/g{g:06d}"


def anchor_task_key(kind: str, i: int) -> str:
    return f"{ANCHORED}/{kind}/s{i:05d}"


def anchor_input_key(kind: str, i: int) -> str:
    """Shard `i`'s input file of an anchors task of `kind`: a suffix shard (`end`), or a name shard (`exact` and `start`
    read the same one)."""
    return f"{ANCHORS_IN}/{'sx' if kind == 'end' else 'names'}/s{i:05d}.parquet"


#: Under the fold's checkpoint: the spills' files (`spilled/done.json` lists them; a measurement helper fetches its ranges').
SPILLED_FILES = "spilled/files"


class Helpers:
    """The fold VM's side of a fan-out: `deal` keeps share 0 and submits the rest, `wait` waits for units (re-dealing what
    a helper job left), `finish` waits for the helper job's end. `ck`: the checkpoint the helpers read and write under
    (`root/`, a subclass's); `submit(checkpoint_url, plan_key, tasks, code, scan)`: runs a helper job of `tasks` tasks to
    its end (raising on its failure; `scan`: the newest scan the compaction folds, what the job is named and labelled
    by); `exists`/`get`/`read_json`/`write_text` go through `ck.store`. `label` begins its log lines; `undone` says what a
    unit no helper finished is, in the failure."""

    root = ""
    label = ""
    undone = ""

    def __init__(self, ck, tasks: int, submit: Callable[[str, str, int, str, str], None], *, code: str, scan: str, poll_s: float = 30,
                 retries: int = 2, log: Callable[[str], None] = err):
        self.ck, self.tasks, self.submit, self.code, self.scan, self.poll_s, self.retries, self.log = ck, tasks, submit, code, scan, poll_s, retries, log
        self.away: set[tuple[str, int]] = set()
        self.params: dict = {}
        self.attempt = 0
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def unit_key(self, kind: str, i: int) -> str:
        raise NotImplementedError

    def done(self, kind: str, i: int) -> bool:
        return self.ck.store.exists(self.ck.key(f"{self.unit_key(kind, i)}/done.json"))

    def listed(self) -> dict[str, int]:
        """Every file under the checkpoint's `root/` (`{key: size}`), one listing: the units done are those with a
        `done.json` in it (a `done` per unit is a request each, minutes over thousands of units)."""
        return self.ck.store.listing(self.ck.key(self.root))

    def done_in(self, listing: dict[str, int], units) -> set[tuple[str, int]]:
        return {u for u in units if self.ck.key(f"{self.unit_key(*u)}/done.json") in listing}

    def deal(self, weights: dict[tuple[str, int], int]) -> tuple[set[tuple[str, int]], list[list[list]]]:
        """`weights`' units dealt over the shares (`static_merge.deal`): share 0 (kept), and the other shares' units not
        done already (an earlier attempt's helpers'), as plan entries; `away` = every unit not in share 0."""
        from .static_merge import deal

        shares = deal(weights, self.tasks)
        done = self.done_in(self.listed(), weights)
        # share 0's units already in the checkpoint (a seeded or resumed run's) are fetched, not built again
        mine = set(shares[0]) - done
        self.away = set(weights) - mine
        return mine, [[[k, i] for k, i in share if (k, i) not in done] for share in shares[1:]]

    def _submit(self, shares: list[list]) -> None:
        if not shares:
            return
        self.attempt += 1
        plan = f"{self.root}/plan-{self.attempt}.json"
        self.ck.store.write_text(self.ck.key(plan), json.dumps({"tasks": shares, "code": self.code, **self.params}) + "\n")
        url = self.ck.store.uri(self.ck.prefix)
        self.log(f"{self.label}: {plan}: {len(shares)} helper tasks submitted")

        def run() -> None:
            try:
                self.submit(url, plan, len(shares), self.code, self.scan)
            except BaseException as e:  # noqa: BLE001 — the waiting side reports it, with the units it left
                self.error = e

        self.error = None
        self.thread = threading.Thread(target=run, name=f"{self.root}-helpers-{self.attempt}", daemon=True)
        self.thread.start()

    def wait(self, units: list[tuple[str, int]], what: str, ids: Callable[[list[tuple[str, int]]], list],
             fetch: Callable[[list[tuple[str, int]], dict[str, int]], None] | None = None) -> None:
        """Wait until every unit of `units` is done, dealing every unit away and not done again (over the helper shares,
        one each) when a helper job ends without them, at most `retries` times; then fail naming the missing (`what`
        `ids(missing)`). `fetch(units, listing)`: called with the units newly done at each poll (and the listing), so
        their outputs download while the rest are still being built."""
        from .static_merge import deal

        fetched: set[tuple[str, int]] = set()
        while True:
            # liveness read before the listing: a helper job already ended then has written all it will, so the listing shows it
            ended = self.thread is None or not self.thread.is_alive()
            listing = self.listed()
            done = self.done_in(listing, units)
            if fetch is not None and (new := sorted(done - fetched)):
                fetch(new, listing)
                fetched.update(new)
                continue
            missing = sorted(set(units) - done)
            if not missing:
                return
            if ended:
                why = f" ({type(self.error).__name__}: {self.error})" if self.error else ""
                if self.attempt > self.retries:
                    raise RuntimeError(f"{self.label}: {what} {ids(missing)} {self.undone} after {self.attempt} helper jobs{why}")
                self.log(f"{self.label}: helper job {self.attempt} ended{why}; dealing {len(missing)} {what} again")
                pend = {u: 1 for u in self.away - self.done_in(listing, self.away)}
                self._submit([[[k, i] for k, i in share] for share in deal(pend, self.tasks - 1) if share])
            time.sleep(self.poll_s)

    def fetch(self, files: dict[str, Path], listing: dict[str, int], stage: str) -> None:
        """Checkpoint files (`{key under root/: local target}`, sizes from `listing`) downloaded in parallel
        (`blobstore.stage_files`: sliced ranged GETs over processes from `gs://`, threads elsewhere) into a `.fetch/` dir
        beside the targets, then moved into place. A file still failing after its retries raises."""
        import os
        import shutil

        from .blobstore import stage_files

        if not files:
            return
        full = {self.ck.key(k): dst for k, dst in files.items()}
        tmp = Path(os.path.commonpath([str(Path(d).parent) for d in full.values()])) / ".fetch"
        got = stage_files(self.ck.store.uri(""), {k: listing[k] for k in full}, tmp, label=stage, log=self.log)
        if got["failed"]:
            raise RuntimeError(f"{self.label}: {len(got['failed'])} files not fetched: {got['failed'][:5]}")
        for k, dst in full.items():
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp / k, dst)
        shutil.rmtree(tmp, ignore_errors=True)

    def finish(self) -> None:
        """Wait for the helper job's end (every unit is done by now; its tasks are exiting)."""
        if self.thread is not None:
            self.thread.join()


class Fanout(Helpers):
    """The drill measurement's fan-out: `start` deals and submits, `groups` names a kind's ranges the helpers measure,
    `collect` waits for and fetches them. `ck`: the fold's checkpoint (`spilled` saved)."""

    root, label, undone = MEASURED, "measure fan-out", "unmeasured"

    def unit_key(self, kind: str, g: int) -> str:
        return range_key(kind, g)

    def start(self, work: Path, groups: dict[str, int], *, R: int, K: int, floor: int, measure: bool = True) -> None:
        """Deal `groups` (`{kind: ranges}`) over the shares by spilled bytes (the checkpoint's `spilled` marker's files
        and their listed sizes: `static_merge._spilled`), keep share 0, fetch its spills not on the work disk into `work`
        (a resumed fold restores only the `spilled` step's tables, not every spill), and submit the helpers for the rest
        (ranges already measured by an earlier attempt's helpers aren't dealt again); the plan carries the measurement's
        `R`, `K`, `floor`, and `measure` (false: the helpers only sort each range's roots, an incremental compaction's:
        `static_local.finish_drill`)."""
        self.params = {"R": R, "K": K, "floor": floor, "measure": measure}
        from . import heartbeat as hb
        from .static_merge import _spilled

        hb.set_stage("drill/measure/fan-out plan")
        sizes, by = _spilled(self.ck)
        weights = {(kind, g): size for (kind, g), rels in by.items() if g < groups.get(kind, 0) and (size := sum(sizes[r] for r in rels))}
        mine, todo = self.deal(weights)
        need = {r: work / r for u in sorted(mine) for r in by[u] if not (work / r).is_file()}
        self.log(f"measure fan-out: {len(mine)} ranges here ({sum(weights[r] for r in mine):,} B; {len(need)} spill files, "
                 f"{sum(sizes[r] for r in need):,} B to fetch), {len(self.away)} on {self.tasks - 1} helper tasks ({sum(len(t) for t in todo)} to measure)")
        self._submit([t for t in todo if t])
        self.fetch({f"{SPILLED_FILES}/{r}": dst for r, dst in need.items()}, {self.ck.key(f"{SPILLED_FILES}/{r}"): sizes[r] for r in need},
                   "measure fan-out: this share's spills")

    def keep(self, kind: str) -> Callable[[int, tuple, dict], None]:
        """`measure_ranges`' `on_range` for share 0: each range measured here saved under the checkpoint as a helper's
        is (`sorted`/`dirs`/`rup`, then `done.json`), so a race seeded from this one (`seed_checkpoint`) or a rerun
        fetches it instead of measuring it again."""
        def save(g: int, task: tuple, stats: dict) -> None:
            base = range_key(kind, g)
            for name, f in (("sorted", task[2]), ("dirs", task[3]), ("rup", task[4])):
                self.ck.store.put(Path(f), self.ck.key(f"{base}/{name}.parquet"))
            self.ck.store.write_text(self.ck.key(f"{base}/done.json"), json.dumps(stats) + "\n")

        return save

    def groups(self, kind: str) -> set[int]:
        return {g for k, g in self.away if k == kind}

    def collect(self, kind: str, paths: dict[int, tuple[str, str, str]]) -> dict[int, dict]:
        """Wait until every range of `paths` (`{g: (sorted, dirs, rup)}` local targets) is measured by a helper, dealing
        the missing ones again when a helper job ends without them; then download each to its targets. Each range's
        stats (its `done.json`)."""
        import shutil

        from . import heartbeat as hb

        out: dict[int, dict] = {}
        nbytes = 0
        # each range's `done.json` lands here (beside the targets, so moved into place), read, then dropped
        if not paths:
            return {}
        docdir = Path(next(iter(paths.values()))[0]).parent / ".fetch-done"
        hb.set_stage(f"drill/measure/{kind}: 0/{len(paths)} helper ranges collected")

        def fetch(units: list[tuple[str, int]], listing: dict[str, int]) -> None:
            nonlocal nbytes
            files, docs = {}, {}
            for _, g in units:
                base = range_key(kind, g)
                files.update({f"{base}/{name}.parquet": Path(dst) for name, dst in zip(("sorted", "dirs", "rup"), paths[g])})
                docs[g] = docdir / f"g{g:06d}.json"
                files[f"{base}/done.json"] = docs[g]
            nbytes += sum(listing[self.ck.key(k)] for k in files)
            self.fetch(files, listing, f"measure fan-out: {kind} ranges")
            for g, f in docs.items():
                out[g] = json.loads(f.read_text())
                f.unlink()
            hb.set_stage(f"drill/measure/{kind}: {len(out)}/{len(paths)} helper ranges collected, {nbytes / 2**30:,.1f} GiB")

        self.wait([(kind, g) for g in paths], f"{kind} ranges", lambda us: [g for _, g in us], fetch)
        shutil.rmtree(docdir, ignore_errors=True)
        self.log(f"measure fan-out: {len(out)} {kind} ranges from the helpers ({nbytes / 2**30:,.1f} GiB)")
        return {g: out[g] for g in paths}


class AnchorFanout(Helpers):
    """The base anchors' fan-out: `start` deals the `(kind, shard)` rollup tasks by input bytes, uploads the inputs of
    the tasks dealt away and submits them; `collect` waits for and fetches their cells. `ck`: the compaction's
    checkpoint."""

    root, label, undone = ANCHORED, "anchors fan-out", "not built"

    def unit_key(self, kind: str, i: int) -> str:
        return anchor_task_key(kind, i)

    def start(self, tasks: list[tuple[str, int, str]], *, R: int, K: int, rule=None, workers: int = 16) -> set[tuple[str, int]]:
        """Deal `tasks` (`(kind, shard index, input file)`) over the shares by input bytes, keep share 0, upload the
        inputs the other shares' tasks not done yet read (each once; one already there is kept: a resumed compaction's
        shards are the same bytes), and submit the helpers. The plan carries `R`, `K` and the hex-run `rule`. Returns
        the tasks dealt away."""
        import os
        from concurrent.futures import ThreadPoolExecutor

        from . import heartbeat as hb
        from .hex_runs import rule_json

        self.params = {"R": R, "K": K, **rule_json(rule)}
        hb.set_stage("anchors/fan-out plan")
        weights = {(kind, i): os.path.getsize(f) for kind, i, f in tasks}
        files = {(kind, i): f for kind, i, f in tasks}
        mine, todo = self.deal(weights)
        need = {anchor_input_key(k, i): files[(k, i)] for share in todo for k, i in share}
        have = self.ck.store.listing(self.ck.key(ANCHORS_IN))
        put = {key: f for key, f in need.items() if have.get(self.ck.key(key)) != os.path.getsize(f)}
        hb.set_stage(f"anchors/fan-out inputs: {len(put)} shard files")
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(lambda kf: self.ck.store.put(Path(kf[1]), self.ck.key(kf[0])), sorted(put.items())))
        self.log(f"anchors fan-out: {len(mine)} shard tasks here ({sum(weights[t] for t in mine):,} B), {len(self.away)} on "
                 f"{self.tasks - 1} helper tasks ({sum(len(t) for t in todo)} to build; {len(put)} of {len(need)} inputs uploaded, "
                 f"{sum(os.path.getsize(f) for f in put.values()):,} B)")
        self._submit([t for t in todo if t])
        return set(self.away)

    def collect(self, paths: dict[tuple[str, int], str]) -> dict[tuple[str, int], dict]:
        """Wait until every task of `paths` (`{(kind, i): local cells file}`) is built by a helper, dealing the missing
        ones again when a helper job ends without them; then download each one's cells to its file, and wait for the
        helper job's end. Each task's `_shard_task` doc."""
        import shutil

        from . import heartbeat as hb

        out: dict[tuple[str, int], dict] = {}
        docdir = Path(next(iter(paths.values()))).parent / ".fetch-done" if paths else None
        hb.set_stage(f"anchors/rollups: 0/{len(paths)} helper shard tasks collected")

        def fetch(units: list[tuple[str, int]], listing: dict[str, int]) -> None:
            files, docs = {}, {}
            for kind, i in units:
                base = anchor_task_key(kind, i)
                files[f"{base}/cells.parquet"] = Path(paths[(kind, i)])
                docs[(kind, i)] = docdir / f"{kind}-{i:05d}.json"
                files[f"{base}/done.json"] = docs[(kind, i)]
            self.fetch(files, listing, "anchors fan-out: shard tasks")
            for u, f in docs.items():
                out[u] = json.loads(f.read_text())["doc"]
                f.unlink()
            hb.set_stage(f"anchors/rollups: {len(out)}/{len(paths)} helper shard tasks collected")

        self.wait(sorted(paths), "shard tasks", lambda us: [f"{k}/s{i:05d}" for k, i in us], fetch)
        if docdir is not None:
            shutil.rmtree(docdir, ignore_errors=True)
        self.log(f"anchors fan-out: {len(out)} shard tasks from the helpers")
        self.finish()
        return out
