"""One append runner for the run stores (specs/index-landscape.md §5, recommendation 1): the static name index's runs
(`static_runner`, `static_merge`) and the interval store's (`interval_append`) share everything here; each store plugs
in its stage list (`Runner.one`), its merged-run build (`Carry.build`) and its reader files (`Carry.missing`).

A run store is a base generation `<prefix>/<gen>/` plus one small **run** per newer scan (`deltas/<first>[_<last>]/`),
listed by immutable manifests:

- **Order.** Scans are appended strictly in scan-id order (`pending_scans`): exit `NOT_NEXT` (3) when SCAN_ID is not
  published yet or an earlier published scan is pending; `-c` catches up every pending scan in order.
- **Stages** (`Runner`): each skipped when its output exists, so a rerun resumes at the first missing one; Batch jobs
  submitted over the API and polled (`BatchRunner`, at most every 30 s), several at once where they're independent
  (`Runner.concurrently`), labelled for cost attribution (`job_spec`, `cost_labels`).
- **Publish** adds only the scan's own level-0 run: `manifests/<id>.json`, written once, last, after every file of every
  run it lists exists.
- **Deferred carries** (`Runner.carries`, after the scans, non-fatal): the binary counter replayed over the newest
  manifest's runs (`plan_carries`), carries that chain folded into one N-way merge, none reaching the profile's
  `compact_level` (a compaction's job; none when it is unbounded). A Batch job (`merge_pending`) builds each merged
  run into its own dir and publishes a **revision** `manifests/<id>.m<NNN>.json` of the newest manifest
  (`publish_revision`), rebased onto a scan's manifest that lands
  meanwhile; one merger per generation (a lease in the scratch bucket); an interrupted merge leaves the newest manifest
  as it was and resumes (a whole merged dir is reused). Nothing is deleted. The lease's holder mirrors it to R2 beside
  the store (`<prefix>/<gen>/merging.json`, `MergeStatus`; best-effort, never failing the merge) so `/health` can show
  a merge in progress, or stuck.
- **R2**: each manifest's runs (their liveness markers last), checked there, then the manifest, last (`r2_publish`).
- **Prune**: the scratch bucket's earlier open-version states, once the scan's is complete and published (`prune_state`).

Readers take the greatest manifest key (`manifest_keys`: `<scan id>.json` or `<scan id>.m<NNN>.json`, which sort by scan,
then revision), so a revision is served as soon as it is written.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import sys
import threading
import time
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Protocol

from click import ParamType

from .cost_labels import label_batch_spec
from .scan_id import SCAN_ID
from .static_profile import COMPACT_LEVEL, Profile, parse_compact_level


class Level(ParamType):
    """`-L`: a compaction level, an integer ≥ 1 or `none` (unbounded; `static_profile.parse_compact_level`)."""
    name = "level"

    def convert(self, value, param, ctx):
        if value is None or isinstance(value, int):
            return value
        try:
            return parse_compact_level("-L", value)
        except SystemExit as e:
            self.fail(str(e), param, ctx)


LEVEL_HELP = f"Compaction level: no carry reaches it; `none`: unbounded (default {COMPACT_LEVEL})"


def err(*a, **kw) -> None:
    print(*a, file=sys.stderr, flush=True, **kw)


#: Exit status: the scan is not published yet, or is not the next one to append.
NOT_NEXT = 3
#: Revision suffix width: `<id>.m001.json` … `<id>.m999.json` sort in revision order.
REV_DIGITS = 3
MANIFEST = re.compile(rf"(?P<scan>[^/]+?)(?:\.m(?P<rev>\d{{{REV_DIGITS}}}))?\.json")
#: The merge lease's lifetime: a Batch task's `maxRunDuration` (`job_spec`), so a crashed holder's lease never blocks the
#: next merge for longer than its job could have run.
LEASE_S = 4 * 3600
#: A Batch task's longest run (`job_spec`).
MAX_RUN_S = 4 * 3600
#: The merge status record's name under the generation (`<prefix>/<gen>/merging.json` on R2): `/health` reads it by key.
#: No reader lists the generation's root (manifests are found under `manifests/`, by name), so it never reads as data.
MERGE_STATUS = "merging.json"
#: How often a merge holding the lease refreshes its status record (`updated_ts`): `/health` calls a record stuck after
#: ~6 missed beats (`STUCK_S` there, 30 min) or once it outlives `LEASE_S`.
HEARTBEAT_S = 300
#: The merge job's env var naming its Batch job (`Runner.carries`): the status record's `holder`.
JOB_NAME_VAR = "DT_JOB_NAME"


class NotNext(Exception):
    """The scan can't be appended now (not published, or an earlier published scan is pending): exit 3."""


class StillRunning(Exception):
    """A Batch job outlived the wait given for it (it keeps running; nothing is cancelled)."""


class Superseded(Exception):
    """A merge's inputs are no longer consecutive runs of the newest manifest: nothing to publish."""


class StateIncomplete(Exception):
    """`prune` refused: the scan's state is not complete (or its run not published), so nothing is deleted."""


# ── Order ──────────────────────────────────────────────────────────────────


def pending_scans(have: list[str], published: list[str], scan_id: str, catch_up: bool) -> list[str]:
    """The scans to append for a run given `scan_id`, oldest first: the published scans after `have[-1]` (the
    generation's newest: base + live runs) through `scan_id`. Raises `NotNext` when `scan_id` is not published, or
    when an earlier published scan is pending and not `catch_up`. `[]` when `scan_id` is already appended."""
    if not SCAN_ID.fullmatch(scan_id):
        raise ValueError(f"{scan_id!r} is not a scan id")
    if scan_id in have:
        return []
    if scan_id <= have[-1]:
        raise NotNext(f"{scan_id} precedes the generation's newest scan {have[-1]} but was never appended: it can't join now")
    if scan_id not in published:
        raise NotNext(f"{scan_id} is not published (no path sort under the generation's layouts)")
    todo = sorted(s for s in published if have[-1] < s <= scan_id)
    if todo[0] != scan_id and not catch_up:
        raise NotNext(f"{len(todo) - 1} earlier published scan(s) pending: {', '.join(todo[:-1])} (append them first, or -c)")
    return todo


# ── Runs and manifests ─────────────────────────────────────────────────────


def run_key(first: str, last: str) -> str:
    """A run's directory under the generation: `deltas/<first>` (one scan) or `deltas/<first>_<last>`."""
    return f"deltas/{first}" if first == last else f"deltas/{first}_{last}"


def manifest_name(scan: str, rev: int = 0) -> str:
    """`<scan>.json` (a scan's `publish`), or `<scan>.m<NNN>.json` (a merge's revision of it)."""
    if not 0 <= rev < 10 ** REV_DIGITS:
        raise ValueError(f"manifest revision {rev}: outside 0..{10 ** REV_DIGITS - 1}")
    return f"{scan}.json" if rev == 0 else f"{scan}.m{rev:0{REV_DIGITS}d}.json"


def parse_manifest(name: str) -> tuple[str, int] | None:
    """`(scan, rev)` of a manifest's file name (`rev` 0 for a scan's own), or None for anything else."""
    m = MANIFEST.fullmatch(name)
    return (m["scan"], int(m["rev"] or 0)) if m and SCAN_ID.fullmatch(m["scan"]) else None


def manifest_keys(keys: Collection[str]) -> list[str]:
    """The manifests among generation-relative `keys` (`manifests/<name>`), oldest first: by key, which is by scan, then
    revision."""
    return sorted(k for k in keys if k.startswith("manifests/") and parse_manifest(k.removeprefix("manifests/")))


def latest_key(keys: Collection[str], before: str | None = None) -> str | None:
    """The newest manifest among generation-relative `keys` (of a scan strictly before `before`, when given), or None."""
    ms = [k for k in manifest_keys(keys) if before is None or parse_manifest(k.removeprefix("manifests/"))[0] < before]
    return ms[-1] if ms else None


def scans_of(base: dict, runs: list[dict]) -> list[str]:
    """The generation's scans: its base's (`scans.json`), then the runs'."""
    return [*(s["id"] for s in base["scans"]), *(s for r in runs for s in r["scans"])]


#: The fields of a run a manifest lists.
RUN_FIELDS = ("key", "first", "last", "level", "scans", "rows", "bytes")


def listed(runs: list[dict]) -> list[dict]:
    return [{k: r[k] for k in RUN_FIELDS if k in r} for r in runs]


# ── The plan ───────────────────────────────────────────────────────────────


def plan_carries(runs: list[dict], drilled: Collection[str] = frozenset(), max_level: int | None = COMPACT_LEVEL) -> tuple[list[dict], list[tuple[list[dict], dict]]]:
    """The binary counter replayed over `runs` (oldest first): each pushed in turn, and while the two newest share a level
    (and both carry a drill or neither: a merge of one with and one without would drop the one's drill, the reader
    stopping at the first run without one) they carry into one a level up. Carries that chain fold into one merge of every
    run they consumed. A carry never reaches `max_level` (a compaction's job; None: unbounded, carries at every level). Returns the runs
    after and the merges (`(inputs, output)`, oldest first; their inputs are disjoint)."""
    drilled = set(drilled)
    stack: list[tuple[dict, list[dict]]] = []
    for r in runs:
        stack.append((r, [r]))
        while len(stack) >= 2:
            (a, ia), (b, ib) = stack[-2], stack[-1]
            if a["level"] != b["level"] or (max_level is not None and a["level"] + 1 >= max_level) or (a["key"] in drilled) != (b["key"] in drilled):
                break
            m = {"key": run_key(a["first"], b["last"]), "first": a["first"], "last": b["last"], "level": a["level"] + 1,
                 "scans": [*a["scans"], *b["scans"]]}
            if a["key"] in drilled:
                drilled.add(m["key"])
            stack[-2:] = [(m, ia + ib)]
    return [r for r, _ in stack], [(ins, r) for r, ins in stack if len(ins) > 1]


def rebase(runs: list[dict], inputs: list[str], out: dict) -> list[dict] | None:
    """`runs` with the consecutive runs keyed `inputs` replaced by `out`; None when `out` is already listed. Raises
    `Superseded` when they aren't there consecutively."""
    keys = [r["key"] for r in runs]
    if out["key"] in keys:
        return None
    for i in range(len(keys) - len(inputs) + 1):
        if keys[i:i + len(inputs)] == inputs:
            return [*runs[:i], out, *runs[i + len(inputs):]]
    raise Superseded(f"{out['key']}: inputs {inputs} are not consecutive runs of the newest manifest ({keys})")


# ── Storage ────────────────────────────────────────────────────────────────


class RunStore(Protocol):
    """A generation's keys (relative to `<prefix>/<gen>/`) in the data bucket, and the merge lease (scratch)."""

    gen: str

    def listing(self, prefix: str) -> dict[str, int]:
        """`{key: size}` under `prefix`."""
    def keys(self, prefix: str) -> list[str]: ...
    def exists(self, key: str) -> bool: ...
    def read_json(self, key: str) -> dict: ...
    def create(self, key: str, text: str) -> None:
        """Write a new key; `FileExistsError` if it exists (never overwrites)."""
    def upload(self, local: Path, prefix: str, last: tuple[str, ...] = ("meta.json",)) -> list[dict]:
        """Every file under `local` → `prefix/<rel>`, the `last` (relative paths, in that order) after all others;
        `{key, size}` per file."""
    def lease(self, owner: str, now: datetime, ttl_s: int) -> dict | None:
        """Take the merge lease for `owner`: None when taken, else the holder's record."""
    def release(self, owner: str) -> None: ...


def lease_stale(held: dict, now: datetime, ttl_s: int) -> bool:
    return (now - datetime.fromisoformat(held["at"])).total_seconds() > ttl_s


class LocalRunStore:
    """`RunStore` over local dirs: the generation's (`root`) and the scratch side's (tests, and a local dry run)."""

    def __init__(self, root: Path, scratch: Path, gen: str = "local"):
        self.root, self.scratch, self.gen = Path(root), Path(scratch), gen

    def listing(self, prefix: str) -> dict[str, int]:
        return {k: p.stat().st_size for p in sorted(self.root.rglob("*")) if p.is_file() and (k := p.relative_to(self.root).as_posix()).startswith(prefix)}

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.listing(prefix))

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()

    def read_json(self, key: str) -> dict:
        return json.loads((self.root / key).read_text())

    def create(self, key: str, text: str) -> None:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x") as fh:
            fh.write(text)

    def upload(self, local: Path, prefix: str, last: tuple[str, ...] = ("meta.json",)) -> list[dict]:
        rank = {r: i for i, r in enumerate(last)}
        files = sorted((p for p in local.rglob("*") if p.is_file()), key=lambda p: (rank.get(p.relative_to(local).as_posix(), -1), p))
        out = []
        for p in files:
            key = f"{prefix}/{p.relative_to(local).as_posix()}"
            (self.root / key).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, self.root / key)
            out.append({"key": key, "size": p.stat().st_size})
        return out

    def lease(self, owner: str, now: datetime, ttl_s: int) -> dict | None:
        p = self.scratch / "merge.lease.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                with open(p, "x") as fh:
                    fh.write(json.dumps({"owner": owner, "at": now.isoformat()}) + "\n")
                return None
            except FileExistsError:
                held = json.loads(p.read_text())
                if not lease_stale(held, now, ttl_s):
                    return held
                p.unlink()
        return json.loads(p.read_text())

    def release(self, owner: str) -> None:
        p = self.scratch / "merge.lease.json"
        if p.exists() and json.loads(p.read_text())["owner"] == owner:
            p.unlink()


class GcsRunStore:
    """`RunStore` over the data bucket (`gs://<bucket>/<prefix>/<gen>/`) and the scratch bucket's lease
    (`gs://<scratch>/<prefix>/<gen>/merge.lease.json`)."""

    def __init__(self, bucket: str, scratch: str | None, gen: str, *, prefix: str, client=None):
        """`scratch`: the lease's bucket (None: a store that never takes it, e.g. `publish`'s). `prefix`: the store's
        (`static-names`, `interval-store`)."""
        from google.cloud import storage

        self.client = client or storage.Client()
        self.bucket, self.scratch, self.gen, self.prefix = bucket, scratch, gen, f"{prefix}/{gen}"
        self.b = self.client.bucket(bucket)
        self.sb = self.client.bucket(scratch) if scratch else None

    def listing(self, prefix: str) -> dict[str, int]:
        return {x.name.removeprefix(self.prefix + "/"): int(x.size or 0) for x in self.client.list_blobs(self.bucket, prefix=f"{self.prefix}/{prefix}")}

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.listing(prefix))

    def exists(self, key: str) -> bool:
        return self.b.blob(f"{self.prefix}/{key}").exists()

    def read_json(self, key: str) -> dict:
        return json.loads(self.b.blob(f"{self.prefix}/{key}").download_as_bytes())

    def create(self, key: str, text: str) -> None:
        from google.api_core.exceptions import PreconditionFailed

        try:
            self.b.blob(f"{self.prefix}/{key}").upload_from_string(text, if_generation_match=0)
        except PreconditionFailed as e:
            raise FileExistsError(key) from e

    def upload(self, local: Path, prefix: str, last: tuple[str, ...] = ("meta.json",)) -> list[dict]:
        from .static_names import upload_tree

        return upload_tree(local, self.bucket, f"{self.prefix}/{prefix}", last=last)

    def lease(self, owner: str, now: datetime, ttl_s: int) -> dict | None:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        blob = self.sb.blob(f"{self.prefix}/merge.lease.json")
        for _ in range(2):
            try:
                blob.upload_from_string(json.dumps({"owner": owner, "at": now.isoformat()}) + "\n", if_generation_match=0)
                return None
            except PreconditionFailed:
                try:
                    blob.reload()
                    held = json.loads(blob.download_as_bytes(if_generation_match=blob.generation))
                    if not lease_stale(held, now, ttl_s):
                        return held
                    blob.delete(if_generation_match=blob.generation)
                except (NotFound, PreconditionFailed):
                    continue
        return {"owner": "?", "at": "?"}

    def release(self, owner: str) -> None:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        blob = self.sb.blob(f"{self.prefix}/merge.lease.json")
        try:
            blob.reload()
            if json.loads(blob.download_as_bytes(if_generation_match=blob.generation))["owner"] == owner:
                blob.delete(if_generation_match=blob.generation)
        except (NotFound, PreconditionFailed):
            pass


# ── Publish: a scan's level-0 run ──────────────────────────────────────────


def publish_scan(store: RunStore, scan: str, new: dict, doc: Callable[[dict, list[dict]], dict], missing: Callable[[list[dict]], list[str]],
                 refuse: Callable[[list[dict], list[dict]], str | None] = lambda before, after: None) -> dict:
    """Add scan `scan`'s level-0 run `new` to the newest earlier manifest's runs (a scan's, or a merge's revision of it)
    and write `manifests/<scan>.json`, once and last: `doc(prev, runs)` the manifest (`prev` the newest earlier manifest,
    or `{}`), checked first that no listed run lacks a file (`missing(runs)`) and that `refuse(before, after)` says
    nothing. No carries: those run apart (`merge_pending`). Raises `SystemExit` on a refusal."""
    key = f"manifests/{manifest_name(scan)}"
    if store.exists(key):
        raise SystemExit(f"{key} exists: manifests are never rewritten")
    prev = latest_key(store.keys("manifests/"), before=scan)
    m = store.read_json(prev) if prev else {}
    runs = m.get("runs", [])
    after = [*runs, {**new, "level": 0}]
    out = doc(m, after)
    if bad := missing(after):
        raise SystemExit(f"not publishing {key}: listed runs lack {bad}")
    if why := refuse(runs, after):
        raise SystemExit(f"not publishing {key}: {why}")
    store.create(key, json.dumps(out, indent=1) + "\n")
    return out


# ── Merge status: the lease, mirrored where the site reads ─────────────────


class MergeStatus(Protocol):
    """Where a merge mirrors its status record (`merging.json`): `put` replaces it, `delete` removes it (absent is fine)."""

    def put(self, doc: dict) -> None: ...
    def delete(self) -> None: ...


class LocalMergeStatus:
    """`MergeStatus` as a local file (tests, a local run)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def put(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc, indent=1) + "\n")

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class R2MergeStatus:
    """`MergeStatus` on R2 at `key` (`<prefix>/<gen>/merging.json`, beside the store /health reads)."""

    def __init__(self, client, bucket: str, key: str):
        self.client, self.bucket, self.key = client, bucket, key

    def put(self, doc: dict) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.key, Body=(json.dumps(doc, indent=1) + "\n").encode(),
                               ContentType="application/json", CacheControl="no-store")

    def delete(self) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.key)


def r2_merge_status(root: str, *, env: dict | None = None, log: Callable[[str], None] = err) -> R2MergeStatus | None:
    """The R2 status sink for the store at `root` (`<prefix>/<gen>`), from the R2 copy's env (`R2_ENDPOINT`, `R2_BUCKET`,
    `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`, else the AWS defaults), or None (logged) when the job has none: the merge
    runs the same, /health just doesn't see it. Short timeouts and few retries: a status write never holds a merge up."""
    env = os.environ if env is None else env
    if missing := [k for k in ("R2_ENDPOINT", "R2_BUCKET") if not env.get(k)]:
        log(f"merge status: no {', '.join(missing)} in the env; /health won't show this merge")
        return None
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3", endpoint_url=env["R2_ENDPOINT"], region_name="auto",
        aws_access_key_id=env.get("R2_ACCESS_KEY_ID"), aws_secret_access_key=env.get("R2_SECRET_ACCESS_KEY"),
        config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"}),
    )
    return R2MergeStatus(client, env["R2_BUCKET"], f"{root}/{MERGE_STATUS}")


def epoch(t: datetime) -> int:
    return int(t.timestamp())


class StatusMirror:
    """A merge's status record, mirrored best-effort to `sink` (None: nowhere): every write's failure is logged, never
    raised; while `beat` runs, a daemon thread refreshes `updated_ts` every `heartbeat_s`. The record:

    `{holder, owner, state: merging | failed, started_ts, updated_ts, heartbeat_s, lease_s, manifest, merges, done[, error]}`
    — `holder` the merge's Batch job (`$DT_JOB_NAME`, else the lease owner), `merges` the carries still planned
    (`{inputs, output, level, first, last, scans}`, the first being built), `done` the merged runs published so far
    (`{output, manifest}`), times in epoch s."""

    def __init__(self, sink: MergeStatus | None, owner: str, now: Callable[[], datetime], log: Callable[[str], None], heartbeat_s: float | None = HEARTBEAT_S):
        self.sink, self.now, self.log, self.heartbeat_s = sink, now, log, heartbeat_s
        self.doc: dict | None = None
        self.owner = owner
        self.lock = threading.Lock()
        self.stop_ = threading.Event()
        self.thread: threading.Thread | None = None

    def _put(self) -> None:
        try:
            self.sink.put(dict(self.doc))
        except Exception as e:  # best-effort: the merge never waits on, or fails for, its status
            self.log(f"merge status: write failed, not fatal: {e}")

    def write(self, manifest: str, merges: list[tuple[list[dict], dict]], done: list[dict]) -> None:
        if self.sink is None:
            return
        with self.lock:
            t = epoch(self.now())
            self.doc = {
                "holder": os.environ.get(JOB_NAME_VAR) or self.owner, "owner": self.owner, "state": "merging",
                "started_ts": self.doc["started_ts"] if self.doc else t, "updated_ts": t,
                "heartbeat_s": self.heartbeat_s, "lease_s": LEASE_S, "manifest": manifest,
                "merges": [{"inputs": [r["key"] for r in ins], "output": out["key"], "level": out["level"], "first": out["first"], "last": out["last"],
                            "scans": len(out["scans"])} for ins, out in merges],
                "done": [{"output": d["output"], "manifest": d["manifest"]} for d in done],
            }
            self._put()
        if self.heartbeat_s and self.thread is None:
            self.thread = threading.Thread(target=self._beat, name="merge-status", daemon=True)
            self.thread.start()

    def _beat(self) -> None:
        while not self.stop_.wait(self.heartbeat_s):
            with self.lock:
                if self.doc is None or self.doc["state"] != "merging":
                    return
                self.doc["updated_ts"] = epoch(self.now())
                self._put()

    def _stop(self) -> None:
        self.stop_.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

    def failed(self, e: BaseException) -> None:
        """The merge raised (the lease is released after): the record stays, `state: failed` with the error, until the
        next merge takes the lease."""
        self._stop()
        if self.sink is None or self.doc is None:
            return
        with self.lock:
            self.doc = {**self.doc, "state": "failed", "updated_ts": epoch(self.now()), "error": f"{type(e).__name__}: {e}"}
            self._put()

    def clear(self) -> None:
        """The merge ended (nothing more due, or `max_merges`): the record is removed — a stale or failed one an earlier
        holder left included."""
        self._stop()
        if self.sink is None:
            return
        try:
            self.sink.delete()
        except Exception as e:
            self.log(f"merge status: delete failed, not fatal: {e}")


# ── Carries: merged runs and revisions ─────────────────────────────────────


@dataclass(frozen=True)
class Carry:
    """What a store plugs into the deferred carries: `build(dirs, run, outp, *, drilled, tmp, log)` writes the merged run
    `run` of the input run dirs `dirs` (oldest first) into `outp`, its `meta.json` (naming `run["scans"]`) included, and
    returns `{rows, bytes, s}`; `missing(store, runs)` the reader files listed runs lack; `drilled(store, runs)` the runs
    carrying a drill (merged only with each other, `plan_carries`); `refuse(store, before, after)` why a revision listing
    `after` in place of `before` must not be published, or None."""
    build: Callable[..., dict]
    missing: Callable[[RunStore, list[dict]], list[str]]
    drilled: Callable[[RunStore, list[dict]], set[str]] = lambda store, runs: set()
    refuse: Callable[[RunStore, list[dict], list[dict]], str | None] = lambda store, before, after: None


def newest(store: RunStore) -> tuple[str, dict] | None:
    keys = manifest_keys(store.keys("manifests/"))
    return (keys[-1], store.read_json(keys[-1])) if keys else None


def complete(store: RunStore, carry: Carry, run: dict) -> bool:
    """Whether `run`'s dir is whole on the store: its `meta.json` (uploaded last) names the planned scans, and every reader
    file is there."""
    if not store.exists(f"{run['key']}/meta.json") or store.read_json(f"{run['key']}/meta.json").get("scans") != run["scans"]:
        return False
    return not carry.missing(store, [run])


@dataclass
class Published:
    key: str
    runs: list[dict]


def publish_revision(store: RunStore, carry: Carry, inputs: list[str], out: dict, *, attempts: int = 5, log: Callable[[str], None] = err) -> Published | None:
    """Publish the merged run `out` (in place of the runs keyed `inputs`) as a revision of the newest manifest, rebased
    onto whichever manifest is newest when it's written, and re-checked after: until the newest manifest lists `out`.
    None when the newest already did. Refuses (`RuntimeError`) a revision whose runs lack a reader file, or that the
    store's `refuse` turns down."""
    for _ in range(attempts):
        key, m = newest(store)
        runs = rebase(m["runs"], inputs, out)
        if runs is None:
            return None
        scan, _ = parse_manifest(key.removeprefix("manifests/"))
        revs = [p[1] for k in store.keys(f"manifests/{scan}") if (p := parse_manifest(k.removeprefix("manifests/"))) and p[0] == scan]
        name = f"manifests/{manifest_name(scan, max(revs) + 1)}"
        if missing := carry.missing(store, runs):
            raise RuntimeError(f"not publishing {name}: listed runs lack {missing}")
        if why := carry.refuse(store, m["runs"], runs):
            raise RuntimeError(f"not publishing {name}: {why}")
        doc = {**m, "runs": listed(runs), "rev": max(revs) + 1, "revises": key}
        if [s for r in doc["runs"] for s in r["scans"]] != [s for r in m["runs"] for s in r["scans"]]:
            raise RuntimeError(f"not publishing {name}: its runs' scans differ from {key}'s")
        try:
            store.create(name, json.dumps(doc, indent=1) + "\n")
        except FileExistsError:
            log(f"merge {out['key']}: {name} appeared meanwhile; rebasing")
            continue
        if newest(store)[0] == name:
            return Published(name, runs)
        log(f"merge {out['key']}: a newer manifest than {name} landed meanwhile; rebasing onto it")
    raise RuntimeError(f"merge {out['key']}: no revision stayed newest in {attempts} attempts")


def merge_pending(store: RunStore, carry: Carry, mount: Path, *, tmp: Path, owner: str | None = None, dry_run: bool = False,
                  max_merges: int | None = None, max_level: int | None = COMPACT_LEVEL, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                  status: MergeStatus | None = None, heartbeat_s: float | None = HEARTBEAT_S, log: Callable[[str], None] = err) -> dict:
    """Run the newest manifest's due carries (`plan_carries`), one merged run at a time (`carry.build`), each published as
    a revision (`publish_revision`) before the next is planned, until none is due (or `max_merges`). `mount`: the
    generation's dir on a mount of the data bucket (the inputs are read from it). Holds the merge lease throughout
    (returns `{"held": …}` without merging when another holder has it). A failure leaves the newest manifest as it was
    (every run it lists whole), and a rerun resumes: a merged dir already whole is reused. `max_level`: the compaction
    level, which no carry reaches (`plan_carries`; None: unbounded). `status`: where the lease is mirrored for /health
    (`StatusMirror`: written as each carry starts, refreshed every `heartbeat_s`, removed on a clean end, marked failed on
    a raise; best-effort throughout)."""
    owner = owner or f"{os.environ.get('BATCH_JOB_UID') or socket.gethostname()}:{os.getpid()}"
    cur = newest(store)
    if cur is None:
        return {"merged": [], "manifest": None}
    if dry_run:
        _, merges = plan_carries(cur[1]["runs"], carry.drilled(store, cur[1]["runs"]), max_level)
        return {"manifest": cur[0], "plan": [{"inputs": [r["key"] for r in ins], "output": out["key"], "level": out["level"]} for ins, out in merges]}
    if held := store.lease(owner, now(), LEASE_S):
        log(f"merge: the lease is held ({held}); not merging")
        return {"held": held, "merged": [], "manifest": cur[0]}
    done: list[dict] = []
    mirror = StatusMirror(status, owner, now, log, heartbeat_s)
    try:
        while max_merges is None or len(done) < max_merges:
            key, m = newest(store)
            drilled = carry.drilled(store, m["runs"])
            _, merges = plan_carries(m["runs"], drilled, max_level)
            if not merges:
                break
            mirror.write(key, merges, done)
            ins, out = merges[0]
            t0 = monotonic()
            if complete(store, carry, out):
                log(f"merge {out['key']}: already whole; publishing it")
                meta = store.read_json(f"{out['key']}/meta.json")
                doc = {"rows": meta.get("rows"), "bytes": meta.get("bytes"), "s": {}}
            else:
                outp = tmp / "merge" / out["key"]
                doc = carry.build([mount / r["key"] for r in ins], out, outp, drilled=all(r["key"] in drilled for r in ins), tmp=tmp, log=log)
                # a merged run is written once: an earlier attempt may have left only keys this one writes (overwritten here)
                ours = {f"{out['key']}/{f.relative_to(outp).as_posix()}" for f in outp.rglob("*") if f.is_file()}
                if stale := sorted(set(store.keys(f"{out['key']}/")) - ours):
                    raise RuntimeError(f"{out['key']}/ holds {len(stale)} objects this merge doesn't write (e.g. {stale[0]}): not merging into it")
                up = store.upload(outp, out["key"])
                log(f"merge {out['key']}: uploaded {len(up)} files, {sum(f['size'] for f in up) / 2**30:.2f} GiB")
                shutil.rmtree(outp)
            out = {**out, **{k: doc[k] for k in ("rows", "bytes") if doc.get(k) is not None}}
            pub = publish_revision(store, carry, [r["key"] for r in ins], out, log=log)
            done.append({"inputs": [r["key"] for r in ins], "output": out["key"], "level": out["level"], "scans": len(out["scans"]),
                         "rows": out.get("rows"), "manifest": pub.key if pub else None, "tiers_s": doc["s"], "s": round(monotonic() - t0, 1)})
            log(f"merge {out['key']} ({len(ins)} runs, {len(out['scans'])} scans): {pub.key if pub else 'already listed'} in {done[-1]['s']:.0f}s")
    except BaseException as e:
        mirror.failed(e)
        raise
    else:
        mirror.clear()
    finally:
        store.release(owner)
    return {"merged": done, "manifest": newest(store)[0]}


# ── Batch ──────────────────────────────────────────────────────────────────


def task_command(p: Profile, module: str, args: list[str], *, mount: bool = True) -> str:
    """A stage task's shell command: `python -m dt_cloud.<module> <args> [-m /gcs/<bucket>]`, from the image's code or
    (`p.src`) the given mounted dirs."""
    prep = "mkdir -p /stage/tmp /stage/out"
    py = "python3 -u -m"
    if p.src:
        prep += " /stage/src && cp -r " + " ".join(shlex.quote(s) for s in p.src) + " /stage/src/"
        py = "PYTHONPATH=/stage/src " + py
    m = ["-m", f"/gcs/{p.bucket}"] if mount else []
    return f"set -euo pipefail; {prep} && cd /stage && {py} dt_cloud.{module} {shlex.join([*args, *m])}"


def duckdb_args(machine: str) -> list[str]:
    """`-M <mem> -p <threads>` for a task's DuckDB sized to its machine: ¾ of its memory (7,700 MiB per vCPU), every vCPU."""
    vcpus = int(machine.rsplit("-", 1)[-1])
    return ["-M", f"{vcpus * 7700 * 3 // 4 // 1024}GB", "-p", str(vcpus)]


def job_spec(p: Profile, name: str, tasks: int, commands: list[str], *, stage: str, purpose: str, component: str, scratch: bool = True,
             r2: bool = False, machine: str | None = None, ssd_gb: int | None = None) -> dict:
    """A Batch job of `tasks` tasks (in parallel) running `commands` (one shell script, `&&`-chained) in the image, the
    data bucket (and `scratch`) mounted read-only under /gcs, a local SSD at /stage. `r2`: as the R2 account, with its
    credentials from Secret Manager. `purpose` labels the job; `component` is its cost label (`cost_labels`)."""
    machine = machine or p.machine
    vcpus = int(machine.rsplit("-", 1)[-1])
    buckets = [p.bucket, *([p.scratch] if scratch else [])]
    env = {"STATIC_NAMES_BUCKET": p.bucket, "STATIC_NAMES_SCRATCH": p.scratch}
    environment: dict = {"variables": env}
    if r2:
        env["R2_BUCKET"] = p.r2_bucket
        if "endpoint" not in p.r2_secrets:
            env["R2_ENDPOINT"] = p.r2_endpoint
        secrets = p.r2_env_secrets()
        environment["secretVariables"] = {k: f"projects/{p.project}/secrets/{v}/versions/latest" for k, v in sorted(secrets.items())}
    return label_batch_spec({
        "taskGroups": [{
            "taskCount": tasks,
            "parallelism": tasks,
            "taskSpec": {
                "runnables": [{"container": {
                    "imageUri": p.need("image"),
                    "entrypoint": "bash",
                    "commands": ["-c", " && ".join(f"( {c} )" for c in commands) if len(commands) > 1 else commands[0]],
                    "volumes": [*(f"/mnt/disks/gcs/{b}:/gcs/{b}:ro" for b in buckets), "/mnt/disks/stage:/stage:rw"],
                }}],
                "environment": environment,
                "computeResource": {"cpuMilli": vcpus * 1000, "memoryMib": vcpus * 7700},
                "maxRetryCount": 3 if p.spot else 0,
                "maxRunDuration": f"{MAX_RUN_S}s",
                "volumes": [
                    *({"gcs": {"remotePath": b}, "mountPath": f"/mnt/disks/gcs/{b}", "mountOptions": ["--implicit-dirs"]} for b in buckets),
                    {"deviceName": "stage", "mountPath": "/mnt/disks/stage"},
                ],
            },
        }],
        "allocationPolicy": {
            "instances": [{"policy": {
                "machineType": machine,
                "provisioningModel": "SPOT" if p.spot else "STANDARD",
                "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
                "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": str(ssd_gb or p.ssd_gb)}, "deviceName": "stage"}],
            }}],
            "serviceAccount": {"email": (p.r2_sa or p.sa) if r2 else p.sa},
            "location": {"allowedLocations": [f"regions/{p.region}"]},
        },
        "labels": {"purpose": purpose, "stage": stage, "gen": label(p.gen)},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }, component)


def label(v: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in v.lower())[:63]


def job_id(prefix: str, stage: str, scan_id: str, now: datetime | None = None) -> str:
    """`<prefix>-<stage>-<scan>-<hhmmss>`: Batch ids are lowercase letters, digits and hyphens."""
    t = (now or datetime.now(timezone.utc)).strftime("%H%M%S")
    return f"{prefix}-{stage}-{scan_id.lower().replace('t', '-')}-{t}"


class BatchRunner:
    """Submit a Batch job and wait for it (REST over ADC, `batch.submit_job` / `gcp.batch_job`)."""

    def __init__(self, p: Profile, log: Callable[[str], None], *, delay: float = 10, max_delay: float = 30):
        """Polls every `delay` s, doubling up to `max_delay`: each stage's end is noticed up to one poll late, so the cap
        stays low (at 120 s, gcs 10-10's static chain lost minutes between stages; the interval store's jobs run a minute
        or a few)."""
        self.p, self.log, self.delay, self.max_delay = p, log, delay, max_delay

    def __call__(self, name: str, spec: dict, wait: float | None = None) -> None:
        """Submit, then poll until the job ends; `wait`: stop polling after that many seconds (`StillRunning`; the job runs on)."""
        from .batch import submit_job
        from .gcp import batch_job

        submit_job(spec, name, region=self.p.region)
        self.log(f"submitted {name}")
        delay, t0 = self.delay, monotonic()
        while True:
            if wait is not None and monotonic() - t0 >= wait:
                raise StillRunning(f"Batch job {name}: still running after {wait:.0f}s")
            st = batch_job(name, project=self.p.project, region=self.p.region).get("status", {})
            state = st.get("state", "?")
            counts = " ".join(f"{k}={v}" for g in (st.get("taskGroups") or {}).values() for k, v in sorted(g.get("counts", {}).items()))
            self.log(f"{name} {state} {counts}")
            if state == "SUCCEEDED":
                return
            if state in ("FAILED", "DELETION_IN_PROGRESS", "CANCELLED"):
                raise RuntimeError(f"Batch job {name}: {state}")
            time.sleep(delay if wait is None else max(0.0, min(delay, wait - (monotonic() - t0))))
            delay = min(delay * 2, self.max_delay)


# ── Prune: the newest complete open-version state only ─────────────────────


def delete_objects(bucket, names: list[str], *, workers: int = 8) -> int:
    """Delete `names` from `bucket` (a `google.cloud.storage.Bucket`) over `workers` threads (one request each; ≤ the
    client's 10 pooled connections). One already gone is skipped; any other error raises. Returns the objects deleted."""
    from google.api_core.exceptions import NotFound

    def one(n: str) -> int:
        try:
            bucket.blob(n).delete()
        except NotFound:
            return 0
        return 1

    with ThreadPoolExecutor(workers) as ex:
        return sum(ex.map(one, names))


def prune_state(gcs, root: str, scan: str, plan: Callable[[list[tuple[str, int]], bool], dict], *, bucket: str, scratch: str,
                dry_run: bool = False, workers: int = 8) -> dict:
    """Delete every earlier scan's `<root>/state/<prev>/` in the scratch bucket (nowhere else), once `scan`'s state is
    complete and its manifest `manifests/<scan>.json` published: `plan(objects, published)` (the store's; it raises
    `StateIncomplete` otherwise) over the scratch bucket's `(name, size)` listing under `<root>/state/`, `workers`
    deletes at a time. Idempotent: a rerun finds nothing earlier. `gcs`: a `google.cloud.storage.Client`. Returns the
    plan, `deleted` = objects."""
    objects = [(b.name, int(b.size or 0)) for b in gcs.list_blobs(scratch, prefix=f"{root}/state/")]
    published = gcs.bucket(bucket).blob(f"{root}/manifests/{manifest_name(scan)}").exists()
    out = plan(objects, published)
    names = out.pop("names")
    if not dry_run and names:
        delete_objects(gcs.bucket(scratch), names, workers=workers)
    return {**out, "deleted": 0 if dry_run else len(names)}


# ── R2 ─────────────────────────────────────────────────────────────────────


def r2_objects(bucket: str, prefixes: list[str], keep: Callable[[str], bool] = lambda key: True) -> list:
    """The GCS objects under `prefixes` whose key `keep` takes (`publish.Obj`s)."""
    from . import publish as pub

    return [o for o in pub.list_source(bucket, prefixes) if keep(o.key)]


def r2_missing(objs: list, *, workers: int = 16) -> list[str]:
    """The keys of `objs` R2 lacks, or holds with another size or md5."""
    from . import publish as pub

    s3, r2 = pub.r2_client(), pub.r2_bucket()
    with ThreadPoolExecutor(workers) as ex:
        return [o.key for o, do in ex.map(lambda o: (o, pub.should_copy(o, pub.head_dest(s3, r2, o.key))), objs) if do]


def r2_copy(bucket: str, objs: list, *, workers: int = 8, dry_run: bool = False, log: Callable[[str], None] = err) -> dict:
    """Copy `objs` GCS → R2 under the same keys (`publish.copy_one`'s streaming copy, the GCS md5 stamped as metadata),
    skipping objects already there with the same size and md5. Returns `{objects, copied, bytes, s}` (`keys` too on a
    dry run: what would be copied)."""
    from . import publish as pub

    s3, r2 = pub.r2_client(), pub.r2_bucket()
    with ThreadPoolExecutor(workers) as ex:
        todo = [o for o, do in ex.map(lambda o: (o, pub.should_copy(o, pub.head_dest(s3, r2, o.key))), objs) if do]
    total = sum(o.size for o in todo)
    if dry_run:
        return {"objects": len(objs), "copied": 0, "bytes": total, "s": 0, "keys": [o.key for o in todo]}
    t0, done = monotonic(), 0

    def one(o):
        pub.copy_one(bucket, s3, r2, o)
        return o

    with ThreadPoolExecutor(workers) as ex:
        for o in ex.map(one, todo):
            done += o.size
            log(f"  → {o.key} ({o.size:,} B; {done / total:.1%}, {done / max(monotonic() - t0, 1e-9) / 1e6:.0f} MB/s)")
    return {"objects": len(objs), "copied": len(todo), "bytes": total, "s": round(monotonic() - t0, 1)}


def r2_publish(bucket: str, root: str, manifest: str, *, served: tuple[str, ...] = ("",), markers: tuple[str, ...] = (), workers: int = 8,
               log: Callable[[str], None] = err) -> dict:
    """One manifest to R2: the served files (under the run-relative prefixes `served`) of every run
    `<root>/manifests/<manifest>.json` lists, each run's liveness `markers` (run-relative keys, in order) after all the
    rest; then a check that every one is on R2 (size, md5); then the manifest, last. Raises (`SystemExit`, the manifest
    not copied) when the check fails."""
    from .static_names import read_json

    key = f"{root}/manifests/{manifest}.json"
    runs = read_json(f"gs://{bucket}/{key}")["runs"]
    body, tail = [], []
    for r in runs:
        run = f"{root}/{r['key']}/"
        objs_ = r2_objects(bucket, [run + s for s in served])
        body += [o for o in objs_ if o.key.removeprefix(run) not in markers]
        tail += sorted((o for o in objs_ if o.key.removeprefix(run) in markers), key=lambda o, run=run: markers.index(o.key.removeprefix(run)))
    t0 = monotonic()
    doc = r2_copy(bucket, body, workers=workers, log=log)
    for o in tail:
        r2_copy(bucket, [o], workers=1, log=log)
    objs = [*body, *tail]
    if bad := r2_missing(objs, workers=workers):
        raise SystemExit(f"r2 {manifest}: {len(bad)} of {len(objs)} served files not on R2 after the copy (e.g. {bad[0]}): manifest not copied")
    r2_copy(bucket, r2_objects(bucket, [key]), workers=1, log=log)
    return {"manifest": manifest, "runs": [r["key"] for r in runs], "objects": len(objs), "copied": doc["copied"] + len(tail),
            "bytes": doc["bytes"] + sum(o.size for o in tail), "s": round(monotonic() - t0, 1)}


# ── The chain ──────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class Runner:
    """A store's append chain over injectable effects (tests pass fakes): `exists(key)` / `count(prefix, suffix)` /
    `read_json(key)` / `list_keys(prefix)` on the data bucket (keys relative to it), `published(layouts, start)` the scan
    ids under the generation's layouts, `run_job(name, spec[, wait])`, `prepare(scan_id)` and `prune(scan_id)` (local
    stages), `log`. `merge`: run the merge stage (the deferred carries) after the scans; `merge_wait`: how long it waits
    on its Batch job (None: to its end; 0: submit and go).

    A store subclasses it: `store_prefix` / `job_prefix` / `purpose`, its per-scan stages (`one`), what a rerun of an
    appended scan redoes (`rerun`), its jobs' commands and specs (`command`, `spec`), its merge job (`merge_job`), its R2
    job (`r2`) and its drilled runs (`drilled`)."""
    cfg: Any
    exists: Callable[[str], bool]
    count: Callable[[str, str], int]
    read_json: Callable[[str], dict]
    published: Callable[[Any, str], list[str]]
    run_job: Callable[..., None]
    prepare: Callable[[str], None]
    prune: Callable[[str], None]
    list_keys: Callable[[str], list[str]] = lambda prefix: []
    log: Callable[[str], None] = err
    dry_run: bool = False
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    merge: bool = True
    merge_wait: float | None = 0
    timings: dict = field(default_factory=dict)

    store_prefix: ClassVar[str]
    job_prefix: ClassVar[str]

    @property
    def p(self) -> Profile:
        return self.cfg

    @property
    def root(self) -> str:
        return f"{self.store_prefix}/{self.p.gen}"

    def manifests(self) -> list[str]:
        """The manifests' keys, oldest first: by scan, then revision (`manifest_keys`)."""
        return [f"{self.root}/{k}" for k in manifest_keys([k.removeprefix(f"{self.root}/") for k in self.list_keys(f"{self.root}/manifests/")])]

    def base(self) -> dict:
        """The generation's `scans.json`."""
        return self.read_json(f"{self.root}/scans.json")

    def layouts(self, base: dict):
        """Where the generation's scans are published (`published`' first argument)."""
        return self.p.layouts

    def have(self) -> tuple[list[str], Any]:
        """The generation's scans (base + the newest manifest's runs), and its layouts."""
        base = self.base()
        keys = self.manifests()
        runs = self.read_json(keys[-1])["runs"] if keys else []
        return scans_of(base, runs), self.layouts(base)

    def run(self, scan_id: str, catch_up: bool = False) -> list[str]:
        """Append `scan_id` (and with `catch_up` every earlier pending scan, in order), then the merge stage. Returns the
        scans appended."""
        have, layouts = self.have()
        todo = pending_scans(have, self.published(layouts, have[-1]), scan_id, catch_up)
        # Every listed run's missing tiers first (a scan's own build reads its earlier runs').
        built = self.backfill()
        if not todo:
            self.log(f"{scan_id}: already appended; {self.rerun_note()}")
            if scan_id == have[-1]:
                self.rerun(scan_id)
            elif built:
                self.r2_newest()
            self.carries()
            return []
        if len(todo) > 1:
            self.log(f"catching up {len(todo)} scans: {', '.join(todo)}")
        for s in todo:
            self.one(s)
        self.carries()
        return todo

    def one(self, d: str) -> None:
        raise NotImplementedError

    def rerun_note(self) -> str:
        return "the R2 copy and prune only"

    def rerun(self, d: str) -> None:
        """An appended scan, run again: its R2 copy and prune."""
        self.r2(d)
        self.stage(f"{d} prune", lambda: self.prune(d))

    def stage(self, name: str, fn: Callable[[], None]) -> None:
        if self.dry_run:
            self.log(f"{name}: would run")
            return
        t = monotonic()
        self.log(f"{name}: start")
        fn()
        self.timings[name] = round(monotonic() - t, 1)
        self.log(f"{name}: done in {monotonic() - t:.0f}s")

    def concurrently(self, d: str, jobs: list[tuple[str, Callable[[], None]]]) -> None:
        """`jobs` (`(label, fn)`, each a stage's job and its post-check) as one stage, at once (a lone one as itself). Each
        runs to its end (a failure doesn't stop the other, whose output is kept, so a rerun resumes only the failed one);
        then the failure is raised, or with several a `RuntimeError` naming each."""
        if not jobs:
            return
        if len(jobs) == 1:
            (label_, fn), = jobs
            self.stage(f"{d} {label_}", fn)
            return

        def timed(label_: str, fn: Callable[[], None]) -> None:
            t = monotonic()
            try:
                fn()
            except Exception as e:
                self.log(f"{d} {label_}: failed after {monotonic() - t:.0f}s: {e}")
                raise
            self.log(f"{d} {label_}: done in {monotonic() - t:.0f}s")

        def all_() -> None:
            with ThreadPoolExecutor(len(jobs)) as ex:
                futures = [(label_, ex.submit(timed, label_, fn)) for label_, fn in jobs]
            errs = [(label_, e) for label_, f in futures if (e := f.exception()) is not None]
            if len(errs) == 1:
                raise errs[0][1]
            if errs:
                raise RuntimeError("; ".join(f"{label_}: {e}" for label_, e in errs)) from errs[0][1]
        self.stage(f"{d} {' ∥ '.join(label_ for label_, _ in jobs)}", all_)

    def command(self, module: str, args: list[str], *, mount: bool = True) -> str:
        return task_command(self.p, module, args, mount=mount)

    def spec(self, name: str, tasks: int, commands: list[str], *, stage: str, **kw) -> dict:
        raise NotImplementedError

    def job_name(self, stage: str, scan_id: str) -> str:
        return job_id(self.job_prefix, stage, scan_id, self.now())

    def job(self, stage: str, scan_id: str, tasks: int, module: str, args: list[str], **kw) -> tuple[str, dict]:
        name = self.job_name(stage, scan_id)
        cmd = self.command(module, args, mount=kw.pop("mount", True))
        return name, self.spec(name, tasks, [cmd], stage=stage, **kw)

    def drilled(self, runs: list[dict]) -> set[str]:
        """The runs carrying a drill (merged only with each other)."""
        return set()

    def backfill(self) -> bool:
        """Give every run the newest manifest lists the tiers it lacks (a store with optional per-run tiers: the static
        index's drill and anchors), before any scan's stages. Returns whether any step ran (its runs then need the R2
        copy: `run`'s). None here."""
        return False

    def r2_newest(self) -> None:
        """The R2 job for the newest manifest (a scan's, or a revision): its runs, then it."""
        keys = self.manifests()
        if keys:
            stem = keys[-1].rsplit("/", 1)[-1].removesuffix(".json")
            self.r2(parse_manifest(f"{stem}.json")[0], stem)

    def merge_job(self, scan: str) -> tuple[str, dict]:
        """The merge job (one task: the due carries, each a revision), with the R2 credentials when `merge_r2`."""
        raise NotImplementedError

    def merge_r2(self) -> bool:
        """Whether the merge job gets the R2 copy's credentials, for its status record on R2 (`r2_merge_status`): when the
        R2 account is the stages' own (a job runs as one account, and the merge's writes the data bucket). Otherwise the
        merge runs without, and /health doesn't see it."""
        return self.p.r2_sa in (None, "", self.p.sa)

    def r2(self, d: str, manifest: str | None = None) -> None:
        """The R2 job for `manifests/<manifest>.json` (default `d`'s own): its runs, checked there, then it, last."""
        raise NotImplementedError

    def carries(self, fatal: bool = False) -> None:
        """The merge stage, once after the scans: the newest manifest's due carries (`plan_carries`), when any, as one Batch
        job (`merge_pending`: each merged run, then a revision of the newest manifest), waited on up to `merge_wait` s; then,
        when the newest manifest is a revision, the R2 job for it (a merge that outlived the wait reaches R2 here on a later
        run, or with the next scan's manifest, which lists its run). Non-fatal: a failure or timeout is logged and the store
        stays as it was, servable (the next run plans again; a merge resumes). `fatal`: raise a failure (`RuntimeError`)
        instead."""
        if not self.merge:
            return
        try:
            keys = self.manifests()
            if not keys:
                return
            m = self.read_json(keys[-1])
            top = self.p.compact_level
            after, merges = plan_carries(m["runs"], self.drilled(m["runs"]), top)
            compact = top is not None and any(a["level"] == b["level"] == top - 1 for a, b in zip(after, after[1:]))
            if compact:
                self.log(f"merge: level {top} is due: compact into a new base generation (carries stop below it)")
            if merges:
                desc = "; ".join(f"{len(ins)} runs → {out['key']} (level {out['level']})" for ins, out in merges)
                name, spec = self.merge_job(m["date"])
                # its status record's holder (`StatusMirror`)
                spec["taskGroups"][0]["taskSpec"]["environment"]["variables"][JOB_NAME_VAR] = name
                try:
                    self.stage(f"merge: {desc}", lambda: self.run_job(name, spec, wait=self.merge_wait))
                except StillRunning as e:
                    self.log(f"merge: {e}; it publishes its revision on GCS when done, and R2 gets it with a later run")
                    return
                if self.dry_run:
                    return
                keys = self.manifests()
            elif not compact:
                self.log(f"merge: nothing due ({' '.join(f'L{r["level"]}' for r in m['runs']) or 'no runs'})")
            scan, rev = parse_manifest(keys[-1].rsplit("/", 1)[-1])
            if rev:
                self.r2(scan, keys[-1].rsplit("/", 1)[-1].removesuffix(".json"))
        except Exception as e:
            if fatal:
                raise RuntimeError(str(e)) from e
            self.log(f"merge: failed, not fatal (every listed run is whole; the next run plans again): {e}")


def exit_on(label_: str, fn: Callable[[], Any], log: Callable[[str], None]) -> Any:
    """`fn()`, a CLI's run: `NotNext` exits `NOT_NEXT`, a failed stage (`RuntimeError`) exits 1, each logged as
    `<label>: <why>`."""
    try:
        return fn()
    except NotNext as e:
        log(f"{label_}: {e}")
        sys.exit(NOT_NEXT)
    except RuntimeError as e:
        log(f"{label_}: {e}")
        sys.exit(1)
