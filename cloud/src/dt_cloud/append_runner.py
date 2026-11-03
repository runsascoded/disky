"""One append runner for the run stores (specs/done/index-landscape.md §5, recommendation 1): the static name index's runs
(`static_runner`, `static_merge`) and the interval store's (`interval_append`) share everything here; each store plugs
in its stage list (`Runner.one`), its merged-run build (`Carry.build`) and its reader files (`Carry.missing`).

A run store is a base generation `<prefix>/<gen>/` plus one small **run** per newer scan (`deltas/<first>[_<last>]/`),
listed by immutable manifests:

- **Order.** Scans are appended strictly in scan-id order (`pending_scans`): exit `NOT_NEXT` (3) when SCAN_ID is not
  published yet or an earlier published scan is pending; `-c` catches up every pending scan in order.
- **One coordinator** per store (`Runner.coordination`, `coordinator_lease`; specs/done/append-coordinator-lease.md): a lease
  in the scratch bucket, held for the whole run and renewed by a heartbeat; another `run` meanwhile exits `BUSY` (4).
- **A compaction holds off appends** to its generation (`compaction_lease`, beside the coordinator lease): written for
  the `compact` job when it's submitted, adopted and renewed by the job until it ends, whoever submitted it; a `run`
  meanwhile exits `BUSY`, and once the successor is written it follows there.
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
  as it was and resumes (a whole merged dir is reused). The lease's holder mirrors it to R2 beside
  the store (`<prefix>/<gen>/merging.json`, `MergeStatus`; best-effort, never failing the merge) so `/health` can show
  a merge in progress, or stuck.
- **R2**: each manifest's runs (their liveness markers last), checked there, then the manifest, last (`r2_publish`).
- **Prune**: the scratch bucket's earlier open-version states, once the scan's is complete and published (`prune_state`);
  and on every store a manifest is published to (GCS, R2, a local or URL store), after the publish, the runs a carry
  folded (`plan_prune`, `prune_folded`): those listed by no manifest but the newest and the ones superseded less than
  `GRACE_S` (1 h) ago, each inside one of the newest's runs; a store's `retain` keeps what later appends read of a folded run.

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
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Protocol

from click import ParamType

from . import heartbeat as hb
from .cost_labels import label_batch_spec
from .maint_runs import Recorder
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


POLICY_FORMS = "`L` / `level:L` (an integer ≥ 1), `every:<N>d` / `every:<N>s` (days, scans), `geometric:<r>` (r > 0) or `none`"


class PolicyType(ParamType):
    """`-C`: a compaction policy (`parse_policy`): a bare level (`5`), `level:L`, `every:<N>d|s`, `geometric:<r>`, `none`."""
    name = "policy"

    def convert(self, value, param, ctx):
        if value is None or isinstance(value, Policy):
            return value
        try:
            return parse_policy("-C", value)
        except SystemExit as e:
            self.fail(str(e), param, ctx)


POLICY_HELP = (f"Compaction policy: {POLICY_FORMS} (specs/static-compaction.md); a bare level L: carries stop below it and "
               f"the base and every run fold into a new generation once two runs reach L − 1 (every 2^L scans); "
               f"`every` / `geometric`: carries unbounded below the trigger; `none`: never (default {COMPACT_LEVEL})")


def err(*a, **kw) -> None:
    print(*a, file=sys.stderr, flush=True, **kw)


#: Exit status: the scan is not published yet, or is not the next one to append.
NOT_NEXT = 3
#: Exit status: another coordinator holds the store's coordinator lease (`Busy`); it appends this scan too.
BUSY = 4
#: Exit status: the newest manifest lists more unmerged runs than the binary counter leaves (`runs lag`).
UNMERGED = 5
#: Revision suffix width: `<id>.m001.json` … `<id>.m999.json` sort in revision order.
REV_DIGITS = 3
MANIFEST = re.compile(rf"(?P<scan>[^/]+?)(?:\.m(?P<rev>\d{{{REV_DIGITS}}}))?\.json")
#: The merge lease's lifetime: a Batch task's `maxRunDuration` (`job_spec`), so a crashed holder's lease never blocks the
#: next merge for longer than its job could have run.
LEASE_S = 4 * 3600
#: A Batch task's longest run (`job_spec`).
MAX_RUN_S = 4 * 3600
#: Stages that fold a whole generation on one VM (`compact`): they run longer than `MAX_RUN_S` (gcs's first interval-store
#: compaction, 2026-10-21: a 2 h 10 min fold, then ~35 min per served sort in series), and always STANDARD, since a
#: reclaimed spot task restarts from zero (the static one was reclaimed ~3 h in). No merge lease is held through them.
#: `anchors` too: one backlogged scan's anchored-search build ran past `MAX_RUN_S` twice on cw (2026-10-22, n2-highmem-16,
#: with its probe joins bounded), each retry from zero.
LONG_STAGES = ("compact", "anchors")
LONG_RUN_S = 16 * 3600
#: A merge job whose carries reach this level runs as a long stage (STANDARD, `LONG_RUN_S`), and its lease lives as long:
#: a carry's inputs double per level (gcs's level-4 carry, 16 scans' runs: 183 min on a spot n2-highmem-16, 2026-10-16),
#: so level 5's ≈ 6 h outgrows `MAX_RUN_S`, and a reclaimed spot task restarts it from zero.
LONG_MERGE_LEVEL = 5
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


class Busy(Exception):
    """Another coordinator holds the store's coordinator lease: exit 4."""


class LeaseLost(RuntimeError):
    """The coordinator lease was taken over (or couldn't be renewed) mid-run: the run stops at the next stage."""


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


#: A scan's run listed before its publish: `pending/<scan>.json`, a manifest's `runs` through it, written by `prepare`. A
#: pipelined backlog's later scans build over it while the earlier ones are still in flight; `publish` removes it
#: (specs/static-backlog-pipeline.md).
PENDING = "pending/"


def prior_key(keys: Collection[str], before: str) -> str | None:
    """The listing of the runs before scan `before` among generation-relative `keys`: the newest pending listing of a scan
    after the newest manifest before `before` (`latest_key`) and before `before`, else that manifest; None without
    either."""
    m = latest_key(keys, before)
    floor = parse_manifest(m.removeprefix("manifests/"))[0] if m else ""
    pend = sorted(k for k in keys if k.startswith(PENDING) and SCAN_ID.fullmatch(s := k.removeprefix(PENDING).removesuffix(".json"))
                  and floor < s < before)
    return pend[-1] if pend else m


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


# ── The compaction policy (both stores) ────────────────────────────────────


#: A policy's rules (`Policy.rule`).
RULES = ("level", "every", "geometric", "none")


@dataclass(frozen=True)
class Policy:
    """When a generation's base and runs fold into a new base generation (specs/static-compaction.md, "Compaction policy"),
    evaluated by `compaction_why` for both stores:

    - `level` L: once the runs, carried, hold two runs at level L − 1 (every 2^L scans past the base); carries stop below
      L (`carry_level`). A profile's `compact_level` is shorthand for it.
    - `every` N `d` / `s`: once the newest scan is at least N days after the base's newest (`d`), or N scans past the base
      (`s`). Carries are unbounded below the trigger.
    - `geometric` r: once the scans past the base number at least r × the base's own (r = 1: at 2, 4, 8, … × the first
      base's). Each compaction rewrites the whole base, so this keeps its cost per scan constant. Carries unbounded.
    - `none`: never; carries unbounded."""
    rule: str
    level: int | None = None
    every: int | None = None
    unit: str | None = None
    ratio: float | None = None

    def __post_init__(self):
        ok = {"level": isinstance(self.level, int) and not isinstance(self.level, bool) and self.level >= 1,
              "every": isinstance(self.every, int) and not isinstance(self.every, bool) and self.every >= 1 and self.unit in ("d", "s"),
              "geometric": isinstance(self.ratio, (int, float)) and not isinstance(self.ratio, bool) and self.ratio > 0,
              "none": True}
        if self.rule not in ok or not ok[self.rule]:
            raise ValueError(f"not a compaction policy: {self!r}")

    def __str__(self) -> str:
        if self.rule == "level":
            return f"level:{self.level}"
        if self.rule == "every":
            return f"every:{self.every}{self.unit}"
        if self.rule == "geometric":
            return f"geometric:{self.ratio:g}"
        return "none"

    @property
    def carry_level(self) -> int | None:
        """The level no carry reaches (`plan_carries`' `max_level`): L under `level`, else None (unbounded)."""
        return self.level if self.rule == "level" else None

    def to_json(self) -> dict | str:
        """As a profile's `compact` field: `{"level": L}`, `{"every": "7d"}`, `{"geometric": r}`, or `"none"`."""
        if self.rule == "level":
            return {"level": self.level}
        if self.rule == "every":
            return {"every": f"{self.every}{self.unit}"}
        if self.rule == "geometric":
            return {"geometric": self.ratio}
        return "none"


NEVER = Policy("none")


def level_policy(level: int | None) -> Policy:
    """A compaction level (`compact_level`, `-L`) as a policy: `level` L, or `none` for None (unbounded)."""
    return NEVER if level is None else Policy("level", level=level)


def parse_policy(var: str, raw: Any) -> Policy:
    """A profile's `compact` value (JSON: `{"level": 5}`, `{"every": "7d"}`, `{"geometric": 1}`, `"none"`, or a bare
    level), or an env var's / option's text (`5`, `level:5`, `every:7d`, `every:30s`, `geometric:1.5`, `none`), as a
    `Policy`; `var` names the source in the error (`SystemExit`)."""
    bad = SystemExit(f"{var}: {raw!r} is not a compaction policy ({POLICY_FORMS})")
    if isinstance(raw, Policy):
        return raw
    if isinstance(raw, dict):
        if len(raw) != 1:
            raise bad
        (k, v), = raw.items()
        raw = f"{k}:{v}"
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise bad
    text = str(raw).strip().lower()
    rule, _, arg = text.partition(":") if ":" in text else (("level", "", text) if text != "none" else ("none", "", ""))
    try:
        if rule == "none" and not arg:
            return NEVER
        if rule == "level":
            return Policy("level", level=int(arg))
        if rule == "every" and arg[-1:] in ("d", "s"):
            return Policy("every", every=int(arg[:-1]), unit=arg[-1])
        if rule == "geometric":
            r = float(arg)
            return Policy("geometric", ratio=int(r) if r.is_integer() else r)
    except ValueError:
        pass
    raise bad


def as_policy(p: Policy | int | None) -> Policy:
    """`p`, or a compaction level (None: unbounded) as its `level` policy."""
    return p if isinstance(p, Policy) else level_policy(p)


def compaction_why(runs: list[dict], policy: Policy | int | None, drilled: Collection[str] = frozenset(), *,
                   base: Collection[str] = ()) -> str | None:
    """Why the generation whose newest manifest lists `runs` (oldest first) is due a compaction under `policy` (a
    `Policy`, or a compaction level: None unbounded), or None when it isn't; the reason names the rule that fired.
    `base`: the base generation's scan ids, oldest first (`every …d` and `geometric` read them). Never without runs."""
    policy = as_policy(policy)
    if not runs or policy.rule == "none":
        return None
    if policy.rule == "level":
        top = policy.level
        assert top is not None
        after, _ = plan_carries(runs, drilled, top)
        pair = next(((a, b) for a, b in zip(after, after[1:]) if a["level"] == b["level"] == top - 1), None)
        return None if pair is None else f"level {top}: two runs at level {top - 1} ({pair[0]['key']}, {pair[1]['key']})"
    past = sum(len(r["scans"]) for r in runs)
    if policy.rule == "every" and policy.unit == "s":
        return f"every {policy.every}s: {past} scans past the base" if past >= (policy.every or 0) else None
    base = list(base)
    if not base:
        raise ValueError(f"compaction policy {policy}: the base's scans are needed")
    if policy.rule == "every":
        from .scan_id import scan_time

        days = (scan_time(runs[-1]["last"]) - scan_time(base[-1])).total_seconds() / 86400
        return (f"every {policy.every}d: the newest scan {runs[-1]['last']} is {days:g} days after the base's newest {base[-1]}"
                if days >= (policy.every or 0) else None)
    ratio = policy.ratio or 0
    return (f"geometric {ratio:g}: {past} scans past the base ≥ {ratio:g} × its {len(base)}"
            if past >= ratio * len(base) else None)


def compaction_due(runs: list[dict], policy: Policy | int | None, drilled: Collection[str] = frozenset(), *, base: Collection[str] = ()) -> bool:
    """Whether `runs` (a manifest's, oldest first) are due to fold with the base into a new base generation under
    `policy` (`compaction_why`). Under a level L (an int, or `Policy` `level`): once their due carries are made
    (`plan_carries`), two runs at level L − 1 — the carry between them would reach the compaction level (every 2^L scans
    past the base). Never when unbounded (None, `none`)."""
    return compaction_why(runs, policy, drilled, base=base) is not None


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


def rm_local(root: Path, keys: list[str]) -> None:
    """Files `keys` (relative to `root`) removed (one already gone is skipped), then each dir they emptied, up to `root`."""
    root = Path(root)
    dirs = set()
    for k in keys:
        p = root / k
        p.unlink(missing_ok=True)
        dirs.update(p.parents[:len(Path(k).parents) - 1])
    for d in sorted(dirs, key=lambda d: -len(d.parts)):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def lease_stale(held: dict, now: datetime, ttl_s: int) -> bool:
    """Whether `held` (a lease record) is past its lifetime: its own `ttl_s` (a long merge's), else `ttl_s`."""
    return (now - datetime.fromisoformat(held["at"])).total_seconds() > held.get("ttl_s", ttl_s)


def merge_ttl(merges: list[tuple[list[dict], dict]], long_rows: int | None = None) -> int:
    """A merge job's lease lifetime: `LONG_RUN_S` when it runs long (`merge_is_long`), else `LEASE_S`."""
    return LONG_RUN_S if merge_is_long(merges, long_rows) else LEASE_S


def merge_is_long(merges: list[tuple[list[dict], dict]], long_rows: int | None = None) -> bool:
    """Whether planned carries (`plan_carries`' merges) run as a long stage: one reaches `LONG_MERGE_LEVEL`, or (`long_rows`:
    the store's bound, `Carry.long_rows`) their inputs hold at least `long_rows` rows (the job runs every carry in turn)."""
    return any(out["level"] >= LONG_MERGE_LEVEL for _, out in merges) or (
        long_rows is not None and sum(r.get("rows") or 0 for ins, _ in merges for r in ins) >= long_rows)


# ── The coordinator lease: one `Runner.run` per store at a time ───────────

#: The coordinator lease's name under the store's prefix in the scratch bucket (`<prefix>/coordinator.lease.json`): per
#: store, not per generation, so a run that follows a compaction into a new generation keeps holding it.
COORD_LEASE = "coordinator.lease.json"
#: How long a coordinator lease lives without a renewal: three missed heartbeats, so a crashed coordinator blocks the next
#: one for at most ~15 min (the holder renews it every `HEARTBEAT_S`).
COORD_TTL_S = 3 * HEARTBEAT_S


class LeaseFile(Protocol):
    """One lease record, written only conditionally: `token` is the version a read saw (a GCS generation)."""
    def create(self, text: str) -> bool:
        """Write `text` if there is no record; False when there is."""
    def read(self) -> tuple[dict, Any] | None:
        """The record and its token, or None when there is none."""
    def replace(self, text: str, token: Any) -> Any | None:
        """Overwrite the record if it is still at `token`: the new token, or None when it changed."""
    def delete(self, token: Any) -> bool:
        """Delete the record if it is still at `token`."""


class LocalLeaseFile:
    """`LeaseFile` on a local file (tests, local runs): the token is the record's text; one process's threads serialize
    on a lock."""

    def __init__(self, path: Path):
        self.path, self.lock = Path(path), threading.Lock()

    def create(self, text: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.path, "x") as fh:
                fh.write(text)
            return True
        except FileExistsError:
            return False

    def read(self) -> tuple[dict, Any] | None:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        return json.loads(text), text

    def replace(self, text: str, token: Any) -> Any | None:
        with self.lock:
            if (cur := self.read()) is None or cur[1] != token:
                return None
            self.path.write_text(text)
            return text

    def delete(self, token: Any) -> bool:
        with self.lock:
            if (cur := self.read()) is None or cur[1] != token:
                return False
            self.path.unlink()
            return True


class GcsLeaseFile:
    """`LeaseFile` on a GCS object, by generation preconditions."""

    def __init__(self, bucket: str, key: str, client=None):
        from google.cloud import storage

        self.blob = (client or storage.Client()).bucket(bucket).blob(key)

    def create(self, text: str) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        try:
            self.blob.upload_from_string(text, if_generation_match=0)
            return True
        except PreconditionFailed:
            return False

    def read(self) -> tuple[dict, Any] | None:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        for _ in range(3):
            try:
                self.blob.reload()
                gen = self.blob.generation
                return json.loads(self.blob.download_as_bytes(if_generation_match=gen)), gen
            except NotFound:
                return None
            except PreconditionFailed:
                continue
        raise RuntimeError(f"{self.blob.name}: the lease record kept changing while read")

    def replace(self, text: str, token: Any) -> Any | None:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        try:
            self.blob.upload_from_string(text, if_generation_match=token)
            return self.blob.generation
        except (NotFound, PreconditionFailed):
            return None

    def delete(self, token: Any) -> bool:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        try:
            self.blob.delete(if_generation_match=token)
            return True
        except (NotFound, PreconditionFailed):
            return False


class CoordinatorLease:
    """The coordinator lease on `file`, for one run (`owner`, a fresh uuid): `take` it (or raise `Busy`), `renew` it
    (the heartbeat; a renewal that finds it changed marks it lost), `check` (raises `LeaseLost` once lost), `release`.

    `extra`: more fields in the record (the compaction lease's `gen`). `adopt`: `take` a record whose `holder` is this
    one's, live or not (the compaction lease its submitter wrote for the job, `Runner.compact_stage`). `tolerate_errors`:
    a renewal that raises is retried at the next beat instead of losing the lease (its TTL spans three beats)."""

    def __init__(self, file: LeaseFile, *, holder: str, now: Callable[[], datetime], ttl_s: int = COORD_TTL_S, extra: dict | None = None,
                 adopt: bool = False, tolerate_errors: bool = False, what: str = "coordinator lease", log: Callable[[str], None] = err):
        from uuid import uuid4

        self.file, self.holder, self.now, self.ttl_s = file, holder, now, ttl_s
        self.extra, self.adopt, self.tolerate_errors, self.what, self.log = extra or {}, adopt, tolerate_errors, what, log
        self.owner, self.token, self.lost = uuid4().hex, None, None

    def record(self) -> str:
        return json.dumps({"owner": self.owner, "holder": self.holder, "at": self.now().isoformat(), "ttl_s": self.ttl_s, **self.extra}) + "\n"

    def take(self) -> None:
        for _ in range(3):
            if self.file.create(self.record()):
                self.token = self.file.read()[1]
                return
            if (cur := self.file.read()) is None:
                continue
            held, token = cur
            if self.adopt and held.get("holder") == self.holder:
                if (new := self.file.replace(self.record(), token)) is not None:
                    self.token = new
                    return
                continue
            if not lease_stale(held, self.now(), self.ttl_s):
                raise Busy(f"the {self.what} is held by {held.get('holder', held['owner'])} (since {held['at']})")
            self.file.delete(token)
        raise Busy(f"the {self.what} changed hands while being taken")

    def renew(self) -> None:
        if self.lost is not None:
            return
        try:
            token = self.file.replace(self.record(), self.token)
        except Exception as e:
            if self.tolerate_errors:
                self.log(f"{self.what}: renewing it failed (retried at the next beat): {type(e).__name__}: {e}")
                return
            self.lost = f"renewing it failed: {e}"
            return
        if token is None:
            cur = self.file.read()
            self.lost = f"it is now {cur[0].get('holder', cur[0]['owner'])}'s" if cur else "it was deleted"
        else:
            self.token = token

    def check(self) -> None:
        if self.lost is not None:
            raise LeaseLost(f"lost the coordinator lease: {self.lost}")

    def release(self) -> None:
        if self.lost is None and self.token is not None:
            self.file.delete(self.token)


@contextmanager
def coordinator_lease(file: LeaseFile, *, holder: str | None = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                      ttl_s: int = COORD_TTL_S, heartbeat_s: float = HEARTBEAT_S) -> Iterator[Callable[[], None]]:
    """Hold the coordinator lease on `file` (`holder`: `$DT_JOB_NAME`, else host:pid), renewed every `heartbeat_s` on a
    thread; yields its `check`. Raises `Busy` when another holds it. Released on the way out, unless it was lost."""
    lease = CoordinatorLease(file, holder=holder or default_holder(), now=now, ttl_s=ttl_s)
    lease.take()
    with heartbeat(lease, heartbeat_s):
        yield lease.check


def default_holder() -> str:
    """A lease's `holder`: `$DT_JOB_NAME` (a Batch job's name), else host:pid."""
    return os.environ.get(JOB_NAME_VAR) or f"{socket.gethostname()}:{os.getpid()}"


@contextmanager
def heartbeat(lease: CoordinatorLease, heartbeat_s: float) -> Iterator[None]:
    """`lease` (taken) renewed every `heartbeat_s` on a thread; released on the way out, unless it was lost."""
    stop = threading.Event()

    def beat():
        while not stop.wait(heartbeat_s):
            lease.renew()

    t = threading.Thread(target=beat, daemon=True, name=lease.what.replace(" ", "-"))
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=30)
        lease.release()


def coordinator_prefix(store, prefix: str, gen: str) -> str:
    """Where appends to `gen` take their coordinator lease: the store's `prefix`, or while `gen` is a candidate nothing
    points at yet (`candidate.json`, its source without `successor.json`) its own `<prefix>/<gen>`: a candidate's
    catch-up never holds off the current generation's appends. Once promoted, the store's again."""
    marker = f"{prefix}/{gen}/{CANDIDATE}"
    if not store.exists(marker):
        return prefix
    return prefix if store.exists(f"{prefix}/{store.read_json(marker)['from']}/{SUCCESSOR}") else f"{prefix}/{gen}"


def gcs_coordinator_lease(scratch: str, prefix: str, client=None) -> AbstractContextManager[Callable[[], None]]:
    """`coordinator_lease` at `gs://<scratch>/<prefix>/coordinator.lease.json`."""
    return coordinator_lease(GcsLeaseFile(scratch, f"{prefix}/{COORD_LEASE}", client))


# ── The compaction lease: a running compaction holds off appends to its generation ──
#
# A compaction (hours) folds the generation's runs as its job finds them; a scan appended to that generation meanwhile
# supersedes it (`compact_store`'s guard discards the new generation). So a compaction holds its own lease for its whole
# run, whoever submitted it and whether or not the submitter still waits: `Runner.compact_stage` writes the record for
# its job before submitting it (`holder` = the job's name, alive `COMPACT_START_S` while Batch schedules it); the job
# adopts it (`run_compaction`) before reading the generation and renews it every `HEARTBEAT_S` (alive `COORD_TTL_S`: a
# crashed job's lapses within ~15 min) until it ends. `Runner.append` refuses (`Busy`, exit 4) while a live one names
# the generation it would append to; once the compaction wrote its successor, the append follows there.

#: The compaction lease's name under the store's prefix in the scratch bucket (`<prefix>/compacting.lease.json`): per
#: store, its record naming the generation being compacted (`gen`).
COMPACT_LEASE = "compacting.lease.json"
#: How long the submitter's record lives before its job adopts it: Batch's scheduling of a STANDARD VM, with margin. A
#: job that never starts holds appends off this long.
COMPACT_START_S = 3600


def compaction_holds(file: LeaseFile, gen: str, now: datetime) -> dict | None:
    """The compaction lease's record, when a live one (within its `ttl_s`) is compacting generation `gen`; else None."""
    if (cur := file.read()) is None:
        return None
    held = cur[0]
    return held if held.get("gen") == gen and not lease_stale(held, now, COORD_TTL_S) else None


@contextmanager
def compaction_lease(file: LeaseFile, gen: str, *, holder: str | None = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                     ttl_s: int = COORD_TTL_S, heartbeat_s: float = HEARTBEAT_S, log: Callable[[str], None] = err) -> Iterator[CoordinatorLease]:
    """Hold the compaction lease on `file` for generation `gen` (`holder`: `$DT_JOB_NAME`, else host:pid), adopting the
    record its submitter wrote for it, renewed every `heartbeat_s` (a failed renewal retried at the next); raises `Busy`
    when another live compaction holds it. Released on the way out."""
    lease = CoordinatorLease(file, holder=holder or default_holder(), now=now, ttl_s=ttl_s, extra={"gen": gen}, adopt=True,
                             tolerate_errors=True, what="compaction lease", log=log)
    lease.take()
    with heartbeat(lease, heartbeat_s):
        yield lease


def gcs_compaction_lease_file(scratch: str | None, prefix: str, client=None) -> LeaseFile | None:
    """The compaction lease's file at `gs://<scratch>/<prefix>/compacting.lease.json` (None without a scratch bucket)."""
    return GcsLeaseFile(scratch, f"{prefix}/{COMPACT_LEASE}", client) if scratch else None


class LocalRunStore:
    """`RunStore` over local dirs: the generation's (`root`) and the scratch side's (tests, and a local dry run)."""

    def __init__(self, root: Path, scratch: Path, gen: str = "local"):
        self.root, self.scratch, self.gen = Path(root), Path(scratch), gen

    def listing(self, prefix: str) -> dict[str, int]:
        return {k: p.stat().st_size for p in sorted(self.root.rglob("*")) if p.is_file() and (k := p.relative_to(self.root).as_posix()).startswith(prefix)}

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.listing(prefix))

    def mtimes(self, prefix: str) -> dict[str, float]:
        return {k: (self.root / k).stat().st_mtime for k in self.listing(prefix)}

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
                    fh.write(json.dumps({"owner": owner, "at": now.isoformat(), "ttl_s": ttl_s}) + "\n")
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

    def delete(self, keys: list[str]) -> None:
        """`keys` removed (one already gone is skipped), then each emptied dir up to the root."""
        rm_local(self.root, keys)


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

    def mtimes(self, prefix: str) -> dict[str, float]:
        blobs = self.client.list_blobs(self.bucket, prefix=f"{self.prefix}/{prefix}")
        return {x.name.removeprefix(self.prefix + "/"): x.updated.timestamp() for x in blobs}

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
                blob.upload_from_string(json.dumps({"owner": owner, "at": now.isoformat(), "ttl_s": ttl_s}) + "\n", if_generation_match=0)
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

    def delete(self, keys: list[str]) -> None:
        delete_objects(self.b, [f"{self.prefix}/{k}" for k in keys])

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
    nothing. No carries: those run apart (`merge_pending`). Raises `SystemExit` on a refusal.

    Never into a compacted generation (its `successor.json`; the rerun follows it and appends there), checked before
    the manifest is written and again after: a compaction checks the newest manifest after writing `successor.json`
    (`compact_store`), so a race is caught by one side or both, never published past."""
    key = f"manifests/{manifest_name(scan)}"
    if store.exists(key):
        raise SystemExit(f"{key} exists: manifests are never rewritten")
    if store.exists(SUCCESSOR):
        raise SystemExit(f"not publishing {key}: generation {store.gen} was compacted ({SUCCESSOR}); the rerun appends to its successor")
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
    if store.exists(SUCCESSOR):
        raise SystemExit(f"{key} published while generation {store.gen} was compacted ({SUCCESSOR} appeared): when the compaction "
                         f"kept its successor, that lacks scan {scan}; the rerun appends it there")
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

    def __init__(self, sink: MergeStatus | None, owner: str, now: Callable[[], datetime], log: Callable[[str], None], heartbeat_s: float | None = HEARTBEAT_S,
                 lease_s: int = LEASE_S):
        self.sink, self.now, self.log, self.heartbeat_s, self.lease_s = sink, now, log, heartbeat_s, lease_s
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
                "heartbeat_s": self.heartbeat_s, "lease_s": self.lease_s, "manifest": manifest,
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
    returns `{rows, bytes, s}` (`rec`: the run's record, its phases prefixed with the carry's, for the build's steps); `missing(store, runs)` the reader files listed runs lack; `drilled(store, runs)` the runs
    carrying a drill (merged only with each other, `plan_carries`); `refuse(store, before, after)` why a revision listing
    `after` in place of `before` must not be published, or None; `long_rows`: input rows from which a merge runs long
    (`merge_is_long`; None: by level only)."""
    build: Callable[..., dict]
    missing: Callable[[RunStore, list[dict]], list[str]]
    drilled: Callable[[RunStore, list[dict]], set[str]] = lambda store, runs: set()
    refuse: Callable[[RunStore, list[dict], list[dict]], str | None] = lambda store, before, after: None
    long_rows: int | None = None


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
                  status: MergeStatus | None = None, heartbeat_s: float | None = HEARTBEAT_S, log: Callable[[str], None] = err,
                  source=None, rec: Recorder | None = None) -> dict:
    """Run the newest manifest's due carries (`plan_carries`), one merged run at a time (`carry.build`), each published as
    a revision (`publish_revision`) before the next is planned, until none is due (or `max_merges`). `mount`: the
    generation's dir on a mount of the data bucket (the inputs are read from it). Holds the merge lease throughout
    (returns `{"held": …}` without merging when another holder has it). A failure leaves the newest manifest as it was
    (every run it lists whole), and a rerun resumes: a merged dir already whole is reused. `max_level`: the compaction
    level, which no carry reaches (`plan_carries`; None: unbounded). `status`: where the lease is mirrored for /health
    (`StatusMirror`: written as each carry starts, refreshed every `heartbeat_s`, removed on a clean end, marked failed on
    a raise; best-effort throughout). `rec`: the run's record on `/scans` (`maint_runs`, kind `merge`): started here
    (its `scan` the newest manifest's scan), a phase per merged run (`carry L<level>`), `failed` on a raise; the caller
    finishes it (`finish_merge`, after its prune)."""
    from .maint_runs import NULL, Prefixed, gib, phase

    rec = rec or NULL
    owner = owner or f"{os.environ.get('BATCH_JOB_UID') or socket.gethostname()}:{os.getpid()}"
    cur = newest(store)
    if cur is None:
        return {"merged": [], "manifest": None}
    if dry_run:
        _, merges = plan_carries(cur[1]["runs"], carry.drilled(store, cur[1]["runs"]), max_level)
        return {"manifest": cur[0], "plan": [{"inputs": [r["key"] for r in ins], "output": out["key"], "level": out["level"]} for ins, out in merges]}
    scan, _ = parse_manifest(cur[0].removeprefix("manifests/")) or (None, 0)
    rec.start(run_scans(cur[1]["runs"])[-1] if cur[1].get("runs") else scan, getattr(store, "prefix", None) or store.gen)
    ttl = merge_ttl(plan_carries(cur[1]["runs"], carry.drilled(store, cur[1]["runs"]), max_level)[1], carry.long_rows)
    if held := store.lease(owner, now(), ttl):
        log(f"merge: the lease is held ({held}); not merging")
        with phase(rec, "lease") as ph:
            ph.note = f"held by {held.get('owner') if isinstance(held, dict) else held}: not merging"
        return {"held": held, "merged": [], "manifest": cur[0]}
    done: list[dict] = []
    running: str | None = None
    names: list[str] = []
    mirror = StatusMirror(status, owner, now, log, heartbeat_s, ttl)
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
            base_name = f"carry L{out['level']}"
            n = sum(x.split(" #")[0] == base_name for x in names)
            names.append(name := base_name if not n else f"{base_name} #{n + 1}")
            rec.begin(running := name)
            if complete(store, carry, out):
                log(f"merge {out['key']}: already whole; publishing it")
                meta = store.read_json(f"{out['key']}/meta.json")
                doc = {"rows": meta.get("rows"), "bytes": meta.get("bytes"), "s": {}}
            else:
                outp = tmp / "merge" / out["key"]
                root, reads = read_runs(store, [r["key"] for r in ins], mount, tmp / "read", source=source, log=log)
                doc = carry.build([root / r["key"] for r in ins], out, outp, drilled=all(r["key"] in drilled for r in ins), tmp=tmp, log=log,
                                  rec=Prefixed(rec, f"{name} · "))
                doc["reads"] = reads
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
                         "rows": out.get("rows"), "manifest": pub.key if pub else None, "tiers_s": doc["s"], "reads": doc.get("reads"), "s": round(monotonic() - t0, 1)})
            log(f"merge {out['key']} ({len(ins)} runs, {len(out['scans'])} scans): {pub.key if pub else 'already listed'} in {done[-1]['s']:.0f}s")
            rec.end(name, f"{len(ins)} runs → {out['key']} ({len(out['scans'])} scans"
                    + (f", {out['rows']:,} rows" if isinstance(out.get("rows"), int) else "") + (f", {gib(out['bytes'])}" if out.get("bytes") is not None else "")
                    + f"): {pub.key if pub else 'already listed'} in {done[-1]['s']:.0f}s")
            running = None
    except BaseException as e:
        mirror.failed(e)
        if running is not None:
            rec.end(running, f"failed: {type(e).__name__}: {e}", failed=True)
        rec.finish("failed", error=f"{type(e).__name__}: {e}")
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
    diagnostics = ""
    if module == "static_drill" and args[0] == "build":
        diagnostics = drill_exit_diagnostics()
    elif module == "static_anchors" and args[0] == "run":
        diagnostics = drill_exit_diagnostics("ANCHOR")
    elif module == "static_merge" and args[0] in ("carry", "probe"):
        diagnostics = drill_exit_diagnostics("MERGE")
    return f"set -euo pipefail; {diagnostics}{prep} && cd /stage && {py} dt_cloud.{module} {shlex.join([*args, *m])}"


def drill_exit_diagnostics(prefix: str = "DRILL") -> str:
    """The surviving shell records kernel memory counters even when its Python child is SIGKILLed."""
    return """trap 'rc=$?; printf "PREFIX_EXIT code=%s\\n" "$rc"; for f in /sys/fs/cgroup/memory.events /sys/fs/cgroup/memory.events.local /sys/fs/cgroup/memory/memory.failcnt /sys/fs/cgroup/memory/memory.oom_control; do if [ -r "$f" ]; then printf "PREFIX_CGROUP %s\\n" "$f"; cat "$f"; fi; done' EXIT; """.replace("PREFIX", prefix)


def duckdb_args(machine: str) -> list[str]:
    """`-M <mem> -p <threads>` for a task's DuckDB sized to its machine: ¾ of its memory (7,700 MiB per vCPU), every vCPU."""
    vcpus = int(machine.rsplit("-", 1)[-1])
    return ["-M", f"{vcpus * 7700 * 3 // 4 // 1024}GB", "-p", str(vcpus)]


def job_spec(p: Profile, name: str, tasks: int, commands: list[str], *, stage: str, purpose: str, component: str, scratch: bool = True,
             r2: bool = False, machine: str | None = None, ssd_gb: int | None = None, long: bool = False) -> dict:
    """A Batch job of `tasks` tasks (in parallel) running `commands` (one shell script, `&&`-chained) in the image, the
    data bucket (and `scratch`) mounted read-only under /gcs, a local SSD at /stage. `r2`: as the R2 account, with its
    credentials from Secret Manager. `purpose` labels the job; `component` is its cost label (`cost_labels`). `long`: run it as a
    `LONG_STAGES` stage (STANDARD, `LONG_RUN_S`) whatever its stage (a merge reaching `LONG_MERGE_LEVEL`)."""
    per = getattr(p, "machine_for", None)  # a profile with per-stage overrides (static names); else its one machine
    machine = machine or (per(stage) if per else p.machine)
    vcpus = int(machine.rsplit("-", 1)[-1])
    long = long or stage in LONG_STAGES
    spot = p.spot and stage not in p.standard_stages and not long
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
                "maxRetryCount": 3 if spot else 0,
                "maxRunDuration": f"{LONG_RUN_S if long else MAX_RUN_S}s",
                "volumes": [
                    *({"gcs": {"remotePath": b}, "mountPath": f"/mnt/disks/gcs/{b}", "mountOptions": ["--implicit-dirs"]} for b in buckets),
                    {"deviceName": "stage", "mountPath": "/mnt/disks/stage"},
                ],
            },
        }],
        "allocationPolicy": {
            "instances": [{"policy": {
                "machineType": machine,
                "provisioningModel": "SPOT" if spot else "STANDARD",
                "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
                "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": str(ssd_gb or (p.ssd_for(stage) if hasattr(p, "ssd_for") else p.ssd_gb))}, "deviceName": "stage"}],
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


#: A poll of a Batch job that fails transiently (`transient`) is retried this many times in a row, backing off from the
#: poll delay up to `POLL_RETRY_MAX_S`, before the stage fails (2026-10-22: one `ConnectionResetError` polling a 7 h
#: compaction failed its submitter's stage and dropped the coordinator lease).
POLL_RETRIES = 5
POLL_RETRY_MAX_S = 120


def transient(e: BaseException) -> bool:
    """Whether `e` (from an HTTP call) may pass on a retry: a connection / timeout error (`OSError`, which `requests`'
    exceptions are), unless it carries an HTTP response that isn't 429 or 5xx."""
    if not isinstance(e, OSError):
        return False
    status = getattr(getattr(e, "response", None), "status_code", None)
    return status is None or status == 429 or status >= 500


class BatchRunner:
    """Submit a Batch job and wait for it (REST over ADC, `batch.submit_job` / `gcp.batch_job`); a transient poll error is
    retried (`POLL_RETRIES`)."""

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
        delay, t0, fails = self.delay, monotonic(), 0
        while True:
            if wait is not None and monotonic() - t0 >= wait:
                raise StillRunning(f"Batch job {name}: still running after {wait:.0f}s")
            try:
                st = batch_job(name, project=self.p.project, region=self.p.region).get("status", {})
            except Exception as e:
                if not transient(e) or fails >= POLL_RETRIES:
                    raise
                fails += 1
                back = min(self.delay * 2 ** (fails - 1), POLL_RETRY_MAX_S)
                self.log(f"{name}: polling failed ({type(e).__name__}: {e}): retry {fails}/{POLL_RETRIES} in {back:.0f}s")
                time.sleep(back)
                continue
            fails = 0
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


# ── Prune: the runs a carry folded ─────────────────────────────────────────


def run_span(key: str) -> tuple[str, str] | None:
    """`(first, last)` of a run key `deltas/<first>[_<last>]`, or None for anything else."""
    first, _, last = key.removeprefix("deltas/").partition("_")
    last = last or first
    return (first, last) if key.startswith("deltas/") and SCAN_ID.fullmatch(first) and SCAN_ID.fullmatch(last) else None


#: How long a superseded manifest's runs are kept (`plan_prune`): ≫ readers' 5 min cache of the newest manifest, so a
#: reader holding any manifest it may still serve never 404s, however many manifests land at once (a catch-up, a carry
#: right after a publish).
GRACE_S = 3600
#: The least grace a CLI accepts (twice the readers' 5 min manifest cache).
MIN_GRACE_S = 600


def grace_manifests(manifests: Collection[tuple[str, float]], now: float, grace_s: float = GRACE_S) -> list[str]:
    """The manifests whose runs a prune keeps, oldest first, from `(key, mtime)` pairs (generation-relative keys, epoch
    seconds; non-manifest keys ignored): the newest, and each superseded less than `grace_s` before `now`, its
    superseded time the next manifest's (in key order: readers' order) write time. `[]` without a manifest."""
    t = dict(manifests)
    ms = manifest_keys(t)
    return [k for k, nxt in zip(ms, ms[1:]) if now - t[nxt] < grace_s] + ms[-1:]


def plan_prune(listing: Collection[str], manifests: Collection[tuple[str, float]], runs_of: dict[str, Collection[str]], now: float, *,
               grace_s: float = GRACE_S, retain: Collection[str] = ()) -> list[str]:
    """The keys to delete once a manifest is published: the files of every run a carry folded, kept for `grace_s` after
    the last manifest listing them was superseded. `listing`: the store's generation-relative keys (its `deltas/` at
    least); `manifests`: its manifests as `(key, mtime)`; `runs_of`: the run keys each manifest `grace_manifests` keeps
    lists (the newest, and those superseded within the grace: usually 2–3; others are ignored, a missing one raises
    `KeyError`). Of the run dirs under `deltas/`, a file is deleted when its run is

    - listed by none of those manifests (a reader holding one, cached, never 404s), and
    - inside one run the newest manifest M lists (its scan span within that run's): a run some carry folded into it. A
      dir no manifest lists that isn't, is kept: an append in flight (its scan is past M's), or a merge in flight (its
      output spans several of M's runs, until its revision lists it);

    and its run-relative path is not under `retain` (prefixes, or exact names: what later appends still read of a folded
    run, e.g. the static index's `cdelta/`). Never a manifest, nor a file of the base generation (outside `deltas/`).
    Sorted; `[]` without a manifest."""
    kept = grace_manifests(manifests, now, grace_s)
    if not kept:
        return []
    newest = [run_span(k) for k in runs_of[kept[-1]]]
    keep = {r for m in kept for r in runs_of[m]}
    out = []
    for key in sorted(listing):
        run, sep, rel = key.partition("/")[2].partition("/")
        run = f"deltas/{run}"
        if not key.startswith("deltas/") or not sep or run in keep or (span := run_span(run)) is None:
            continue
        if not any(s is not None and s[0] <= span[0] and span[1] <= s[1] for s in newest):
            continue
        if any(rel == r or (r.endswith("/") and rel.startswith(r)) for r in retain):
            continue
        out.append(key)
    return out


class PruneTarget(Protocol):
    """A store `prune_folded` deletes from: `listing(prefix)` (`{key: size}`, generation-relative), `mtimes(prefix)`
    (`{key: write time}`, epoch seconds), `read_json(key)`, `delete(keys)`."""

    def listing(self, prefix: str) -> dict[str, int]: ...
    def mtimes(self, prefix: str) -> dict[str, float]: ...
    def read_json(self, key: str) -> dict: ...
    def delete(self, keys: list[str]) -> None: ...


def finish_merge(rec: Recorder | None, doc: dict, prune: Callable[[], dict]) -> dict:
    """A merge job's end (both stores' `carry`): `prune()` (`prune_folded`'s summary) as the run's `prune` phase, then
    the run finished (`maint_runs`): `succeeded` when it merged a run, else `nop` (nothing due, the lease held), or
    `failed` (re-raised) when the prune raises. Returns `prune()`'s summary."""
    from .maint_runs import NULL, gib, phase

    rec = rec or NULL
    try:
        with phase(rec, "prune") as ph:
            got = prune()
            ph.note = f"{len(got.get('runs', []))} folded runs, {got.get('objects', 0)} objects, {gib(got.get('bytes', 0))} " + ("deleted" if got.get("deleted") else "to delete")
    except BaseException as e:
        rec.finish("failed", error=f"{type(e).__name__}: {e}")
        raise
    rec.finish("succeeded" if doc.get("merged") else "nop")
    return got


def prune_folded(target: PruneTarget, *, now: float | None = None, grace_s: float = GRACE_S, retain: Collection[str] = (),
                 dry_run: bool = False, where: str = "", log: Callable[[str], None] = err) -> dict:
    """Delete from `target` the runs its own manifests' carries folded (`plan_prune` over its listing and its manifests'
    write times, reading only the manifests `grace_manifests` keeps: a store copied from another, as R2 from GCS, is
    planned by what its readers read), each run logged with its objects and bytes. Call it after a publish to `target`
    succeeds. `now`: epoch seconds (default the clock). Returns `{manifest, grace, runs: [{key, objects, bytes}], objects,
    bytes, deleted, keys}` (`grace`: the superseded manifests kept; `deleted` 0 on a `dry_run`)."""
    now = time.time() if now is None else now
    manifests = list(target.mtimes("manifests/").items())
    listing = target.listing("deltas/")
    kept = grace_manifests(manifests, now, grace_s)
    runs_of = {k: [r["key"] for r in target.read_json(k)["runs"]] for k in kept}
    keys = plan_prune(listing, manifests, runs_of, now, grace_s=grace_s, retain=retain)
    runs: dict[str, dict] = {}
    for k in keys:
        r = runs.setdefault("/".join(k.split("/", 2)[:2]), {"objects": 0, "bytes": 0})
        r["objects"] += 1
        r["bytes"] += listing[k]
    total = sum(listing[k] for k in keys)
    verb = "would delete" if dry_run else "deleting"
    for key, r in runs.items():
        log(f"prune{where}: {verb} {key}/ ({r['objects']} objects, {r['bytes']:,} B)")
    log(f"prune{where}: {verb} {len(runs)} folded runs, {len(keys)} objects, {total:,} B"
        + (f" (newest {kept[-1]}; superseded within {grace_s:g} s: {', '.join(kept[:-1]) or 'none'})" if kept else " (no manifest)"))
    if keys and not dry_run:
        target.delete(keys)
    return {"manifest": kept[-1] if kept else None, "grace": kept[:-1],
            "runs": [{"key": k, **r} for k, r in runs.items()], "objects": len(keys), "bytes": total,
            "deleted": 0 if dry_run else len(keys), "keys": keys}


class R2RunStore:
    """`PruneTarget` over a generation's copy on R2 (`<root>/…` in `bucket`), over a boto3 S3 `client`."""

    def __init__(self, client, bucket: str, root: str):
        self.client, self.bucket, self.root = client, bucket, root.rstrip("/")

    def listing(self, prefix: str) -> dict[str, int]:
        out = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=f"{self.root}/{prefix}"):
            out.update({o["Key"].removeprefix(self.root + "/"): int(o["Size"]) for o in page.get("Contents", [])})
        return out

    def mtimes(self, prefix: str) -> dict[str, float]:
        out = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=f"{self.root}/{prefix}"):
            out.update({o["Key"].removeprefix(self.root + "/"): o["LastModified"].timestamp() for o in page.get("Contents", [])})
        return out

    def read_json(self, key: str) -> dict:
        return json.loads(self.client.get_object(Bucket=self.bucket, Key=f"{self.root}/{key}")["Body"].read())

    def delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            got = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": f"{self.root}/{k}"} for k in keys[i:i + 1000]], "Quiet": True})
            if errs := got.get("Errors"):
                raise RuntimeError(f"R2 delete: {len(errs)} of {len(keys[i:i + 1000])} failed (e.g. {errs[0]})")


def r2_run_store(root: str) -> R2RunStore:
    """The R2 copy of the store at `root` (`<prefix>/<gen>`), from the env (`publish.r2_client`, `publish.r2_bucket`)."""
    from . import publish as pub

    return R2RunStore(pub.r2_client(), pub.r2_bucket(), root)


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


#: Attempts at one object's copy (`r2_copy`) R2 throttled past botocore's own retries, backing off `R2_RETRY_S · 2^n`
#: (cw's level-5 compaction's copy died on one throttled multipart part, 2026-10-29).
R2_ATTEMPTS = 5
R2_RETRY_S = 15.0
#: The error codes R2 throttles with (`ServiceUnavailable: Reduce your concurrent request rate for the same object`).
R2_THROTTLED = ("ServiceUnavailable", "SlowDown", "TooManyRequests")
_R2_THROTTLED_RE = re.compile(r"\((?:" + "|".join(R2_THROTTLED) + r")\)")


def r2_throttled(e: BaseException) -> bool:
    """R2 throttled the request: botocore's `ClientError` (`(<code>)` in its message) or boto3's `S3UploadFailedError`
    wrapping one (a multipart upload's failed part)."""
    return bool(_R2_THROTTLED_RE.search(str(e)))


def r2_copy(bucket: str, objs: list, *, workers: int = 8, dry_run: bool = False, log: Callable[[str], None] = err) -> dict:
    """Copy `objs` GCS → R2 under the same keys (`publish.copy_one`'s streaming copy, the GCS md5 stamped as metadata),
    skipping objects already there with the same size and md5. An object R2 throttles is copied again, whole (at most
    `R2_ATTEMPTS` times); any other failure raises. Returns `{objects, copied, bytes, s}` (`keys` too on a dry run: what
    would be copied)."""
    from . import publish as pub

    s3, r2 = pub.r2_client(), pub.r2_bucket()
    with ThreadPoolExecutor(workers) as ex:
        todo = [o for o, do in ex.map(lambda o: (o, pub.should_copy(o, pub.head_dest(s3, r2, o.key))), objs) if do]
    total = sum(o.size for o in todo)
    if dry_run:
        return {"objects": len(objs), "copied": 0, "bytes": total, "s": 0, "keys": [o.key for o in todo]}
    t0, done = monotonic(), 0

    def one(o):
        for n in range(R2_ATTEMPTS):
            try:
                pub.copy_one(bucket, s3, r2, o)
                return o
            except Exception as e:
                if not r2_throttled(e) or n == R2_ATTEMPTS - 1:
                    raise
                log(f"  ↻ {o.key}: R2 throttled ({e}); again in {R2_RETRY_S * 2 ** n:.0f}s")
                time.sleep(R2_RETRY_S * 2 ** n)
        raise AssertionError("unreachable")

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
        if not objs_:
            raise SystemExit(f"r2 {manifest}: {r['key']} has no served files on GCS (folded and pruned?): manifest not copied")
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
    #: The manifests this run's R2 jobs copied (stems): the merge stage doesn't copy one again.
    copied: set = field(default_factory=set)
    #: Why the merge stage found a compaction due (`compaction_why`: the rule that fired), or None: for the job's record.
    compaction: str | None = None
    #: Compact after the carries whatever the policy says (`-K`), when the generation has runs.
    compact_now: bool = False
    #: A due compaction is not run here (`-D`): `deferred` says why it was due, for the caller to run it on its own (a
    #: scan's step submits a coordinator of its own rather than wait out an hours-long fold inside the scan).
    defer_compaction: bool = False
    deferred: str | None = None
    #: The compaction stage writes a candidate (`compact -N`): no compaction lease taken, nothing pointed at it.
    candidate: bool = False
    #: The submitter's env: what its merge and compaction jobs record their runs with (`record_env`).
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    #: The coordinator lease (`coordinator_lease`), held for all of `run`; yields its check, which `stage` calls before and
    #: after each stage. None: no lease (dry runs, tests).
    coordination: Callable[[], AbstractContextManager[Callable[[], None]]] | None = None
    check_lease: Callable[[], None] | None = None
    #: The compaction lease's file (`compaction_lease`): `append` waits (`Busy`) while a live compaction holds the
    #: generation, and `compact_stage` writes it for its job. None: no lease (dry runs, tests).
    compacting: Callable[[], LeaseFile] | None = None

    store_prefix: ClassVar[str]
    job_prefix: ClassVar[str]
    #: Input rows from which its merge job runs long (`merge_is_long`, the store's `Carry.long_rows`); None: by level only.
    long_merge_rows: ClassVar[int | None] = None

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
        """Append `scan_id` (and with `catch_up` every earlier pending scan, in order), then the merge stage, holding the
        coordinator lease throughout (`coordination`; `Busy` when another run holds it). Returns the scans appended."""
        if self.coordination is None:
            return self.append(scan_id, catch_up)
        with self.coordination() as check:
            self.check_lease = check
            try:
                return self.append(scan_id, catch_up)
            finally:
                self.check_lease = None

    def append(self, scan_id: str, catch_up: bool) -> list[str]:
        self.follow()
        self.wait_compaction()
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
        self.run_scans(todo)
        self.carries()
        return todo

    def run_scans(self, todo: list[str]) -> None:
        """Each scan's chain (`one`), in turn."""
        for s in todo:
            self.wait_compaction()
            self.one(s)

    def wait_compaction(self) -> None:
        """`Busy` while a live compaction holds the generation (`compaction_holds`): a scan appended to it would supersede
        the compaction; once it wrote its successor, the next run appends there (`follow`)."""
        if self.compacting is not None and (held := compaction_holds(self.compacting(), self.p.gen, self.now())):
            raise Busy(f"generation {self.p.gen} is being compacted by {held['holder']} (since {held['at']}): the append waits for its successor")

    def one(self, d: str) -> None:
        raise NotImplementedError

    def rerun_note(self) -> str:
        return "the R2 copy and prune only"

    def rerun(self, d: str) -> None:
        """An appended scan, run again: the R2 copy of the newest manifest (its own, or a revision of it: an older one's
        folded runs may be pruned) and prune."""
        keys = self.manifests()
        stem = keys[-1].rsplit("/", 1)[-1].removesuffix(".json") if keys else d
        self.r2(d, None if stem == d else stem)
        self.copied.add(stem)
        self.stage(f"{d} prune", lambda: self.prune(d))

    def stage(self, name: str, fn: Callable[[], None]) -> None:
        if self.dry_run:
            self.log(f"{name}: would run")
            return
        if self.check_lease:
            self.check_lease()
        t = monotonic()
        self.log(f"{name}: start")
        hb.set_stage(name)
        fn()
        hb.after(name)
        self.timings[name] = round(monotonic() - t, 1)
        if self.check_lease:
            self.check_lease()
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
        spec = self.spec(name, tasks, [cmd], stage=stage, **kw)
        return name, label_batch_spec(spec, spec.get("labels", {}).get("component", self.job_prefix), scan_id=scan_id)

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

    def merge_job(self, scan: str, long: bool = False) -> tuple[str, dict]:
        """The merge job (one task: the due carries, each a revision), with the R2 credentials when `merge_r2`; `long`: as a
        long stage (`merge_is_long`)."""
        raise NotImplementedError

    def merge_r2(self) -> bool:
        """Whether the merge job gets the R2 copy's credentials, for its status record on R2 (`r2_merge_status`): when the
        R2 account is the stages' own (a job runs as one account, and the merge's writes the data bucket). Otherwise the
        merge runs without, and /health doesn't see it."""
        return self.p.r2_sa in (None, "", self.p.sa)

    def r2(self, d: str, manifest: str | None = None) -> None:
        """The R2 job for `manifests/<manifest>.json` (default `d`'s own): its runs, checked there, then it, last."""
        raise NotImplementedError

    def read_args(self) -> list[str]:
        """The merge and compaction jobs' `-F` (the profile's `read_from`; none for the default `gcs`). Reading the R2 copy
        needs its credentials in the job, so the R2 account must be the stages' own (`merge_r2`)."""
        if self.p.read_from == "r2" and not self.merge_r2():
            raise SystemExit(f"read_from r2: the merge and compaction jobs run as {self.p.sa}, not the R2 account {self.p.r2_sa}: "
                             "they can't read the R2 copy (read from gcs, or make the R2 account the stages')")
        return read_from_arg(self.p)

    def record_env(self, name: str, spec: dict) -> dict:
        """`spec` (a merge or compaction job, named `name`) with what its run's record on `/scans` needs (`maint_runs`):
        its job facts (`DT_JOB_NAME`, `DT_JOB_REGION`, `DT_JOB_MACHINE`, `DT_JOB_SPOT`, `DT_JOB_IMAGE`), and when the
        profile names the D1 token's secret (`record_secrets`) and this process has `$D1_DB_ID` and
        `$CLOUDFLARE_ACCOUNT_ID`, those (with `$SCAN_RUNS_PROFILE`, for the cost) and the token from Secret Manager.
        Without them the job runs unrecorded (it says so in its log)."""
        from .maint_runs import FACT_ENV, PASS_ENV

        env = spec["taskGroups"][0]["taskSpec"].setdefault("environment", {})
        v = env.setdefault("variables", {})
        inst = spec["allocationPolicy"]["instances"][0]["policy"]
        image = spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["imageUri"].rsplit("/", 1)[-1]
        facts = {"job_name": name, "region": self.p.region, "machine": inst["machineType"],
                 "spot": "1" if inst["provisioningModel"] == "SPOT" else "0", "image": image}
        v.update({FACT_ENV[k]: x for k, x in facts.items() if x})
        secrets = dict(getattr(self.p, "record_secrets", None) or {})
        missing = [k for k in ("D1_DB_ID", "CLOUDFLARE_ACCOUNT_ID") if not (self.environ.get(k) or "").strip()]
        if secrets and not missing:
            v.update({k: self.environ[k].strip() for k in PASS_ENV if (self.environ.get(k) or "").strip()})
            env.setdefault("secretVariables", {}).update({k: f"projects/{self.p.project}/secrets/{x}/versions/latest" for k, x in sorted(secrets.items())})
        else:
            # said here, where it's decided: the job only logs that it isn't recorded (c001's 16 h compaction never reached /scans)
            self.log(f"{name}: not recorded on /scans: " + (f"no {', '.join('$' + k for k in missing)} in the submitter's environment"
                                                            if secrets else "the profile names no `record_secrets`"))
        return spec

    def compact_jobs(self, scan: str) -> list[tuple[str, dict]]:
        """The compaction's Batch jobs, in order: `compact` (one task: `run_compaction`, GCS, then R2 when the job can hold
        the R2 credentials, `merge_r2`), else then `compact-r2` as the R2 account (the new generation to R2, its pointer
        last)."""
        raise NotImplementedError

    def with_gen(self, gen: str) -> Any:
        """`cfg` with generation `gen` (its successor's): what the chain appends to once a compaction moved it."""
        raise NotImplementedError

    def follow(self) -> None:
        """Follow the profile's generation to its newest successor (`successor.json`, a compaction's forwarding record):
        the runs append there, and the profile needs no change."""
        gens: list[str] = [self.p.need("gen")]
        while self.exists(f"{self.store_prefix}/{gens[-1]}/{SUCCESSOR}"):
            nxt = self.read_json(f"{self.store_prefix}/{gens[-1]}/{SUCCESSOR}")["gen"]
            if nxt in gens:
                raise RuntimeError(f"{self.store_prefix}: successor cycle {gens + [nxt]}")
            gens.append(nxt)
        if len(gens) > 1:
            self.log(f"{gens[0]} was compacted into {gens[-1]} ({' → '.join(gens)}): appending there")
            self.cfg = self.with_gen(gens[-1])

    def compact_stage(self, scan: str) -> None:
        """The compaction stage (`compaction` says why): its Batch jobs (`compact_jobs`), the last waited on up to
        `merge_wait` s; they publish the new generation on GCS, then R2 (its pointer last), so the next run appends to it
        (`follow`). Non-fatal, as the merge stage: a rerun resumes (the job follows a finished compaction's successor).

        Before submitting, the compaction lease (`compacting`) is written for the `compact` job (its `holder`, which the
        job adopts): appends to the generation wait from then on, whether or not this run stays to watch the job. None is
        submitted while another live compaction holds it."""
        jobs = self.compact_jobs(scan)
        if jobs:
            # the first job folds and publishes: its run is recorded (`compact-r2`, the R2 account's, isn't)
            jobs[0] = (jobs[0][0], self.record_env(*jobs[0]))
            if self.compacting is not None and not self.dry_run and not self.candidate:
                lease = CoordinatorLease(self.compacting(), holder=jobs[0][0], now=self.now, ttl_s=COMPACT_START_S, extra={"gen": self.p.gen},
                                         what="compaction lease", log=self.log)
                try:
                    lease.take()
                except Busy as e:
                    self.log(f"compact: {e}: not submitting another")
                    return
        try:
            for i, (name, spec) in enumerate(jobs):
                wait = self.merge_wait if i == len(jobs) - 1 else None

                def one(name: str = name, spec: dict = spec, wait: float | None = wait) -> None:
                    self.run_job(name, spec, wait=wait)
                self.stage(f"compact: {self.compaction}" if i == 0 else f"compact: {spec['labels']['stage']}", one)
        except StillRunning as e:
            self.log(f"compact: {e}; it publishes the new generation (GCS, then R2) when done, "
                     + ("a candidate: `promote` points prod at it" if self.candidate else "and the next run appends to it"))

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
            self.follow()
            keys = self.manifests()
            if not keys:
                return
            m = self.read_json(keys[-1])
            policy = self.p.policy
            drilled = self.drilled(m["runs"])
            _, merges = plan_carries(m["runs"], drilled, policy.carry_level)
            needs_base = policy.rule == "geometric" or (policy.rule == "every" and policy.unit == "d")
            base = [s["id"] for s in self.base()["scans"]] if needs_base and m["runs"] else []
            self.compaction = why = compaction_why(m["runs"], policy, drilled, base=base)
            compact = why is not None
            if compact and policy.rule == "level":
                self.log(f"merge: level {policy.level} is due: compact into a new base generation (carries stop below it)")
            elif compact:
                self.log(f"merge: compaction due under {policy} ({why}): compact into a new base generation")
            forced = self.compact_now and bool(m["runs"]) and not compact
            if merges and forced:
                self.log(f"merge: compaction forced (-K): it folds every run, so the due carries are skipped ({len(merges)} merge{'s' * (len(merges) != 1)})")
            elif merges:
                desc = "; ".join(f"{len(ins)} runs → {out['key']} (level {out['level']})" for ins, out in merges)
                name, spec = self.merge_job(m["date"], long=merge_is_long(merges, self.long_merge_rows))
                # its status record's holder (`StatusMirror`)
                spec["taskGroups"][0]["taskSpec"]["environment"]["variables"][JOB_NAME_VAR] = name
                self.record_env(name, spec)
                try:
                    self.stage(f"merge: {desc}", lambda: self.run_job(name, spec, wait=self.merge_wait))
                except StillRunning as e:
                    self.log(f"merge: {e}; it publishes its revision on GCS when done, and R2 gets it with a later run")
                    return
                if not self.dry_run:
                    keys = self.manifests()
            elif not compact:
                self.log(f"merge: nothing due ({' '.join(f'L{r["level"]}' for r in m['runs']) or 'no runs'})")
            if not self.dry_run:
                scan, rev = parse_manifest(keys[-1].rsplit("/", 1)[-1])
                if rev and (stem := keys[-1].rsplit("/", 1)[-1].removesuffix(".json")) not in self.copied:
                    self.r2(scan, stem)
            if forced:
                self.compaction = "forced (-K)"
            if self.compaction is not None and m["runs"]:
                if self.defer_compaction and not forced:
                    self.deferred = self.compaction
                    self.log(f"compact: {self.compaction}: deferred (`-D`), for the caller to run on its own")
                else:
                    self.compact_stage(m["date"])
        except Exception as e:
            if fatal:
                raise RuntimeError(str(e)) from e
            self.log(f"merge: failed, not fatal (every listed run is whole; the next run plans again): {e}")


def exit_on(label_: str, fn: Callable[[], Any], log: Callable[[str], None]) -> Any:
    """`fn()`, a CLI's run: `NotNext` exits `NOT_NEXT`, `Busy` `BUSY`, a failed stage (`RuntimeError`, `LeaseLost`) exits 1, each logged as
    `<label>: <why>`."""
    try:
        return fn()
    except NotNext as e:
        log(f"{label_}: {e}")
        sys.exit(NOT_NEXT)
    except Busy as e:
        log(f"{label_}: {e}")
        sys.exit(BUSY)
    except RuntimeError as e:
        log(f"{label_}: {e}")
        sys.exit(1)


# ── Compaction: a generation folded into a new one (both stores, both paths) ──
#
# A store-specific `Compactor.fold` writes the new generation's files (what a fresh build over the same scans writes);
# everything around it is shared: the policy (`compaction_why`), the lineage's names (`next_gen`), an unpublished
# attempt cleared, the upload (data in the store's publish order, `compaction.json` last), `successor.json` in the old
# generation, the store's `<prefix>/CURRENT.json`, the R2 copy with its pointer last, `resolve_gen` following successors,
# the predecessor's grace (`prune_predecessors`, its parity markers carried first), and the instrumentation.

#: The store's current generation: `<prefix>/CURRENT.json` = `{"gen": …}` (what unpinned readers serve, `add` appends to).
CURRENT = "CURRENT.json"
#: In a compacted generation: `{"gen": <its successor>}`; kept when the rest of it is pruned (the forwarding record).
SUCCESSOR = "successor.json"
#: In a generation a compaction wrote, uploaded last (its completion marker): `{from, manifest, runs, base_scans, scans,
#: level[, rule, why]}`, and with `Compactor.md5s` each file's md5.
COMPACTION = "compaction.json"
#: In a candidate generation (`compact_store(promote=False)`), written before any of its other files: `{"from": <gen>}`.
#: Nothing points at a candidate until `promote`; a compaction of `<from>` refuses to replace one.
CANDIDATE = "candidate.json"
#: The least seconds between a fold's progress notes on its run record (`compact_store`).
NOTE_S = 30.0
GEN_SUFFIX = re.compile(r"(?P<root>.+)\.c(?P<n>\d{3,})")


def next_gen(gen: str) -> str:
    """The generation a compaction of `gen` writes: `<root>.c<NNN>`, NNN one past `gen`'s (`.c001` for a root)."""
    m = GEN_SUFFIX.fullmatch(gen)
    root, n = (m["root"], int(m["n"])) if m else (gen, 0)
    return f"{root}.c{n + 1:03d}"


def current(store, prefix: str) -> str | None:
    """`<prefix>/CURRENT.json`'s generation, or None."""
    key = f"{prefix}/{CURRENT}"
    return store.read_json(key)["gen"] if store.exists(key) else None


def set_current(store, prefix: str, gen: str) -> None:
    """`<prefix>/CURRENT.json` → `gen` (one object written whole: readers see the old pointer or the new)."""
    store.write_text(f"{prefix}/{CURRENT}", json.dumps({"gen": gen}) + "\n")


def generations(store, prefix: str, marker: str = "scans.json") -> list[str]:
    """The store's generations: `<prefix>/<gen>/<marker>` (a whole listing of the prefix)."""
    return sorted({k.split("/")[1] for k in store.listing(prefix) if k.count("/") == 2 and k.endswith(f"/{marker}")})


def chain(store, prefix: str, gen: str) -> list[str]:
    """`gen`, then each successor a compaction recorded (`successor.json`), to the newest."""
    out = [gen]
    while store.exists(f"{prefix}/{out[-1]}/{SUCCESSOR}"):
        nxt = store.read_json(f"{prefix}/{out[-1]}/{SUCCESSOR}")["gen"]
        if nxt in out:
            raise RuntimeError(f"{prefix}: successor cycle {out + [nxt]}")
        out.append(nxt)
    return out


def resolve(store, prefix: str) -> tuple[str | None, str]:
    """The generation an unpinned `add` appends to, and how it was found: `CURRENT.json`'s (`"current"`), else the
    store's only generation (`"sole"`), else `(None, "none")` (or `"ambiguous"`: several, no pointer)."""
    if (cur := current(store, prefix)) is not None:
        return cur, "current"
    gens = generations(store, prefix)
    if len(gens) == 1:
        return gens[0], "sole"
    return None, "ambiguous" if gens else "none"


def resolve_gen(store, prefix: str, gen: str | None, log: Callable[[str], None] = err, *, build_hint: str = "build one first") -> str:
    """The generation `add` appends to: `gen` (else `resolve`'s; the store's sole generation adopted as current), followed
    to its newest successor. A pointer naming a compacted generation (a compaction stopped between its successor record
    and the pointer) is moved to the successor."""
    cur = current(store, prefix)
    if gen is None:
        gen, how = resolve(store, prefix)
        if gen is None:
            raise SystemExit(f"{store.root}/{prefix}: no current generation ({how}: "
                             + ("name one with -g" if how == "ambiguous" else build_hint) + ")")
        if how == "sole":
            log(f"{store.root}/{prefix}/{CURRENT}: none; adopting the store's only generation {gen}")
            set_current(store, prefix, gen)
            cur = gen
    gens = chain(store, prefix, gen)
    if len(gens) > 1:
        log(f"{gen} was compacted into {gens[-1]} ({' → '.join(gens)}): appending there")
    if cur is not None and cur != gens[-1] and cur in gens:
        log(f"{store.root}/{prefix}/{CURRENT}: {cur} → {gens[-1]} (its successor)")
        set_current(store, prefix, gens[-1])
    return gens[-1]


def carry_markers(store, prefix: str, old: str, into: str, files: Iterable[str] | None = None) -> int:
    """Generation `old`'s parity markers (`verify/<id>.json`: `scan retire`'s gate) copied into `into` (a generation
    holding the same versions: one `old` was compacted or backfilled into), each unless `into` has one for the scan;
    returns how many. Before `old` is deleted, so its scans stay vouched for."""
    pre = f"{prefix}/{old}/verify/"
    keys = [k for k in (files if files is not None else store.keys(pre)) if k.startswith(pre) and k.endswith(".json") and "/" not in k[len(pre):]]
    have = set(store.keys(f"{prefix}/{into}/verify/"))
    n = 0
    for k in sorted(keys):
        dst = f"{prefix}/{into}/verify/{k[len(pre):]}"
        if dst not in have:
            store.write_bytes(dst, store.read_bytes(k))
            n += 1
    return n


def prune_predecessors(store, prefix: str, gen: str, *, now: float, grace_s: float = GRACE_S, dry_run: bool = False,
                       log: Callable[[str], None] = err) -> list[dict]:
    """Each generation `gen` was compacted from (its `compaction.json`'s `from`, and theirs while unpruned), deleted from
    `store` once its successor's `compaction.json` is `grace_s` old — but for its `successor.json` — unless the pointer
    names it. One record per predecessor still holding files: `{gen, objects, bytes, age_s, deleted}`."""
    # The chain first (a pruned generation's `compaction.json` goes with it), newest first, to the first pruned one.
    found = []
    g = gen
    while store.exists(key := f"{prefix}/{g}/{COMPACTION}"):
        old = store.read_json(key)["from"]
        files = {k: n for k, n in store.listing(f"{prefix}/{old}").items() if k != f"{prefix}/{old}/{SUCCESSOR}"}
        if not files:
            break
        found.append((g, old, store.mtimes(f"{prefix}/{g}")[key], files))
        g = old
    out = []
    cur = current(store, prefix)
    for g, old, mt, files in found:
        age = now - mt
        go = age >= grace_s and old != cur
        rec = {"gen": old, "objects": len(files), "bytes": sum(files.values()), "age_s": round(age), "deleted": 0}
        if go and not dry_run:
            rec["markers"] = carry_markers(store, prefix, old, gen, files)
            rec["deleted"] = store.delete(sorted(files))
        log(f"prune {old} (compacted into {g} {round(age)}s ago): {len(files)} objects, {rec['bytes']:,} B "
            + ("deleted" if go and not dry_run else "would be deleted" if go else f"kept (grace {grace_s:.0f}s" + (", current" if old == cur else "") + ")"))
        out.append(rec)
    return out


def compaction_doc(policy: Policy | int | None, why: str | None) -> dict:
    """`compaction.json`'s record of the rule: `{"level": L}` under a level that fired (as it always was), else
    `{"level": <carry level>, "rule": "<policy>", "why": …}` (`why` also names a forced compaction)."""
    policy = as_policy(policy)
    if policy.rule == "level" and why is not None and why.startswith("level "):
        return {"level": policy.level}
    return {"level": policy.carry_level, "rule": str(policy), "why": why}


def newest_runs(gen_dir: Path) -> tuple[str | None, list[dict]]:
    """The newest manifest of the generation in `gen_dir` (a local dir or a mount) and its runs (`(None, [])`: none)."""
    keys = sorted(p.relative_to(gen_dir).as_posix() for p in (gen_dir / "manifests").glob("*.json")) if (gen_dir / "manifests").is_dir() else []
    newest = latest_key(keys)
    return newest, json.loads((gen_dir / newest).read_text())["runs"] if newest else []


def due_in(gen_dir: Path, policy: Policy | int | None, drilled: Collection[str] = frozenset()) -> str | None:
    """`compaction_why` for the generation in `gen_dir` (its base's scans, its newest manifest's runs)."""
    _, runs = newest_runs(gen_dir)
    if not runs:
        return None
    base = [s["id"] for s in json.loads((gen_dir / "scans.json").read_text())["scans"]]
    return compaction_why(runs, policy, drilled, base=base)


def forced(gen_dir: Path, why: str = "forced (-K)") -> str | None:
    """A forced compaction's reason, when the generation in `gen_dir` has runs to fold (else None: nothing to do)."""
    return why if newest_runs(gen_dir)[1] else None


@dataclass(frozen=True)
class Compactor:
    """A store's plug into `compact_store`: `fold(gen_dir, out, work, *, gen, new_gen, runs, threads, mem, jobs, progress)`
    writes the new generation into `out` (every file a fresh build over the same scans writes) and returns a summary
    (`progress(note[, next_phase])`: the run record's hook, for its running phase's note and a next phase);
    `publish_order` the store's upload order (data, then liveness markers); `last`: the generation-relative files its
    readers open it by, copied to R2 after the rest, in order (`compaction.json` after them); `prune`: whether
    predecessors leave the store after the grace (`prune_predecessors`; off: a reader may pin the old generation,
    `store-gc -k` collects it); `md5s`: `compaction.json` records each file's md5."""
    prefix: str
    fold: Callable[..., dict]
    publish_order: Callable[[list[str]], list[str]]
    last: tuple[str, ...] = ("scans.json",)
    prune: bool = True
    md5s: bool = False
    #: What the fold reads of the generation (generation-relative keys): what a read from another copy mirrors.
    keep: Callable[[str], bool] = lambda rel: True
    #: `blobstore.upload_new`'s `tier` over `publish_order`'s keys (None: every file in turn): consecutive keys of one
    #: tier go up together, a key of tier None alone. `compaction.json` always goes up alone, last.
    publish_tier: Callable[[str], object] | None = None
    #: Whether `fold` takes `source`: the generation's store URL (`store.uri(<prefix>/<gen>)`), to copy what it reads many
    #: times onto the work disk around `gen_dir` (a bucket mount).
    source: bool = False


#: Files hashed at a time for `compaction.json`'s md5s (hashlib releases the GIL over each 1 MiB block).
MD5_WORKERS = 16


def md5s(root: Path, workers: int = MD5_WORKERS) -> dict[str, str]:
    """`{relative path: md5}` of every file under `root`, in path order, `workers` files at a time."""
    files = sorted(p for p in root.rglob("*") if p.is_file())
    with ThreadPoolExecutor(max(1, workers)) as ex:
        return {p.relative_to(root).as_posix(): h for p, h in zip(files, ex.map(_md5, files))}


def _md5(p: Path) -> str:
    import hashlib

    h = hashlib.md5()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run_scans(runs: list[dict]) -> list[str]:
    """The scans a manifest's runs cover, in order (a carry's revision keeps them; an append adds one)."""
    return [s for r in runs for s in r["scans"]]


def store_newest_runs(store, prefix: str) -> tuple[str | None, list[dict]]:
    """`newest_runs` read from the store itself (`<prefix>/manifests/`, listed fresh), not a mount or a mirror."""
    keys = [k.removeprefix(f"{prefix}/") for k in store.keys(f"{prefix}/manifests/")]
    newest = latest_key(keys)
    return newest, store.read_json(f"{prefix}/{newest}")["runs"] if newest else []


def compact_store(store, c: Compactor, gen: str, gen_dir: Path, work: Path, *, policy: Policy | int | None, why: str, threads: int = 4,
                  mem: str = "4GB", jobs: int = 1, r2: Callable[[str, str], dict] | None = None, reads: dict | None = None,
                  promote: bool = True, rec: Recorder | None = None, log: Callable[[str], None] = err) -> dict:
    """Generation `gen` of `store` (a `blobstore.Store`; its files readable in `gen_dir`: a mirror or a mount) folded
    into the next one (`next_gen`, by `c.fold`) and published: an earlier, unpublished attempt's keys removed; the new
    generation's files in `c.publish_order`, `compaction.json` last; `successor.json` in `gen`; `CURRENT.json` when it
    named `gen` (or nothing); then `r2(new, gen)` (the R2 copy, its pointer last), when given. `reads`: what the caller
    read the generation from (`{source, bytes, s}`), recorded with the summary. Returns a summary (seconds, peak
    work-disk and RSS GiB).

    An append can land on `gen` while the fold runs (hours): the new generation would lack its scan. So `gen`'s newest
    manifest must still cover exactly the folded scans (a carry's revision may replace it; it keeps them) just before
    `successor.json` is written, and again just after (`publish_scan` checks for `successor.json` after writing its
    manifest, so one of the two sees the other). A scan that landed: the new generation is deleted (and the successor
    record, when written), nothing else moves, and the summary says `superseded`; the next compaction folds it all.

    `promote` off: a candidate. `candidate.json` goes up first, then the generation as above (`r2` copies it too), but
    no `successor.json` and no pointer: readers and appends stay on `gen` until `promote_gen` (or the candidate is
    deleted). Appends landing on `gen` meanwhile don't supersede it; `promote_gen` wants them in the candidate. An
    existing candidate is replaced only by another candidate; a promoting compaction refuses it.

    `rec`: the run's record (`maint_runs`): phases `fold` (the interval store's then `sorts`: the fold's `progress`
    hook moves it on), `upload`, `pointer`, `r2`, each with its note (rows, files, GiB, seconds, work-disk / RSS peaks)."""
    from .blobstore import upload_new
    from .maint_runs import NULL, PhaseHook, gib, phase
    from .static_local import DiskPeak, rss_peak_gb

    rec = rec or NULL

    new = next_gen(gen)
    new_prefix = f"{c.prefix}/{new}"
    if stale := store.listing(new_prefix):
        # Not yet recorded as `gen`'s successor (`resolve_gen` would have followed it): an interrupted compaction's, or a
        # candidate (whole or not) awaiting `promote_gen`.
        cand = f"{new_prefix}/{CANDIDATE}" in stale
        if cand != (not promote):
            raise SystemExit(f"{store.root}/{new_prefix}: " + (
                f"a candidate generation (`compact -N`): promote it or delete it before compacting {gen} again" if cand else
                f"{len(stale)} objects of an unpublished compaction (not a candidate; maybe one running now): not replacing them with a candidate"))
        log(f"compact {gen}: {len(stale)} objects of an unpublished {'candidate ' if cand else ''}{new}: replaced")
        store.delete(sorted(stale))
    manifest, runs = newest_runs(gen_dir)
    if not runs:
        raise SystemExit(f"{gen}: no runs to compact")
    base_scans = len(json.loads((gen_dir / "scans.json").read_text())["scans"])
    out = work / "store" / new_prefix
    work.mkdir(parents=True, exist_ok=True)
    t0, disk = monotonic(), DiskPeak(work)
    if not promote:
        store.write_text(f"{new_prefix}/{CANDIDATE}", json.dumps({"from": gen}) + "\n")
    log(f"compact {gen} → {new}{'' if promote else ' (a candidate)'} under {as_policy(policy)} ({why}): {len(runs)} runs and the base")
    # the fold's hook: notes on its running phase (at most every `NOTE_S`: a range ends every few seconds), the next phase
    progress = PhaseHook(rec, "fold", note_s=NOTE_S, suffix=lambda: f"; work disk peak {disk.take()} GiB, RSS peak {rss_peak_gb()} GiB")
    progress.begin()
    try:
        src = {"source": store.uri(f"{c.prefix}/{gen}")} if c.source else {}
        summary = {"from": gen, "gen": new, **c.fold(gen_dir, out, work, gen=gen, new_gen=new, runs=runs, threads=threads, mem=mem, jobs=jobs,
                                                      progress=progress, **src)}
    except BaseException as e:
        progress.end(f"failed: {type(e).__name__}: {e}", failed=True)
        raise
    progress.end(f"{fold_note(summary)}; work disk peak {disk.take()} GiB, RSS peak {rss_peak_gb()} GiB")
    comp = {"from": gen, "manifest": manifest, "runs": [r["key"] for r in runs], "base_scans": base_scans,
            "scans": len(json.loads((out / "scans.json").read_text())["scans"]), **compaction_doc(policy, why), **({} if promote else {"candidate": True})}
    if c.md5s:
        comp["files"] = md5s(out)
    (out / COMPACTION).write_text(json.dumps(comp, indent=1) + "\n")
    t1 = monotonic()

    def order(keys: list[str]) -> list[str]:
        return [*c.publish_order([k for k in keys if k != COMPACTION]), *([COMPACTION] if COMPACTION in keys else [])]

    with phase(rec, "upload") as ph:
        tier = (lambda k: None if k == COMPACTION else c.publish_tier(k)) if c.publish_tier else None
        files = upload_new(store, out, new_prefix, {}, order, tier=tier)
        up_s = round(monotonic() - t1, 1)
        ph.note = f"{len(files)} files, {gib(sum(p.stat().st_size for p in out.rglob('*') if p.is_file()))} to {store.root}/{new_prefix}/ in {up_s}s"
    folded = run_scans(runs)

    def landed() -> str | None:
        key, now_runs = store_newest_runs(store, f"{c.prefix}/{gen}")
        return None if run_scans(now_runs) == folded else key

    def superseded(key: str, successor: bool) -> dict:
        log(f"compact {gen} → {new}: {key} landed on {gen} during the fold (it covers scans {new} lacks): not publishing; "
            f"{new} deleted, the next compaction folds it")
        if successor:
            store.delete([f"{c.prefix}/{gen}/{SUCCESSOR}"])
        store.delete(sorted(store.listing(new_prefix)))
        return {**summary, "runs": comp["runs"], "scans": comp["scans"], "superseded": key, "s": round(monotonic() - t0, 1)}

    if promote:
        with phase(rec, "pointer") as ph:
            if key := landed():
                ph.note = f"superseded: {key} landed on {gen} during the fold; {new} deleted, not published"
                return superseded(key, successor=False)
            store.write_text(f"{c.prefix}/{gen}/{SUCCESSOR}", json.dumps({"gen": new}) + "\n")
            if key := landed():
                ph.note = f"superseded: {key} landed on {gen} during the fold; {new} and its successor record deleted, not published"
                return superseded(key, successor=True)
            summary.update(runs=comp["runs"], scans=comp["scans"], why=why, files=len(files), upload_s=up_s,
                           bytes=sum(p.stat().st_size for p in out.rglob("*") if p.is_file()), reads=reads)
            was = current(store, c.prefix)
            summary["current"] = was in (None, gen)
            if summary["current"]:
                set_current(store, c.prefix, new)
            ph.note = f"{gen}/{SUCCESSOR} → {new}; " + (f"{CURRENT} → {new}" if summary["current"] else f"{CURRENT} names {was}: unchanged")
    else:
        summary.update(runs=comp["runs"], scans=comp["scans"], why=why, files=len(files), upload_s=up_s,
                       bytes=sum(p.stat().st_size for p in out.rglob("*") if p.is_file()), reads=reads, current=False, candidate=True)
        with phase(rec, "pointer") as ph:
            ph.note = f"a candidate: no {gen}/{SUCCESSOR}, {CURRENT} unchanged (`promote` moves them)"
    if r2 is not None:
        with phase(rec, "r2") as ph:
            summary["r2"] = r2(new, gen)
            d = summary["r2"]
            ph.note = f"{d.get('copied')} of {d.get('objects')} objects copied ({gib(d.get('bytes'))}) in {d.get('s')}s; the pointer last"
    summary["s"] = round(monotonic() - t0, 1)
    summary["peak"] = {"disk_gb": disk.close(), "rss_gb": rss_peak_gb()}
    log(f"compact {gen} → {new}: {summary['files']} files ({summary['bytes'] / 2**30:.2f} GiB, uploaded in {up_s}s) in {summary['s']}s "
        f"(work disk peak {summary['peak']['disk_gb']} GiB, RSS peak {summary['peak']['rss_gb']} GiB)")
    return summary


#: Objects a generation's R2 copy (`r2_publish_gen`) keeps in flight: thousands of files, ~300 GiB for a compacted
#: static generation, each one streamed GCS → R2 at ~100 MB/s.
R2_WORKERS = 32


def r2_publish_gen(bucket: str, prefix: str, new: str, old: str, *, last: tuple[str, ...] = ("scans.json",), workers: int = R2_WORKERS,
                   promote: bool = True, log: Callable[[str], None] = err) -> dict:
    """A compacted generation to R2 from GCS (`gs://<bucket>/<prefix>/<new>/`, under the same keys): its files, those its
    readers open it by (`last`, generation-relative) after the rest and `compaction.json` after them; checked there; then
    the old generation's `successor.json`; then `<prefix>/CURRENT.json` (as GCS has it), last: an R2 reader following
    the pointer never finds a generation that isn't whole. Raises (`SystemExit`, the pointer not moved) when the check
    fails. `promote` off (a candidate): the generation only; `r2_promote` copies the two pointers later."""
    t0 = monotonic()
    root = f"{prefix}/{new}/"
    tail_names = [*last, COMPACTION]
    objs = r2_objects(bucket, [root])
    body = [o for o in objs if o.key.removeprefix(root) not in tail_names]
    tail = sorted((o for o in objs if o.key.removeprefix(root) in tail_names), key=lambda o: tail_names.index(o.key.removeprefix(root)))
    doc = r2_copy(bucket, body, workers=workers, log=log)
    for o in tail:
        r2_copy(bucket, [o], workers=1, log=log)
    if bad := r2_missing([*body, *tail], workers=workers):
        raise SystemExit(f"r2 {prefix}/{new}: {len(bad)} of {len(objs)} files not on R2 after the copy (e.g. {bad[0]}): pointer not moved")
    if promote:
        r2_promote(bucket, prefix, old, log=log)
    return {"objects": len(objs), "copied": doc["copied"] + len(tail), "bytes": doc["bytes"] + sum(o.size for o in tail), "s": round(monotonic() - t0, 1)}


def r2_promote(bucket: str, prefix: str, old: str, *, log: Callable[[str], None] = err) -> None:
    """The R2 copy's pointers to a generation already on R2: `old`'s `successor.json`, then `<prefix>/CURRENT.json`, as
    the data bucket has them."""
    for key in (f"{prefix}/{old}/{SUCCESSOR}", f"{prefix}/{CURRENT}"):
        r2_copy(bucket, r2_objects(bucket, [key]), workers=1, log=log)


def gen_scans(store, prefix: str) -> list[str]:
    """The scans generation `prefix` holds: its base's (`scans.json`), then its newest manifest's runs'."""
    _, runs = store_newest_runs(store, prefix)
    return [s["id"] for s in store.read_json(f"{prefix}/scans.json")["scans"]] + run_scans(runs)


def promote_gen(store, prefix: str, new: str, *, r2_bucket: str | None = None, log: Callable[[str], None] = err) -> dict:
    """Candidate `new` (`compact_store(promote=False)`) made the successor of the generation it was compacted from and,
    when `CURRENT.json` named that one (or nothing), the current generation; with `r2_bucket` (the data bucket the R2
    copy mirrors), then the R2 copy's pointers (`r2_promote`). Refused, nothing moved: `new` not a whole candidate
    (`candidate.json`, `compaction.json`), its source already compacted, `new` lacking a scan its source holds (append
    those to `new` first), or R2 lacking a file of `new`. A scan appended to the source as its successor record is
    written: the record removed again, refused. Returns `{from, gen, current}`."""
    new_prefix = f"{prefix}/{new}"
    for marker, what in ((CANDIDATE, "not a candidate"), (COMPACTION, "not whole: its compaction didn't finish")):
        if not store.exists(f"{new_prefix}/{marker}"):
            raise SystemExit(f"{store.root}/{new_prefix}: {what} ({marker} missing)")
    old = store.read_json(f"{new_prefix}/{CANDIDATE}")["from"]
    old_prefix = f"{prefix}/{old}"
    if store.exists(f"{old_prefix}/{SUCCESSOR}"):
        raise SystemExit(f"{store.root}/{old_prefix}: already compacted into {store.read_json(f'{old_prefix}/{SUCCESSOR}')['gen']}")

    def lacking() -> list[str]:
        have = set(gen_scans(store, new_prefix))
        return [s for s in gen_scans(store, old_prefix) if s not in have]

    if lack := lacking():
        raise SystemExit(f"{new} lacks {len(lack)} scan{'s' * (len(lack) != 1)} {old} holds ({lack[0]}" + (f" … {lack[-1]}" if len(lack) > 1 else "")
                         + f"): append {'them' if len(lack) > 1 else 'it'} to {new} first")
    if r2_bucket and (bad := r2_missing(r2_objects(r2_bucket, [f"{new_prefix}/"]))):
        raise SystemExit(f"r2 {new_prefix}: {len(bad)} files not on R2 (e.g. {bad[0]}): copy the candidate there first")
    store.write_text(f"{old_prefix}/{SUCCESSOR}", json.dumps({"gen": new}) + "\n")
    if lack := lacking():
        store.delete([f"{old_prefix}/{SUCCESSOR}"])
        raise SystemExit(f"{lack[-1]} landed on {old} as {new} was promoted: its successor record removed; append it to {new}, then promote again")
    was = current(store, prefix)
    moved = was in (None, old)
    if moved:
        set_current(store, prefix, new)
    log(f"promote {new}: {old}/{SUCCESSOR} → {new}; " + (f"{CURRENT} → {new}" if moved else f"{CURRENT} names {was}: unchanged"))
    if r2_bucket:
        r2_promote(r2_bucket, prefix, old, log=log)
        log(f"promote {new}: the R2 copy's pointers moved")
    return {"from": old, "gen": new, "current": moved}


def run_compaction(store, c: Compactor, gen: str, mount: Path, work: Path, *, lease: LeaseFile | None = None, log: Callable[[str], None] = err,
                   **kw) -> dict:
    """`compact_generation` (`kw` its options) holding the compaction lease (`lease`, `compaction_lease`) on the
    generation, followed to its newest successor, from before it is read until the job ends: appends to it wait meanwhile
    (`Runner.wait_compaction`). Raises `Busy` while another live compaction holds the lease."""
    if lease is None:
        return compact_generation(store, c, gen, mount, work, log=log, **kw)
    gen = resolve_gen(store, c.prefix, gen, log)
    with compaction_lease(lease, gen, log=log):
        return compact_generation(store, c, gen, mount, work, log=log, **kw)


def compact_generation(store, c: Compactor, gen: str, mount: Path, work: Path, *, policy: Policy | int | None, force: bool = False,
                       r2_bucket: str | None = None, grace_s: float = GRACE_S, now: float | None = None, threads: int = 4, mem: str = "4GB",
                       jobs: int = 1, read_from: str = "gcs", source=None, promote: bool = True, rec: Recorder | None = None,
                       log: Callable[[str], None] = err) -> dict:
    """The Batch `compact` stage (both stores): generation `gen` of `store` (the data bucket, a `blobstore.Store`)
    followed to its newest successor; when `policy` fires (or `force`, with runs), folded into the next one and published
    (`compact_store`) — GCS first, then, with `r2_bucket` (the data bucket the R2 copy mirrors), R2 (`r2_publish_gen`:
    the new generation, then its pointer last); then (`c.prune`) the predecessors past the grace leave the data bucket.
    `read_from` `gcs`: the generation is read in place from `mount` (`<mount>/<prefix>/<gen>/`, the bucket's mount);
    `r2`: what the fold reads (`c.keep`) is mirrored from the R2 copy (`source`, default `R2Source()`) first, refused
    when it lacks any of it (`read_tree`). Idempotent: a rerun after a compaction finds the successor, which isn't due.
    `promote` off: a candidate (`compact_store`), nothing pruned; `promote_gen` points at it later. Returns `{gen, why,
    compact, predecessors}`.

    `rec`: the run's record on `/scans` (`maint_runs`, kind `compact`): started once the generation is resolved (its
    `scan` the newest scan the generation holds: the newest the fold would fold), phases `read`, `due`, then
    `compact_store`'s, then `prune`; finished `nop` when not due, `failed` (re-raised) on any raise, else
    `succeeded`."""
    from .maint_runs import NULL, gib, phase

    rec = rec or NULL
    gen = resolve_gen(store, c.prefix, gen, log)
    prefix = f"{c.prefix}/{gen}"
    _, newest_listed = store_newest_runs(store, prefix)
    scans = run_scans(newest_listed) or [s["id"] for s in store.read_json(f"{prefix}/scans.json")["scans"]]
    rec.start(scans[-1], prefix)
    try:
        with phase(rec, "read") as ph:
            if read_from == "r2":
                gen_dir = work / "read" / prefix
                reads = read_tree(source or R2Source(), prefix, gen_dir, keep=c.keep, want=store.listing(prefix), label=f"compact {gen}: read", log=log)
            else:
                gen_dir = Path(mount) / prefix
                reads = in_place({k: n for k, n in store.listing(prefix).items() if c.keep(k[len(prefix) + 1:])}, f"{store.root} (mount)",
                                 label=f"compact {gen}: read", log=log)
            ph.note = f"{reads['files']} files, {gib(reads['bytes'])} from {reads['source']}" + (f" in {reads['s']}s" if reads.get("s") is not None else " (in place)")
        with phase(rec, "due") as ph:
            why = forced(gen_dir) if force else due_in(gen_dir, policy)
            _, runs = newest_runs(gen_dir)
            levels = " ".join(f"L{r['level']}" for r in runs) or "no runs"
            ph.note = f"{why} ({len(runs)} runs: {levels})" if why is not None else f"not due under {as_policy(policy)} ({levels})"
        doc: dict = {"gen": gen, "why": why, "compact": None, "predecessors": []}
        if why is None:
            log(f"compact {gen}: not due under {as_policy(policy)}")
            rec.finish("nop")
            return doc
        r2 = None
        if r2_bucket:
            r2 = lambda new, old: r2_publish_gen(r2_bucket, c.prefix, new, old, last=c.last, promote=promote, log=log)  # noqa: E731
        doc["compact"] = compact_store(store, c, gen, gen_dir, work, policy=policy, why=why, threads=threads, mem=mem, jobs=jobs, r2=r2, reads=reads,
                                       promote=promote, rec=rec, log=log)
        if c.prune and promote and not doc["compact"].get("superseded"):
            with phase(rec, "prune") as ph:
                doc["predecessors"] = prune_predecessors(store, c.prefix, doc["compact"]["gen"], now=time.time() if now is None else now, grace_s=grace_s, log=log)
                ph.note = "; ".join(f"{p['gen']}: {p['objects']} objects, {gib(p['bytes'])} " + ("deleted" if p["deleted"] else f"kept ({round(p['age_s'] / 3600)}h old)")
                                    for p in doc["predecessors"]) or "no predecessor holds files"
    except BaseException as e:
        rec.finish("failed", error=f"{type(e).__name__}: {e}")
        raise
    # Superseded: nothing published, and not an error (the guard working): `nop`, as a compaction that wasn't due.
    if sup := doc["compact"].get("superseded"):
        rec.finish("nop", gen=f"{prefix} (superseded: {sup}, not published)")
    else:
        rec.finish("succeeded", gen=f"{prefix} → {doc['compact']['gen']}" + ("" if promote else " (a candidate, not promoted)"))
    return doc


def fold_note(summary: dict) -> str:
    """A fold's summary as its phase's note: scans, rows / versions, sorts, and the static fold's per-part seconds."""
    parts = [f"{summary['scans']} scans" if "scans" in summary else None,
             f"{sum(summary['rows'].values()):,} rows" if isinstance(summary.get("rows"), dict) else None,
             f"{summary['versions']:,} versions" if isinstance(summary.get("versions"), int) else None,
             f"{len(summary['sorts'])} sorts" if isinstance(summary.get("sorts"), (dict, list)) else None,
             ", ".join(f"{k} {v}s" for k, v in summary["stages"].items()) if isinstance(summary.get("stages"), dict) else None]
    return "; ".join(p for p in parts if p)


def publish_r2_chain(bucket: str, prefix: str, gen: str, *, last: tuple[str, ...] = ("scans.json",), log: Callable[[str], None] = err) -> list[dict]:
    """`compact-r2` (the R2 account's job, when the compaction's can't hold the R2 credentials): every compaction from
    `gen` on (its successors on GCS, `gs://<bucket>/<prefix>/`) copied to R2 in turn (`r2_publish_gen`: each new
    generation, then its pointer, last). Idempotent: what R2 holds is skipped."""
    from .blobstore import Store

    gens = chain(Store(f"gs://{bucket}"), prefix, gen)
    return [r2_publish_gen(bucket, prefix, new, old, last=last, log=log) for old, new in zip(gens, gens[1:])]


def publish_r2_candidate(bucket: str, prefix: str, gen: str, *, last: tuple[str, ...] = ("scans.json",), log: Callable[[str], None] = err) -> dict:
    """`compact-r2 -N`: the candidate compacted from `gen` (followed to its newest successor) to R2, without its pointers
    (`r2_publish_gen(promote=False)`); `promote -r` moves those."""
    from .blobstore import Store

    store = Store(f"gs://{bucket}")
    src = chain(store, prefix, gen)[-1]
    new = next_gen(src)
    if not store.exists(f"{prefix}/{new}/{CANDIDATE}"):
        raise SystemExit(f"gs://{bucket}/{prefix}/{new}: no candidate compacted from {src}")
    return r2_publish_gen(bucket, prefix, new, src, last=last, promote=False, log=log)


def compact_options(fn):
    """The Batch `compact` command's options (both stores' CLIs; the store's own `-b`/`-g`/… come with them)."""
    from click import Choice, option

    for opt in reversed([
        option("-C", "--compact", "policy", type=PolicyType(), default=str(COMPACT_LEVEL), help=POLICY_HELP),
        option("-F", "--read-from", type=Choice(READ_FROM), default="gcs", help="Read the generation from the data bucket's mount (gcs) or the R2 copy (r2, mirrored locally; the job's R2 env)"),
        option("-G", "--grace-s", default=GRACE_S, type=int, help=f"Keep a compacted predecessor this many seconds (default {GRACE_S})"),
        option("-j", "--jobs", default=1, type=int, help="Folded at a time: the interval store's key ranges / sorts; the static store's drill-fold chunks"),
        option("-K", "--compact-now", "force", is_flag=True, help="Compact whatever the policy says (when the generation has runs)"),
        option("-m", "--mount", required=True, help="Local mount of the data bucket (the fold reads the generation from it)"),
        option("-M", "--mem", default="100GB", help="DuckDB memory limit"),
        option("-N", "--no-promote", "promote", is_flag=True, default=True, flag_value=False, help="A candidate: the new generation published (and copied to R2 with -r) but nothing pointed at it, the compaction lease not held (appends go on); `promote` points prod at it"),
        option("-p", "--threads", default=16, type=int, help="DuckDB threads"),
        option("-r", "--r2", "to_r2", is_flag=True, help="Then the R2 copy (`r2_publish_gen`: the new generation, its pointer last): the job has the R2 env"),
        option("-T", "--tmp", default="/stage/tmp", help="Work dir (the new generation is written here, then uploaded)"),
    ]):
        fn = opt(fn)
    return fn


# ── The builder's read source (`read_from`, both stores) ───────────────────
#
# Which copy the builder reads a generation's base and runs from in a merge and a compaction (the one-machine `add`s:
# which store it mirrors the generation from). Writes are unchanged (GCS, then the R2 copy). `gcs` (the default) reads
# the data bucket in place (its mount); `r2` mirrors what's read from the R2 copy onto local disk first, refusing when
# the R2 copy lacks a file the data bucket has (it holds the readers' files only, unless the builder's inputs are copied
# there too: specs/static-compaction.md). Each read is logged and returned as `{source, files, bytes, s}`.

READ_FROM = ("gcs", "r2")


class R2Source:
    """The R2 copy as a read source (`listing`, `get`), over the R2 env's boto3 client (`publish.r2_client`)."""

    def __init__(self, client=None, bucket: str | None = None):
        from . import publish as pub

        self.client = client or pub.r2_client()
        self.bucket = bucket or pub.r2_bucket()
        self.root = f"r2://{self.bucket}"

    def listing(self, prefix: str) -> dict[str, int]:
        out = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix.rstrip("/") + "/"):
            out.update({o["Key"]: int(o["Size"]) for o in page.get("Contents", [])})
        return out

    def get(self, key: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(dst))


def read_tree(src, prefix: str, dst: Path, *, keep: Callable[[str], bool] = lambda rel: True, want: dict[str, int] | None = None,
              workers: int = 8, label: str = "read", log: Callable[[str], None] = err) -> dict:
    """`prefix`'s files (those `keep(rel)` takes) from `src` (a `blobstore.Store` or `R2Source`) into `dst` (`dst/<rel>`;
    a file already there with the same size is kept). `want`: the data bucket's listing of the same keys: refused
    (`SystemExit`) when `src` lacks one of them or holds it with another size. Returns `{source, files, bytes, s}`."""
    t0 = monotonic()
    have = src.listing(prefix)
    rel = lambda k: k[len(prefix.rstrip("/")) + 1:]  # noqa: E731
    if want is not None:
        bad = sorted(k for k, n in want.items() if keep(rel(k)) and have.get(k) != n)
        if bad:
            raise SystemExit(f"{label}: {src.root} lacks {len(bad)} of the data bucket's files under {prefix}/ (e.g. {bad[0]}): "
                             "it holds the readers' files only; read from gcs")
    todo = [(k, dst / rel(k)) for k, n in sorted(have.items()) if keep(rel(k)) and not ((dst / rel(k)).is_file() and (dst / rel(k)).stat().st_size == n)]
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda kd: src.get(*kd), todo))
    doc: dict[str, Any] = {"source": src.root, "files": sum(1 for k in have if keep(rel(k))), "bytes": sum(n for k, n in have.items() if keep(rel(k))),
           "fetched": sum(have[k] for k, _ in todo), "s": round(monotonic() - t0, 1)}
    log(f"{label}: {doc['files']} files, {doc['bytes'] / 2**30:.3f} GiB from {doc['source']} ({doc['fetched'] / 2**30:.3f} GiB fetched) in {doc['s']}s")
    return doc


def in_place(listing: dict[str, int], source: str, label: str = "read", log: Callable[[str], None] = err) -> dict:
    """A read in place (the bucket's mount): its bytes, no separate seconds (they're the stage's)."""
    doc: dict[str, Any] = {"source": source, "files": len(listing), "bytes": sum(listing.values()), "fetched": 0, "s": None}
    log(f"{label}: {doc['files']} files, {doc['bytes'] / 2**30:.3f} GiB read in place from {source}")
    return doc


def read_from_arg(p: Any) -> list[str]:
    """A merge / compaction job's `-F` (the profile's `read_from`): none for `gcs` (the default)."""
    rf = getattr(p, "read_from", "gcs")
    return [] if rf == "gcs" else ["-F", rf]


def read_runs(store, keys: list[str], mount: Path, dst: Path, *, source: tuple[Any, str] | None = None, log: Callable[[str], None] = err) -> tuple[Path, dict]:
    """A merge's input runs (`keys`, generation-relative) where its build reads them: in place on `mount` (the data
    bucket's mount of the generation; `source` None: their bytes logged from `store`'s listing), or mirrored into `dst`
    from `source` = `(src, gen_prefix)` (the R2 copy, `R2Source`, and the generation's key prefix there), refused when
    the copy lacks a file `store` (the data bucket) lists. Returns the dir the runs are under and `{source, files, bytes,
    fetched, s}`."""
    listing = {f"{k}/{rel}": n for k in keys for rel, n in ((x.removeprefix(f"{k}/"), n) for x, n in store.listing(f"{k}/").items())}
    if source is None:
        return Path(mount), in_place(listing, f"{getattr(store, 'bucket', None) or getattr(store, 'root', '?')} (mount)", label="merge: read", log=log)
    src, prefix = source
    docs = [read_tree(src, f"{prefix}/{k}", dst / k, want={f"{prefix}/{x}": n for x, n in listing.items() if x.startswith(f"{k}/")},
                      label=f"merge: read {k}", log=log) for k in keys]
    return dst, {"source": src.root, "files": sum(d["files"] for d in docs), "bytes": sum(d["bytes"] for d in docs),
                 "fetched": sum(d["fetched"] for d in docs), "s": round(sum(d["s"] for d in docs), 1)}
