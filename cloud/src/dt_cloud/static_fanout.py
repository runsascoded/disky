"""A compaction's heavy parts fanned out over helper Batch tasks: the drill fold (specs/static-fold-fanout.md), the drill
measurement (specs/static-measure-fanout.md) and the base anchors' rollups (specs/static-anchors-fanout.md).

`FoldFanout` (the drill fold, `static_compact._fanned_fold`): units of the fold (a long chunk's digests or canonical
spills, a short range's spills) dealt in contiguous shares (`contiguous`) over waves, one helper job each (`static_merge
fold-shard`); the spills land under `spilled/files/`, listed by each unit's `done.json`, and the `spilled` marker is
their union. The tuples' digests come from the tiers' persisted ones (`PersistedClasses`: no digest wave) or, where
those are missing, a digest wave first (`DigestClasses`).

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

`WriteFanout` (the drill write, `static_drill.write_kind`; specs/static-write-fanout.md): with the measurement fanned
out, every range's sorted roots are under the checkpoint (`measured/<kind>/g<g>/sorted.parquet`: the helpers' and share
0's, `Fanout.keep`); the roots' output files (cut at `q` boundaries from the ranges' counts, as one VM cuts them) are
dealt by rows, share 0 written here, a helper (`static_merge write-shard`) fetching its files' ranges and uploading
each file and its group index rows to `written/<kind>/r<i>/`; `WriteFanout.collect` downloads them where this VM's
would be, so the drill is a single VM's.

`AnchorFanout` (the base anchors, `static_anchors._tier_parallel`): each `(kind, shard)` rollup task (`end` over the
suffix shards, `exact` and `start` over the name shards) dealt by input rows (footer); the inputs of the tasks dealt away
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
#: Under the fold's checkpoint: the write helpers' drill files and group index rows, per output file, and their plans.
WRITTEN = "written"
#: Under the compaction's checkpoint: the anchors helpers' sorted cells, per task, and the plans that deal them.
ANCHORED = "anchored"
#: Under the compaction's checkpoint: the shard files the anchors helpers read (`anchor_input_key`).
ANCHORS_IN = "anchors-in"


def range_key(kind: str, g: int) -> str:
    return f"{MEASURED}/{kind}/g{g:06d}"


def write_key(kind: str, i: int) -> str:
    return f"{WRITTEN}/{kind}/r{i:04d}"


def sorted_key(kind: str, f: Path | str) -> str:
    """A measured range's sorted roots under the checkpoint, by its local file (`<kind>-sorted/g<g>.parquet`)."""
    return f"{range_key(kind, int(Path(f).stem.removeprefix('g')))}/sorted.parquet"


def anchor_task_key(kind: str, i: int) -> str:
    return f"{ANCHORED}/{kind}/s{i:05d}"


def anchor_input_key(kind: str, i: int) -> str:
    """Shard `i`'s input file of an anchors task of `kind`: a suffix shard (`end`), or a name shard (`exact` and `start`
    read the same one)."""
    return f"{ANCHORS_IN}/{'sx' if kind == 'end' else 'names'}/s{i:05d}.parquet"


#: Under the fold's checkpoint: the spills' files (`spilled/done.json` lists them; a measurement helper fetches its ranges').
SPILLED_FILES = "spilled/files"


def fetch_files(ck, files: dict[str, Path], listing: dict[str, int], stage: str, *, label: str, log: Callable[[str], None] = err) -> None:
    """Checkpoint `ck`'s files (`{key under its prefix: local target}`, sizes from `listing`, by full key) downloaded in
    parallel (`blobstore.stage_files`: sliced ranged GETs over processes from `gs://`, threads elsewhere) into a `.fetch/`
    dir beside the targets, then moved into place. A file still failing after its retries raises (`label` says whose)."""
    import os
    import shutil

    from .blobstore import stage_files

    if not files:
        return
    full = {ck.key(k): dst for k, dst in files.items()}
    tmp = Path(os.path.commonpath([str(Path(d).parent) for d in full.values()])) / ".fetch"
    got = stage_files(ck.store.uri(""), {k: listing[k] for k in full}, tmp, label=stage, log=log)
    if got["failed"]:
        raise RuntimeError(f"{label}: {len(got['failed'])} files not fetched: {got['failed'][:5]}")
    for k, dst in full.items():
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp / k, dst)
    shutil.rmtree(tmp, ignore_errors=True)


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
        # the helper jobs before the current wave's (`FoldFanout`'s waves each get `retries` re-deals of their own)
        self.base = 0
        self.thread: threading.Thread | None = None
        # a presubmitted job's plan key, until the first `_submit` writes it
        self.early: str | None = None
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

    def deal(self, weights: dict[tuple[str, int], int], away: set[tuple[str, int]] = frozenset()) -> tuple[set[tuple[str, int]], list[list[list]]]:
        """`weights`' units dealt over the shares (`static_merge.deal`; a unit of `away` never to share 0): share 0
        (kept), and the other shares' units not done already (an earlier attempt's helpers'), as plan entries; `away` =
        every unit not in share 0."""
        from .static_merge import deal

        shares = deal(weights, self.tasks, away)
        done = self.done_in(self.listed(), weights)
        # share 0's units already in the checkpoint (a seeded or resumed run's) are fetched, not built again
        mine = set(shares[0]) - done
        self.away = set(weights) - mine
        return mine, [[[k, i] for k, i in share if (k, i) not in done] for share in shares[1:]]

    def _write_plan(self, plan: str, shares: list[list]) -> None:
        self.ck.store.write_text(self.ck.key(plan), json.dumps({"tasks": shares, "code": self.code, **self.params}) + "\n")

    def _run(self, plan: str, n: int) -> None:
        url = self.ck.store.uri(self.ck.prefix)

        def run() -> None:
            try:
                self.submit(url, plan, n, self.code, self.scan)
            except BaseException as e:  # noqa: BLE001 — the waiting side reports it, with the units it left
                self.error = e

        self.error = None
        self.thread = threading.Thread(target=run, name=f"{self.root}-helpers-{self.attempt}", daemon=True)
        self.thread.start()

    def presubmit(self) -> None:
        """Submit the first helper job now, before its plan exists (`plan-1.json`, a stale one from an earlier run of
        this checkpoint deleted first), with a task per helper share: its VMs boot while this VM still works (gcs's
        anchors: ~2 min of boot beside `anchors/names`' ~3 min). Its tasks wait for the plan, which the first
        `_submit` writes instead of submitting; one past the plan's shares ends at once (`abandon`: an empty plan)."""
        self.attempt += 1
        self.early = f"{self.root}/plan-{self.attempt}.json"
        self.ck.store.delete([self.ck.key(self.early)])
        self.log(f"{self.label}: {self.early}: {self.tasks - 1} helper tasks submitted ahead of their plan")
        self._run(self.early, self.tasks - 1)

    def abandon(self) -> None:
        """A presubmitted job's plan never coming: an empty one, so its tasks end instead of waiting."""
        if self.early is not None:
            plan, self.early = self.early, None
            self._write_plan(plan, [])

    def _submit(self, shares: list[list]) -> None:
        if self.early is not None:
            plan, self.early = self.early, None
            self._write_plan(plan, shares)
            self.log(f"{self.label}: {plan}: {len(shares)} shares for the presubmitted helper tasks")
            return
        if not shares:
            return
        self.attempt += 1
        plan = f"{self.root}/plan-{self.attempt}.json"
        self._write_plan(plan, shares)
        self.log(f"{self.label}: {plan}: {len(shares)} helper tasks submitted")
        self._run(plan, len(shares))

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
                if self.attempt - self.base > self.retries:
                    raise RuntimeError(f"{self.label}: {what} {ids(missing)} {self.undone} after {self.attempt - self.base} helper jobs{why}")
                self.log(f"{self.label}: helper job {self.attempt} ended{why}; dealing {len(missing)} {what} again")
                pend = {u: 1 for u in self.away - self.done_in(listing, self.away)}
                self._submit([[[k, i] for k, i in share] for share in deal(pend, self.tasks - 1) if share])
            time.sleep(self.poll_s)

    def fetch(self, files: dict[str, Path], listing: dict[str, int], stage: str) -> None:
        """Checkpoint files (`{key under root/: local target}`, sizes from `listing`) downloaded (`fetch_files`)."""
        fetch_files(self.ck, files, listing, stage, label=self.label, log=self.log)

    def finish(self) -> None:
        """Wait for the helper job's end (every unit is done by now; its tasks are exiting)."""
        if self.thread is not None:
            self.thread.join()


class Fanout(Helpers):
    """The drill measurement's fan-out: `start` deals and submits, `groups` names a kind's ranges the helpers measure,
    `collect` waits for and fetches them. `ck`: the fold's checkpoint (`spilled` saved)."""

    root, label, undone = MEASURED, "measure fan-out", "unmeasured"

    def __init__(self, *args, write_submit: Callable[[str, str, int, str, str], None] | None = None, **kw):
        super().__init__(*args, **kw)
        self.write_submit = write_submit

    def unit_key(self, kind: str, g: int) -> str:
        return range_key(kind, g)

    def writer(self) -> WriteFanout | None:
        """The drill write's fan-out over the same tasks and checkpoint (`write_submit` its helper jobs), or None."""
        if self.write_submit is None:
            return None
        return WriteFanout(self.ck, self.tasks, self.write_submit, code=self.code, scan=self.scan, poll_s=self.poll_s, retries=self.retries,
                           log=self.log)

    def start(self, work: Path, groups: dict[str, int], *, R: int, K: int, floor: int, measure: bool = True) -> None:
        """Deal `groups` (`{kind: ranges}`) over the shares by spilled roots (the checkpoint's `spilled` marker's files
        and their rows, `static_merge.SPILL_ROWS`; their listed bytes for a fold that predates it: `static_merge._spilled`), keep share 0, fetch its spills not on the work disk into `work`
        (a resumed fold restores only the `spilled` step's tables, not every spill), and submit the helpers for the rest
        (ranges already measured by an earlier attempt's helpers aren't dealt again); the plan carries the measurement's
        `R`, `K`, `floor`, and `measure` (false: the helpers only sort each range's roots, an incremental compaction's:
        `static_local.finish_drill`)."""
        self.params = {"R": R, "K": K, "floor": floor, "measure": measure}
        from . import heartbeat as hb
        from .static_merge import _spilled, spill_rows

        hb.set_stage("drill/measure/fan-out plan")
        sizes, by = _spilled(self.ck)
        # by roots, as the ranges' seconds go (gcs c001: r 0.78 long, 0.98 short; bytes 0.33 / 0.65, the compression varies)
        rows = spill_rows(self.ck, list(sizes))
        per, unit = (rows, "roots") if rows is not None else (sizes, "B")
        weights = {(kind, g): w for (kind, g), rels in by.items() if g < groups.get(kind, 0) and (w := sum(per[r] for r in rels))}
        mine, todo = self.deal(weights)
        need = {r: work / r for u in sorted(mine) for r in by[u] if not (work / r).is_file()}
        self.log(f"measure fan-out: {len(mine)} ranges here ({sum(weights[r] for r in mine):,} {unit}; {len(need)} spill files, "
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


class WriteFanout(Helpers):
    """The drill write's fan-out (the roots of a kind): `start` deals the output files by rows and submits the helpers,
    `collect` waits for and fetches their files and group index rows. `ck`: the fold's checkpoint (`measured/` there)."""

    root, label, undone = WRITTEN, "write fan-out", "unwritten"

    def unit_key(self, kind: str, i: int) -> str:
        return write_key(kind, i)

    def start(self, kind: str, files: list[tuple[str, str | None, str | None, list[tuple[Path, str, str]], int]], *, rg: int) -> set[int]:
        """Deal `files` (`(name, lo, hi, spans, rows)` per output file in order; `spans`: the sorted range files it
        reads, `(local file, first q, last q)`) by rows, keep share 0, and submit the helpers for the rest (the files an
        earlier attempt's helpers wrote aren't dealt again); the plan carries each file's name, `q` bounds and its spans'
        checkpoint keys (`sorted_key`), and the row-group size `rg`. Returns the files dealt away."""
        from . import heartbeat as hb

        hb.set_stage(f"drill/write/{kind}/fan-out plan")
        self.base = self.attempt  # each kind's files get `retries` re-deals of their own
        weights = {(kind, i): max(1, f[4]) for i, f in enumerate(files)}
        mine, todo = self.deal(weights)
        away = sorted(i for _, i in self.away)
        self.params = {"kind": kind, "rg": rg, "files": {str(i): {"name": files[i][0], "lo": files[i][1], "hi": files[i][2],
                                                                 "spans": [[sorted_key(kind, f), a, b] for f, a, b in files[i][3]]} for i in away}}
        self.log(f"write fan-out: {kind}: {len(mine)} files here ({sum(weights[u] for u in mine):,} rows), {len(away)} on "
                 f"{self.tasks - 1} helper tasks ({sum(len(t) for t in todo)} to write)")
        self._submit([t for t in todo if t])
        return set(away)

    def collect(self, kind: str, paths: dict[int, Path]) -> dict[int, tuple[int, object, int]]:
        """Wait until every file of `paths` (`{i: local target}`) is written by a helper, dealing the missing ones again
        when a helper job ends without them; then download each with its group index rows. Each file's `(rows, group
        index table, bytes)`, as `static_drill._write_chunk_file` returns them."""
        import shutil

        import pyarrow.parquet as pq

        from . import heartbeat as hb
        from .static_roots import GROUP_INDEX_SCHEMA

        out: dict[int, tuple[int, object, int]] = {}
        if not paths:
            return out
        side = Path(next(iter(paths.values()))).parent / ".fetch-written"
        hb.set_stage(f"drill/write/{kind}: 0/{len(paths)} helper files collected")

        def fetch(units: list[tuple[str, int]], listing: dict[str, int]) -> None:
            files = {}
            for _, i in units:
                base = write_key(kind, i)
                files[f"{base}/file.parquet"] = paths[i]
                files[f"{base}/index.parquet"] = side / f"r{i:04d}-index.parquet"
                files[f"{base}/done.json"] = side / f"r{i:04d}.json"
            self.fetch(files, listing, f"write fan-out: {kind} files")
            for _, i in units:
                doc = json.loads((side / f"r{i:04d}.json").read_text())
                # as the writer made it: the parquet round trip changes the schema, which the index's concat would keep
                out[i] = (doc["rows"], pq.read_table(side / f"r{i:04d}-index.parquet").cast(GROUP_INDEX_SCHEMA).replace_schema_metadata(None),
                          doc["bytes"])
            hb.set_stage(f"drill/write/{kind}: {len(out)}/{len(paths)} helper files collected")

        self.wait([(kind, i) for i in paths], f"{kind} files", lambda us: [i for _, i in us], fetch)
        shutil.rmtree(side, ignore_errors=True)
        self.log(f"write fan-out: {len(out)} {kind} files from the helpers ({sum(v[2] for v in out.values()) / 2**30:,.1f} GiB)")
        self.finish()
        return out


class AnchorFanout(Helpers):
    """The base anchors' fan-out: `start` deals the `(kind, shard)` rollup tasks by input rows, uploads the inputs of
    the tasks dealt away and submits them; `collect` waits for and fetches their cells. `ck`: the compaction's
    checkpoint."""

    root, label, undone = ANCHORED, "anchors fan-out", "not built"

    def unit_key(self, kind: str, i: int) -> str:
        return anchor_task_key(kind, i)

    def start(self, tasks: list[tuple[str, int, str]], *, R: int, K: int, rule=None, workers: int = 16) -> set[tuple[str, int]]:
        """Deal `tasks` (`(kind, shard index, input file)`) over the shares by input rows (footers), keep share 0, upload the
        inputs the other shares' tasks not done yet read (each once; one already there is kept: a resumed compaction's
        shards are the same bytes), and submit the helpers. The plan carries `R`, `K` and the hex-run `rule`. Returns
        the tasks dealt away."""
        import os
        from concurrent.futures import ThreadPoolExecutor

        import pyarrow.parquet as pq

        from . import heartbeat as hb
        from .hex_runs import rule_json
        from .static_anchors import heavy_rows

        self.params = {"R": R, "K": K, **rule_json(rule)}
        hb.set_stage("anchors/fan-out plan")
        # by the inputs' footer rows, as `anchor-shard` orders and threads them (bytes put gcs's ~108M-row, ~300 MB `end`
        # shards under the median: tasks of 245–874s)
        weights = {(kind, i): pq.ParquetFile(f).metadata.num_rows for kind, i, f in tasks}
        files = {(kind, i): f for kind, i, f in tasks}
        # a heavy shard to a helper, which builds it alone on all its threads (`anchor-shard`); this VM's pool would give it
        # `ANCHOR_THREADS` (gcs's deal race: a 261M-row `end` shard in share 0, 1170s at 4 threads)
        # the bar over every task (the plan carries it: each helper judges its own by the same one)
        heavy = self.params["heavy_rows"] = heavy_rows(list(weights.values()))
        mine, todo = self.deal(weights, {u for u, w in weights.items() if w >= heavy})
        need = {anchor_input_key(k, i): files[(k, i)] for share in todo for k, i in share}
        have = self.ck.store.listing(self.ck.key(ANCHORS_IN))
        put = {key: f for key, f in need.items() if have.get(self.ck.key(key)) != os.path.getsize(f)}
        hb.set_stage(f"anchors/fan-out inputs: {len(put)} shard files")
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(lambda kf: self.ck.store.put(Path(kf[1]), self.ck.key(kf[0])), sorted(put.items())))
        self.log(f"anchors fan-out: {len(mine)} shard tasks here ({sum(weights[t] for t in mine):,} rows), {len(self.away)} on "
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


# ── The drill fold (specs/static-fold-fanout.md) ───────────────────────────

#: Under the fold's checkpoint: the fold helpers' units (`folded/<kind>/<unit>/done.json`), plans and task reports.
FOLDED = "folded"
#: The fold fan-out's unit kinds: a long chunk's tuples' digests (the digest wave), a long chunk's canonical roots
#: spilled by `q` range (`RELONG`: again, once the classes changed under it), a short range's roots spilled.
DIGEST, LONG, RELONG, SHORT = "digest", "long", "relong", "short"


def fold_unit_key(kind: str, i: int) -> str:
    return f"{FOLDED}/{kind}/{'s' if kind == SHORT else 'c'}{i:06d}"


def contiguous(weights: dict, n: int) -> list[list]:
    """`weights`' keys, in order, cut into `n` contiguous shares of about equal weight: each key goes to the share its
    weight's midpoint falls in, so a share is within one key's weight of the mean (a heavy key, e.g. a single-tuple chunk
    of 440M roots, is most of a share rather than doubling one). Contiguity keeps a share's inputs (the base's `q`-sorted
    roots files, the runs' consecutive spill partitions) to about 1/n of them. Weights below 1 count as 1."""
    keys = sorted(weights)
    w = {k: max(1, weights[k]) for k in keys}
    total = sum(w.values())
    shares: list[list] = [[] for _ in range(n)]
    cum = 0
    for k in keys:
        shares[min(n - 1, (2 * cum + w[k]) * n // (2 * total))].append(k)
        cum += w[k]
    return shares


def gather_digests(con, fan: FoldFanout, chunks: list[int], dig: Path) -> None:
    """Table `tdig (tid, n, h1, h2)` from the digest wave's units' files (`folded/digest/c<ch>/dig.parquet`; those not in
    `dig` already, share 0's, fetched there; a chunk without roots has none)."""
    from .static_names import q

    listing = fan.listed()
    keys = {ch: f"{fold_unit_key(DIGEST, ch)}/dig.parquet" for ch in chunks}
    have = [ch for ch in chunks if fan.ck.key(keys[ch]) in listing]
    fan.fetch({keys[ch]: dig / f"c{ch:06d}.parquet" for ch in have if not (dig / f"c{ch:06d}.parquet").is_file()}, listing,
              "fold fan-out: digests")
    con.execute("CREATE OR REPLACE TABLE tdig (tid BIGINT, n BIGINT, h1 UBIGINT, h2 UBIGINT)")
    if have:
        con.execute(f"INSERT INTO tdig SELECT tid, n, h1, h2 FROM read_parquet([{', '.join(q(str(dig / f'c{ch:06d}.parquet')) for ch in have)}])")


class DigestClasses:
    """A class source that never derives `tdig` (`derive` is false): the fold fans out a wave of digest units first,
    one per long chunk (`static_compact.fold_units`: the chunk's roots combined and digested; only
    `folded/digest/c<ch>/dig.parquet` written), with the short ranges, and gathers them (`gather_digests`). Wave 2 then
    combines each chunk again from its inputs rather than shipping the combined roots between waves (logged)."""

    name = "roots"

    def derive(self, con, tiers: list, log: Callable[[str], None]) -> bool:
        return False


class PersistedClasses(DigestClasses):
    """The default class source: `tdig` derived before any root is read from the tiers' persisted digests
    (`static_compact._derive_digests`: the base's `drill/digests-long.parquet` plus the runs' per-class changes,
    specs/static-incremental-compaction.md step 3), so there is no digest wave: wave 2 carries the short ranges too, and
    each of its chunks checks its tuples' counts against its roots (`fold_units`; a mismatch's digests corrected after
    the wave, every chunk spilled again if that changed the classes). Where `digests_blocker` refuses (a base or run
    built before the digests), the digest wave as `DigestClasses`."""

    name = "digests"

    def derive(self, con, tiers: list, log: Callable[[str], None]) -> bool:
        from .static_compact import _derive_digests

        return _derive_digests(con, tiers, log)


class FoldFanout(Helpers):
    """The drill fold's fan-out (specs/static-fold-fanout.md): units `(kind, index)` (`DIGEST`/`LONG` chunks, `SHORT`
    ranges) run in waves, each wave one helper job (`static_merge fold-shard`) plus share 0 here. A unit writes its
    outputs, then its `done.json` (`folded/<kind>/<unit>/`; a spill's files listed there, themselves under
    `spilled/files/`), so a resumed wave deals only the units without one. `classes`: where `tdig` comes from
    (`PersistedClasses`, the default; `DigestClasses`: always the digest wave). `ck`: the fold's checkpoint."""

    root, label, undone = FOLDED, "fold fan-out", "not folded"

    def __init__(self, *a, classes=None, **kw):
        super().__init__(*a, **kw)
        self.classes = classes if classes is not None else PersistedClasses()

    def unit_key(self, kind: str, i: int) -> str:
        return fold_unit_key(kind, i)

    def wave(self, n: int, weights: dict[tuple[str, int], int], here: Callable[[list[tuple[str, int]]], None],
             basis: str = "estimated") -> None:
        """Wave `n`: `weights`' units (their rows: `basis` says which are exact, the persisted digests' counts, and which
        estimated) not done already, cut into contiguous shares (`contiguous`; each share's rows logged), share 0 run here (`here(units)`), the rest submitted as one helper job; then wait
        for every unit (re-dealing what a job left, `retries` times in this wave) and for the job's end."""
        from . import heartbeat as hb

        hb.set_stage(f"drill/fold/wave {n}: plan")
        self.base = self.attempt
        self.params = {"wave": n}
        done = self.done_in(self.listed(), weights)
        pending = {u: w for u, w in weights.items() if u not in done}
        shares = contiguous(pending, self.tasks) if pending else [[] for _ in range(self.tasks)]
        mine = shares[0]
        self.away = set(pending) - set(mine)
        rows = [sum(max(1, pending[u]) for u in s) for s in shares]
        mean = sum(rows) / len(rows)
        self.log(f"fold fan-out: wave {n}: {len(pending):,} of {len(weights):,} units to fold ({len(done):,} done), {len(mine):,} here, "
                 f"{len(self.away):,} on {self.tasks - 1} helper tasks; rows per share ({basis}) {rows} "
                 f"(mean {mean:,.0f}, max/mean {max(rows) / mean if mean else 0:.2f})")
        self._submit([[[k, i] for k, i in s] for s in shares[1:] if s])
        hb.set_stage(f"drill/fold/wave {n}: {len(mine):,} units here")
        here(sorted(mine))
        hb.set_stage(f"drill/fold/wave {n}: waiting for {len(self.away):,} helper units")
        self.wait(sorted(self.away), "units", lambda us: [fold_unit_key(k, i).removeprefix(f"{FOLDED}/") for k, i in us])
        self.finish()
        hb.set_stage(f"drill/fold/wave {n}: done")  # not left "waiting" through what follows (gathering, the classes)

    def unit_docs(self, units: list[tuple[str, int]], workers: int = 32) -> dict[tuple[str, int], dict]:
        """Each unit's `done.json`."""
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(workers) as ex:
            docs = list(ex.map(lambda u: self.ck.store.read_json(self.ck.key(f"{fold_unit_key(*u)}/done.json")), units))
        return dict(zip(units, docs))
