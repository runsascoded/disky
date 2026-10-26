"""A scan's steps (specs/pipelines-everywhere.md §2.1): `dt-cloud scan step <name> [arg]`, one idempotent function per
step, each run as its own task by `job/nf/scan.nf` (or by hand on a machine holding the data store's mount).

Every step reads two things:
- the deployment's profile (`scan_profile.ScanProfile`, from `$DT_SCAN_PROFILE_JSON`, which the head sets, or
  `$DT_SCAN_PROFILE`, a file);
- the scan's identity, stamped once by the head and never re-derived in a task: `SNAP_ID` (also exported as
  `SCAN_ID`, which the scan-run wrapper reads), `GEN`, `DT_JOB_START`, and `DT_RUN_ID` (the head's run, which every
  task records into).

Adding a step (gcs's access-ingest, rules, healthcheck, … are steps like any other): copy one below.

    @step("access-ingest", core=False, stage="pre", soft=True, options={"glob": str})
    def access_ingest(ctx: Ctx, arg: str | None) -> None:
        if ctx.done(ctx.data_root / "access" / ctx.scan / "_SUCCESS"):   # skip-if-done: its own outputs
            return
        with ctx.phase("access-ingest"):                                 # a `scan_runs` phase, begin → end
            ctx.sh(["dt-cloud", "access", "ingest", "-g", ctx.opt("access-ingest", "glob")], fatal=False,
                   warn="access ingest failed (the index runs without it)")

- `core`: on for every deployment unless the profile `skip`s it (`skippable`); a non-core step runs only where the
  profile's `steps` lists it.
- `stage`: where `scan.nf` runs it. `pre` (beside `list`, before `index`), `post` (after `index-sync`, in the post
  task, in registry order). The core's own stages (`list`, `index`, `serve`, `sync`) are wired in `scan.nf` itself.
- `soft`: the step never fails the run (an unexpected error is a warning, exit 0), so a dependent step always runs:
  the DAG's "soft dependency" (index waits for access-ingest, but runs without its output).
- `options`: the keys the step takes under the profile's `options.<step>`, with their types (unknown keys refused).
- `requires(profile)`: profile-level checks, returned as problems (validated with the profile, before any task).
- Credentials: every step's call runs under `dt-cloud secrets exec` with the profile's `secrets`, overlaid by its own
  `options.<step>.secrets` (and a list call's by its source's `secrets`): `ScanProfile.secrets_for`. Nothing to declare.
- Stores: a profile's `store` (a `STORES_JSON` key, default `primary`) is the site store its scans feed; a step whose
  command files rows per store passes `store_args(ctx)` (`-s <store>`, nothing for the primary). A step that can't
  serve another store refuses it in `requires` (`primary_only`).
- Idempotent: a step skips work whose outputs exist (`ctx.done(path)`), or calls a command that is (publish-r2's
  size + md5, the static-names / interval-store appends after the newest scan they hold). It does not trust
  Nextflow's cache: a cached `done` marker can point at a lost output, so each step also checks its inputs
  (`ctx.need(path)`), and `scan head -F` reruns every task with the cache off (each then skips what's done).

Tests and DAG checks: `DT_SCAN_STEP_FAKE=<file>` makes every step append `<name> <arg>` to the file and return;
`DT_SCAN_STEP_FAIL=<name>` makes that step exit 1 (its marker unwritten).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from disk_tree.env import name as env_name

from .heartbeat import Tail, run_teed, set_stage
from .index_parts import PART_JSON, Part, part_ids, split_parts
from .scan_profile import ScanProfile


def err(*a: object) -> None:
    print(*a, file=sys.stderr)

PROFILE_JSON_ENV = "DT_SCAN_PROFILE_JSON"
PROFILE_PATH_ENV = "DT_SCAN_PROFILE"
DATA_MOUNT_ENV = "DT_DATA_MOUNT"
FAKE_ENV = "DT_SCAN_STEP_FAKE"
FAIL_ENV = "DT_SCAN_STEP_FAIL"
IDENTITY = ("SNAP_ID", "GEN", "DT_JOB_START")
#: Credentials whose absence turns a serving step into a logged skip (the gate; the values never reach argv or logs).
CF = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
R2 = ("R2_ENDPOINT", "R2_BUCKET")


class StepFailed(Exception):
    """A step's failure, its exit code `rc`. `hard`: fails the run even from a soft step (a barrier the rest of the
    DAG must not pass, e.g. publish-r2's `barrier`)."""

    def __init__(self, rc: int, what: str, hard: bool = False):
        self.rc = rc
        self.hard = hard
        super().__init__(what)


@dataclass(frozen=True)
class Step:
    name: str
    fn: Callable[[Ctx, str | None], None]
    core: bool = True
    skippable: bool = True
    stage: str = "post"
    soft: bool = False
    arg: bool = False
    options: Mapping[str, type] = field(default_factory=dict)
    check: Callable[[ScanProfile], list[str]] | None = None

    def requires(self, profile: ScanProfile) -> list[str]:
        return self.check(profile) if self.check else []


#: Every step, in DAG order (the profile's `enabled()` and the post task keep it).
STEPS: dict[str, Step] = {}


def step(name: str, *, core: bool = True, skippable: bool = True, stage: str = "post", soft: bool = False,
         arg: bool = False, options: Mapping[str, type] | None = None,
         requires: Callable[[ScanProfile], list[str]] | None = None):
    def deco(fn: Callable[[Ctx, str | None], None]) -> Callable[[Ctx, str | None], None]:
        STEPS[name] = Step(name, fn, core, skippable, stage, soft, arg, options or {}, requires)
        return fn
    return deco


class _Printer:
    """The phase lines without a record (no `$SCAN_RUNS_PROFILE`: a manual or test run)."""

    def __init__(self, env: Mapping[str, str]):
        self.env = env

    def _clock(self) -> int:
        import time

        v = (self.env.get("DT_JOB_START") or "").strip()
        return int(time.time()) - int(v) if v.isdigit() else 0

    def begin(self, name: str, began: int | None = None, gen: str | None = None) -> None:
        from .scan_runs import phase_line

        err(phase_line(name, self._clock(), start=True))

    def end(self, name: str, rc: int = 0, warn: bool = False, msg: str | None = None, resume: str | None = None,
            note: str | None = None, outputs: Sequence[str] = (), measure: bool = True, timed_out: str | None = None,
            terminated: int | None = None, at: int | None = None) -> int:
        from .scan_runs import phase_line

        if rc:
            note = f"failed (exit {rc})" + (f", {note}" if note else "")
        err(phase_line(name, self._clock(), note))
        return 0 if warn else rc

    def alert(self, level: str, message: str) -> None:
        err(f"alert ({level}, no run recorded): {message}")

    def phases(self) -> list[dict]:
        return []


def cpu_quota(cgroup: Path) -> int | None:
    """The CPUs a cgroup's quota allows, rounded up (v2 `cpu.max`, else v1 `cpu.cfs_quota_us` / `cpu.cfs_period_us`);
    None without one. A container's affinity can list the host's CPUs (Fargate: 4 on a 1-vCPU task), and threads past
    the quota only add contention."""
    try:
        quota, period = (cgroup / "cpu.max").read_text().split()
        return None if quota == "max" else -(-int(quota) // int(period))
    except (OSError, ValueError):
        pass
    try:
        q, p = int((cgroup / "cpu/cpu.cfs_quota_us").read_text()), int((cgroup / "cpu/cpu.cfs_period_us").read_text())
    except (OSError, ValueError):
        return None
    return -(-q // p) if q > 0 else None


#: The share of the task's memory `{mem}` names: room for a DuckDB process's RSS above its `memory_limit` (some tens of
#: percent) and whatever else the task runs beside it.
MEM_SHARE = 0.6


def mem_budget(cgroup: Path, share: float = MEM_SHARE) -> str:
    """`share` of the task's memory as a DuckDB size (`"38GB"`): the machine's RAM, or the cgroup's limit when lower
    (v2 `memory.max`, v1 `memory.limit_in_bytes`; v1's unlimited is ≥ 2^60)."""
    total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    for f in ("memory.max", "memory/memory.limit_in_bytes"):
        try:
            limit = int((cgroup / f).read_text())
        except (OSError, ValueError):  # absent, or v2's "max"
            continue
        if limit < 2**60:
            total = min(total, limit)
    return f"{max(1, int(total * share / 2**30))}GB"


@dataclass
class Ctx:
    """One step's view: the profile, the scan's identity, its environment (also every command's), a local scratch dir,
    and the run its phases record into."""
    profile: ScanProfile
    scan: str
    gen: str
    env: dict[str, str]
    work: Path
    run: object
    #: How `sh` runs a command (`subprocess.run`'s shape; tests pass a fake). The default, `run_teed`, also feeds the
    #: open phase's heartbeat `tail` its output.
    sh_impl: Callable[..., subprocess.CompletedProcess] = run_teed
    #: The open phase's first nonfatal command failure (exit code, warning): the phase ends failed with a warn alert
    failure: tuple[int, str] | None = None
    #: The open phase's heartbeat line source (`beating`); None outside a phase or without a recorded run
    tail: Tail | None = None
    #: The last command `sh` ran (its exit code; its stdout when captured, a failed one's too)
    last: subprocess.CompletedProcess | None = None
    #: The open phase's explicit skip (`skip`): its end's note says `skipped (<kind>): <reason>`
    skipped: str | None = None
    #: The open phase's end note, set by its body (e.g. the probe's verdict); None: `phase`'s own `note`
    note: str | None = None

    # layout

    @property
    def date(self) -> str:
        return self.scan.split("T")[0]

    @property
    def data_uri(self) -> str:
        return self.profile.data.rstrip("/")

    @property
    def data_root(self) -> Path:
        """The data store as a local path: a cloud URL under its FUSE mount (`$DT_DATA_MOUNT/<bucket>/<prefix>`; a
        google-batch task's `/mnt/disks`), a local dir as is."""
        u = urlparse(self.profile.data)
        if not u.scheme:
            return Path(self.profile.data)
        mount = self.env.get(DATA_MOUNT_ENV)
        if not mount:
            raise StepFailed(2, f"data {self.profile.data} needs ${DATA_MOUNT_ENV}: the root its bucket is mounted under (/mnt/disks in a google-batch task)")
        return Path(mount) / u.netloc / u.path.lstrip("/")

    @property
    def layer2(self) -> str:
        return self.profile.layout.layer2.format(scan=self.scan)

    @property
    def index_key(self) -> str:
        return f"{self.layer2}index/{self.gen}"

    @property
    def snapshots(self) -> str:
        return self.profile.layout.snapshots_dir()

    def l2_path(self, bucket: str) -> Path:
        return self.data_root / self.layer2 / f"{bucket}.parquet"

    def listing_dir(self, bucket: str) -> Path:
        """A source's listing: local scratch when `list` imports it at once, else the data store (`layout.listing`),
        where `index` reads it."""
        if self.opt("list", "import", False):
            return self.work / "listing" / bucket
        return self.data_root / self.listing_key(bucket)

    def listing_key(self, bucket: str, scan: str | None = None) -> str:
        """A source's listing dir under the data store (`layout.listing`), for this scan or another."""
        return f"{self.profile.layout.listing.format(scan=scan or self.scan)}{bucket}"

    def listing_out(self, bucket: str) -> str | Path:
        """Where `bulk-list` writes a source's listing: the data store's URL when it is one and `index` reads the
        listing from it (the lister writes shards to `gs://` directly, not through the task's FUSE mount), else
        `listing_dir`."""
        if not self.opt("list", "import", False) and urlparse(self.profile.data).scheme:
            return f"{self.data_uri}/{self.listing_key(bucket)}"
        return self.listing_dir(bucket)

    def prior_listing(self, bucket: str) -> Path | None:
        """The newest completed listing of `bucket` from an earlier scan (`<layout.listing>` with any scan id, a
        `_SUCCESS.json` beside its shards): `bulk-list -W`'s chunk weights, so no worker draws a 100M-object prefix
        alone and straggles for hours."""
        tmpl = self.listing_key(bucket, "{scan}")
        at = tmpl.split("/").index("{scan}")
        done = sorted(((m.parent.relative_to(self.data_root).parts[at], m.parent)
                       for m in self.data_root.glob(f"{tmpl.replace('{scan}', '*')}/_SUCCESS.json")), reverse=True)
        return next((d for scan, d in done if scan < self.scan), None)

    # settings

    def opt(self, step: str, key: str, default: object = None) -> object:
        return self.profile.opts(step).get(key, default)

    def has(self, *names: str) -> bool:
        return all((self.env.get(n) or "").strip() for n in names)

    @property
    def reproc(self) -> bool:
        return self.env.get("REPROC", "0") == "1"

    @property
    def nproc(self) -> int:
        cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
        quota = cpu_quota(Path(self.env.get("DT_CGROUP") or "/sys/fs/cgroup"))
        return min(cpus, quota) if quota else cpus

    @property
    def mem(self) -> str:
        return mem_budget(Path(self.env.get("DT_CGROUP") or "/sys/fs/cgroup"))

    def fill_env(self, template: str) -> str:
        """`template` with each `{env:NAME}` filled from the task's env (a step fails on an unset one)."""
        def var(m: re.Match[str]) -> str:
            v = (self.env.get(m[1]) or "").strip()
            if not v:
                raise StepFailed(2, f"{template}: ${m[1]} unset")
            return v
        return re.sub(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", var, template)

    def expand(self, args: Sequence[str], **extra: object) -> list[str]:
        """`args` with `{nproc}`, `{mem}` (`mem_budget`: a DuckDB `memory_limit` sized to the task's machine), `{scan}`,
        `{date}`, `{gen}`, `{work}`, `{data}` (the data store's local root, `data_root`) (+ `extra`) filled; an arg that
        is exactly `{<key>}` of a list value expands to its items."""
        vals: dict[str, object] = dict(nproc=self.nproc, scan=self.scan, date=self.date, gen=self.gen, work=self.work, **extra)
        if any("{mem}" in a for a in args):
            vals["mem"] = self.mem
        if any("{data}" in a for a in args):
            vals["data"] = self.data_root
        out: list[str] = []
        for a in args:
            k = a[1:-1] if a.startswith("{") and a.endswith("}") else None
            if k in vals and isinstance(vals[k], list):
                out.extend(str(x) for x in vals[k])
            else:
                out.append(a.format(**vals))
        return out

    # running

    def sh(self, argv: Sequence[str], *, fatal: bool = True, warn: str | None = None, env: Mapping[str, str] | None = None,
           capture: bool = False, allowed_codes: tuple[int, ...] = ()) -> str | None:
        """Run `argv` (logged as `+ …`; secrets only ever in the env). A failure raises `StepFailed` when `fatal`, else
        prints `WARN: <warn>` and returns None; returns stdout when `capture`, else "". A nonfatal failure still ends
        the open phase failed (warn-alerted, never failing the run), so /scans doesn't show it done. `allowed_codes`
        accepts only documented expected nonzero results, such as exit 1 for attribution-rule findings."""
        argv = [str(a) for a in argv]
        err("+ " + " ".join(argv))
        teed = {"tail": self.tail} if self.tail is not None and self.sh_impl is run_teed else {}
        r = self.last = self.sh_impl(argv, env={**self.env, **(env or {})}, stdout=subprocess.PIPE if capture else None, text=True, **teed)
        if r.returncode and r.returncode not in allowed_codes:
            if fatal:
                raise StepFailed(r.returncode, f"exit {r.returncode}: {' '.join(argv[:3])}")
            what = warn or " ".join(argv[:3]) + " failed"
            err(f"WARN: {what} (exit {r.returncode})")
            if self.failure is None:
                self.failure = (r.returncode, what)
            return None
        return r.stdout if capture else ""

    @contextmanager
    def phase(self, name: str, note: str | None = None) -> Iterator[None]:
        """A `scan_runs` phase: begun, then ended (failed, with the exit code, when the body raises; failed with a warn
        alert after a nonfatal command's failure; done, with its reason in the note and a warn alert, after `skip`)."""
        self.run.begin(name)
        set_stage(name)
        self.failure = self.skipped = self.note = None
        try:
            with self.beating(name):
                yield
        except StepFailed as e:
            self.run.end(name, e.rc or 1)
            raise
        except BaseException:
            self.run.end(name, 1)
            raise
        finally:
            failure, skipped, set_note = self.failure, self.skipped, self.note
            self.failure = self.skipped = self.note = None
        note = set_note or note
        if failure:
            self.run.end(name, failure[0], warn=True, msg=failure[1], note=note)
        elif skipped:
            self.run.end(name, 0, note=f"{note}, {skipped}" if note else skipped)
            self.run.alert("warn", f"{name} {skipped}")
        else:
            self.run.end(name, 0, note=note)

    def skip(self, kind: str, reason: str) -> None:
        """The open phase's work was skipped, for `reason`: it ends done, its note's `skipped (<kind>): <reason>`
        (`scan_runs.phase_skip`) making the run `partial` on /scans with that reason, and a warn alert names it."""
        self.skipped = f"skipped ({kind}): {reason}"

    def recorded_skip(self, phase: str) -> str | None:
        """`<phase> skipped (<kind>): <reason>` when this run's record says `phase`'s latest closed attempt was skipped
        (`scan_runs.phase_skip`), else None: never guessed, only read from the record (a running phase's note is a
        heartbeat's output line, not its verdict)."""
        from .scan_runs import phase_skip

        read = getattr(self.run, "phases", None)  # a recorder that can't read the record: nothing recorded
        rows = [p for p in (read() if read else []) if p["phase"] == phase and p.get("status") != "running"]
        clause = phase_skip(max(rows, key=lambda p: p["seq"])["note"]) if rows else None
        return f"{phase} {clause}" if clause else None

    @contextmanager
    def beating(self, name: str) -> Iterator[Tail | None]:
        """Phase `name`'s heartbeat around the body (`JobRun.heartbeat`): every command `sh` runs meanwhile feeds its
        note. Stopped when the body exits, before the phase's end is written. A recorder without a heartbeat
        (`_Printer`: no run recorded) runs the body as is."""
        heartbeat = getattr(self.run, "heartbeat", None)
        if heartbeat is None:
            yield None
            return
        with heartbeat(name) as tail:
            prev, self.tail = self.tail, tail
            try:
                yield tail
            finally:
                self.tail = prev

    def done(self, path: Path, what: str | None = None) -> bool:
        """Whether `path` exists; logs the skip when it does."""
        if path.exists():
            err(f"{what or 'output'} exists, skipping: {path}")
            return True
        return False

    @contextmanager
    def creds(self) -> Iterator[None]:
        """The step's credentials (`AWS_*` from `ctx.env`: `r2_profile`'s `AWS_SHARED_CREDENTIALS_FILE`, a source's keys)
        in `os.environ` for an in-process store read, as `ctx.sh` gives them to a subprocess: else boto, finding the
        `profile: r2` that `buckets.yml` names, looks in a credentials file that lacks it (r2, 2026-10-17T0311)."""
        keys = [k for k in self.env if k.startswith("AWS_")]
        saved = {k: os.environ.get(k) for k in keys}
        os.environ.update({k: self.env[k] for k in keys})
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def store_has(self, root: str, key: str) -> bool:
        """Whether the store at `root` (a dir or URL, `blobstore.Store`) holds `key`, read with the step's credentials."""
        from .blobstore import Store

        with self.creds():
            return Store(root).exists(key)

    def need(self, path: Path, step: str) -> Path:
        if not path.exists():
            raise StepFailed(1, f"missing input {path} (from `{step}`): a cached task's output was lost; rerun the head with -F")
        return path


def store_args(ctx: Ctx) -> list[str]:
    """`-s <store>` for a command that files rows per site store (`index-sync`, `index-gc`, `over-time-groups`); nothing
    for the primary, so its argv is what it always was."""
    return [] if ctx.profile.primary else ["-s", ctx.profile.store]


def primary_only(step: str, why: str) -> Callable[[ScanProfile], list[str]]:
    """A `requires` check: `step` serves only the primary store (`why`), so another store's profile must skip it."""
    def check(p: ScanProfile) -> list[str]:
        return [] if p.primary else [f"store {p.store}: skip {step} ({why})"]
    return check


def copy_atomic(src: Path, dst: Path) -> None:
    """`src` → `dst` through a sibling temp name, so `dst` exists only whole (a skip check can trust it)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


# The steps. Phase names are the record's (`job/scan-runs.json` ties outputs to `layer-2`, `index-write`, `publish`).


@step("bucket-check", stage="pre", soft=True)
def bucket_check(ctx: Ctx, arg: str | None) -> None:
    """Every bucket each S3 endpoint's key can list is scanned or empty: a non-empty one outside the sources (a new
    zone's bucket) is billed storage the site would miss, so it alerts every run until added. Never fatal."""
    by_endpoint: dict[str | None, set[str]] = {}
    for s in ctx.profile.sources:
        if s.kind == "s3":
            by_endpoint.setdefault(s.endpoint, set()).add(s.bucket)
    if not by_endpoint:
        err("bucket-check: no s3 sources")
        return
    found: list[str] = []
    with ctx.phase("bucket-check"):
        for endpoint, scanned in by_endpoint.items():
            found += unscanned(s3_client(endpoint), scanned)
        if found:
            from .scan_runs import scan_alert_links

            text = " ".join(found)
            message = f"unscanned-bucket check: {text}"
            err(f"WARN: {message}")
            ctx.failure = (1, message)
            links = scan_alert_links(ctx.env.get("SITE_URL"), getattr(ctx.run, "run_id", None), ctx.profile.store)
            link_args = [arg for label, url in links for arg in ("-L", label, url)]
            ctx.sh(["disk-tree", "notify", "-t", f"⚠️ {ctx.profile.deployment} {ctx.scan}", *link_args,
                    f"non-empty bucket(s) not in the profile's sources: `{text}`"], fatal=False, warn="unscanned-bucket alert not sent")


@step("access-ingest", core=False, stage="pre", soft=True, options={"args": list, "poller": bool})
def access_ingest(ctx: Ctx, arg: str | None) -> None:
    """The buckets' access logs → the read-recency shards (`dt-cloud access ingest`, incremental and watermarked: its
    settings in the env), beside `list`. Soft: `index` runs without the newest shards, and the next run catches up.

    `options.access-ingest.poller: true`: the deployment runs `access ingest` as its own scheduled job (the hourly
    poller, specs/access-log-freshness.md §A), so the scan leaves this step out (`ScanProfile.enabled`) and `index`
    reads `access/agg` as it stands, its `meta.access.through` saying how far that reaches."""
    if ctx.reproc:
        err("access-ingest off under REPROC")
        return
    with ctx.phase("access-ingest"):
        ctx.sh(["dt-cloud", "access", "ingest", *ctx.opt("access-ingest", "args", [])], fatal=False,
               warn="access ingest failed (read recency lags until the next run's ingest)")


#: Where `access-r2` keeps the read aggregate in the data store (`options.index.access` names it).
ACCESS_R2_KEY = "access/r2/agg.parquet"


def _access_r2_check(p: ScanProfile) -> list[str]:
    if p.opts("access-r2").get("state"):
        return []
    return ["options.access-r2.state: required (the collector archive's `state.json`, e.g. `r2://<archive>/state.json`)"]


@step("access-r2", core=False, stage="pre", soft=True, options={"state": str, "out": str, "endpoint": str}, requires=_access_r2_check)
def access_r2(ctx: Ctx, arg: str | None) -> None:
    """The R2 access collector's read aggregates → one layer-2a parquet for `index` (`options.index.access`), beside
    `list`: `dt-cloud access r2-agg -s <state>`, every hour the collector's `state.json` names, cut at the scan's
    instant. Cumulative (all reads before the scan), so it replaces the data store's one key (`out`, default
    `access/r2/agg.parquet`) rather than adding a shard. No reads yet, or a failure: the earlier aggregate stays (valid as
    of its scan) and `index` runs with it. The collector archive's key goes in `options.access-r2.secrets`, its endpoint
    in `endpoint` (default `$R2_ENDPOINT`), set for this one command only."""
    if ctx.reproc:
        err("access-r2 off under REPROC")
        return
    local = ctx.work / "access-r2" / "agg.parquet"
    endpoint = ctx.opt("access-r2", "endpoint") or ctx.env.get("R2_ENDPOINT")
    env = {"DISK_TREE_R2_ENDPOINT_URL": str(endpoint)} if endpoint else {}
    with ctx.phase("access-r2"):
        ran = ctx.sh(["dt-cloud", "access", "r2-agg", "-d", ctx.scan, "-o", local, "-s", ctx.opt("access-r2", "state")], fatal=False,
                     env=env, warn="r2 read aggregate failed (the index reads the previous one)")
        if ran is None or not local.exists():
            return
        key = ctx.data_root / str(ctx.opt("access-r2", "out", ACCESS_R2_KEY))
        key.parent.mkdir(parents=True, exist_ok=True)
        tmp = key.with_name(key.name + ".tmp")
        shutil.copyfile(local, tmp)
        tmp.replace(key)


def s3_client(endpoint: str | None):
    """A virtual-hosted client (CAIOS rejects path-style object calls, `PathStyleRequestNotAllowed`)."""
    import boto3
    from botocore.config import Config

    return boto3.client("s3", endpoint_url=endpoint, config=Config(s3={"addressing_style": "virtual"}))


def unscanned(client, scanned: set[str]) -> list[str]:
    """The non-empty buckets `client` lists that aren't in `scanned`; a failed check is an entry naming the call."""
    try:
        names = sorted(b["Name"] for b in client.list_buckets()["Buckets"])
    except Exception as e:  # noqa: BLE001 — reported in the alert, never fatal
        return [f"check failed: ListBuckets: {type(e).__name__}: {e}"]
    out = []
    for n in names:
        if n in scanned:
            continue
        try:
            if client.list_objects_v2(Bucket=n, MaxKeys=1).get("KeyCount"):
                out.append(n)
        except Exception as e:  # noqa: BLE001
            out.append(f"{n} (check failed: ListObjectsV2: {type(e).__name__}: {e})")
    return out


def _list_check(p: ScanProfile) -> list[str]:
    if not p.opts("list").get("import") and p.layout.listing is None:
        return ["layout.listing: required unless options.list.import is true (index reads the listings from the data store)"]
    return []


def _raise_nofile() -> None:
    """The lister opens a socket per concurrent range; a 1024 soft limit surfaces as connection resets hours in."""
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    for n in (hard, 65536):
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (n, hard))
            return
        except (ValueError, OSError):
            continue


DEFAULT_IMPORT_ARGS = ["-e", "stream", "-j", "{nproc}", "-m"]


def _import(ctx: Ctx, bucket: str, kind: str, listing: Path) -> None:
    """One bucket's listing → its layer-2 in the data store (`<layer2>/<bucket>.parquet`, written whole)."""
    root = ctx.work / "l2" / bucket
    args = ctx.expand(ctx.opt("list", "import_args", DEFAULT_IMPORT_ARGS))
    ctx.sh(["disk-tree", "import", *args, "-s", kind, "-t", f"{ctx.date}T00:00:00+00:00",
            "-l", f"{listing}/shard-*.parquet", "-b", bucket], env={env_name("ROOT"): str(root)})
    blobs = sorted((root / "scans").glob("*.parquet"))
    if not blobs:
        raise StepFailed(1, f"import wrote no layer-2 under {root}/scans")
    copy_atomic(blobs[0], ctx.l2_path(bucket))


@step("list", skippable=False, stage="list", arg=True, requires=_list_check,
      options={"import": bool, "bulk_args": list, "import_args": list, "threads": int, "weights": bool})
def list_source(ctx: Ctx, uri: str | None) -> None:
    """One source's listing (`disk-tree bulk-list`, `-x clear`: a retried task never merges a half-finished attempt's
    shards), and with `options.list.import` its layer-2 at once (the per-source machine imports what it listed; the
    layer-2 goes to the data store, the canonical input every later one-shot reads). Skips when its output exists.
    `options.list.weights`: chunk weights (`-W`) from the source's newest earlier listing in the data store."""
    if not uri:
        raise StepFailed(2, "list needs the source URI")
    src = ctx.profile.source(uri)
    b = src.bucket
    imports = bool(ctx.opt("list", "import", False))
    listing = ctx.listing_dir(b)
    if imports and ctx.done(ctx.l2_path(b), "layer-2"):
        return
    if not imports and ctx.done(listing / "_SUCCESS.json", "listing"):
        return
    # an `r2://` source without its own endpoint is the deployment's R2 (`$R2_ENDPOINT`, a secret where the profile is public)
    endpoint = src.endpoint or (ctx.env.get("R2_ENDPOINT") if src.kind == "r2" else None)
    if src.listing:
        url = ctx.fill_env(src.listing)
        with ctx.phase(f"list {b}"):
            ctx.sh(["dt-cloud", "fetch-listing", "-o", listing, url], env={env_name("R2_ENDPOINT_URL"): endpoint} if endpoint else None)
        return
    _raise_nofile()
    threads = src.threads or ctx.opt("list", "threads")
    weights: list[str] = []
    if ctx.opt("list", "weights", False):
        prior = ctx.prior_listing(b)
        if prior is None:
            err(f"WARN: no earlier completed listing of {b}: listing with unweighted chunks")
        else:
            weights = ["-W", f"{prior}/*.parquet"]
    with ctx.phase(f"list {b}"):
        ctx.sh(["disk-tree", "bulk-list", *ctx.opt("list", "bulk_args", []), uri,
                *(["-E", endpoint] if endpoint else []), "-o", ctx.listing_out(b),
                # the Hub lister is one rate-limited process (`-w` requests in flight); it refuses `-P`
                *(["-P", src.procs or ctx.nproc] if src.kind != "hf" else []),
                *(["-w", threads] if threads else []), "-x", "clear", *weights, *src.args])
    if imports:
        with ctx.phase(f"import {b}"):
            _import(ctx, b, src.kind, listing)


def _index_check(p: ScanProfile) -> list[str]:
    o = p.opts("index")
    mode = o.get("mode", "layer2")
    if mode not in ("layer2", "path-index"):
        return [f"options.index.mode: layer2 or path-index (got {mode!r})"]
    if mode == "path-index":
        problems = [f"options.index.{k}: only with mode layer2" for k in ("webdata", "threads") if k in o]
        if p.opts("list").get("import"):
            problems.append("options.list.import: path-index reads the listings themselves (no per-source layer-2)")
        if urlparse(p.data).scheme not in ("gs", "gcs", ""):
            problems.append("data: a gs:// bucket or a local dir with options.index.mode path-index (its inputs are staged from GCS or read in place)")
        if "parts" in o:
            buckets = {s.bucket for s in p.sources}
            for b, bounds in o["parts"].items():
                if b not in buckets:
                    problems.append(f"options.index.parts.{b}: not one of the sources' buckets")
                elif bounds is not None and not (isinstance(bounds, list) and all(isinstance(x, str) for x in bounds)):
                    problems.append(f"options.index.parts.{b}: a list of split bounds (first key segments)")
                else:
                    try:
                        split_parts(b, bounds or [])
                    except ValueError as e:
                        problems.append(f"options.index.parts.{b}: {e}")
        return problems
    return [f"options.index.{k}: only with mode path-index" + (" (index-write writes the age pyramids itself)" if k == "age_pyramids" else "")
            for k in ("attribution", "access", "rules", "age_pyramids", "verify", "annotate", "parts") if k in o]


def index_parts(profile: ScanProfile) -> list[tuple[str, Part]]:
    """The sharded index's parts (`options.index.parts`, path-index mode), `(id, part)` in source order: every source's
    bucket one part, or, where `parts` lists split bounds for it, one per key range (`index_parts.split_parts`; ids
    `<bucket>~<i>`). None when `parts` is unset: the one-pass index."""
    o = profile.opts("index")
    if o.get("mode") != "path-index" or "parts" not in o:
        return []
    out: list[tuple[str, Part]] = []
    for s in profile.sources:
        ps = split_parts(s.bucket, (o["parts"] or {}).get(s.bucket) or [])
        out += zip(part_ids(s.bucket, len(ps)), ps)
    return out


@step("index", skippable=False, stage="index", requires=_index_check,
      options={"mode": str, "webdata": list, "lifecycle": bool, "mem": str, "threads": int, "args": list,
               "attribution": list, "access": str, "rules": bool, "age_pyramids": bool, "verify": bool, "annotate": list,
               "parts": dict})
def index(ctx: Ctx, arg: str | None) -> None:
    """Every source's layer-2 (imported here from the listings unless `list` did), the snapshot JSONs
    (`options.index.webdata`: the deployment's command, `{web}` its out dir, `{sources}` the `<bucket>=<layer-2>` args),
    published to `snapshots/<subdir>/<scan>/` (+ `lifecycle.json`), and the index tiers (`dt-cloud index-write`) under
    `<layer2>/index/<gen>/`, `path-index.parquet` copied last: the skip-if-done mark. `options.index.mode: path-index`
    builds both from the listings in one pass instead (`path_index`; with `age_pyramids` and `verify`).
    `options.index.mem`: DuckDB's memory limit (index-write's; under path-index, the age pyramids' and the check's).

    `options.index.parts` (path-index mode; `{bucket: [split bounds]}`, `{}` for one part per bucket): the index
    sharded (`index_parts`). `scan step index <part id>` (scan.nf's `index_part` fan-out, one task per part) writes
    one part to `<layer2>/index-parts/<gen>/<id>/`; `scan step index` then merges them into exactly the one pass's
    outputs and carries on as it does."""
    if ctx.opt("index", "mode", "layer2") == "path-index":
        if arg:
            path_index_part(ctx, arg)
        else:
            path_index(ctx)
        return
    if arg:
        raise StepFailed(2, f"index {arg}: a part, but options.index has no parts (path-index mode)")
    imports = bool(ctx.opt("list", "import", False))
    srcs: list[str] = []
    with ctx.phase("layer-2"):
        for s in ctx.profile.sources:
            b = s.bucket
            if not imports and not ctx.l2_path(b).exists():
                _import(ctx, b, s.kind, ctx.need(ctx.listing_dir(b), "list"))
            local = sorted((ctx.work / "l2" / b / "scans").glob("*.parquet"))
            if local:
                srcs.append(f"{b}={local[0]}")
                continue
            dst = ctx.work / "l2-in" / f"{b}.parquet"
            if not dst.exists():
                copy_atomic(ctx.need(ctx.l2_path(b), "list"), dst)
            srcs.append(f"{b}={dst}")
    webdata = ctx.opt("index", "webdata")
    if webdata:
        web = ctx.work / "web"
        with ctx.phase("webdata"):
            ctx.sh(ctx.expand(webdata, web=web, sources=srcs))
        _publish(ctx, web)
    else:
        err("no options.index.webdata: no snapshot JSONs")
    key_dir = ctx.data_root / ctx.index_key
    if ctx.done(key_dir / "path-index.parquet", "index tiers"):
        return
    out = ctx.work / "index"
    with ctx.phase("index-write"):
        ctx.sh(["dt-cloud", "index-write", *(["-m", ctx.opt("index", "mem")] if ctx.opt("index", "mem") else []),
                "-t", ctx.opt("index", "threads") or ctx.nproc, "-o", out, *ctx.expand(ctx.opt("index", "args", [])), *srcs])
        files = sorted(out.glob("*.parquet"), key=lambda p: (p.name == "path-index.parquet", p.name))
        for f in files:
            copy_atomic(f, key_dir / f.name)


def _publish(ctx: Ctx, web: Path) -> None:
    """The snapshot JSONs in `web` to `snapshots/<subdir>/<scan>/` (+ the sources' `lifecycle.json`, and with
    `options.index.rules` the attribution rules' `snapshots/<subdir>/rules.json`; both never fatal)."""
    dest = ctx.data_root / ctx.snapshots / ctx.scan
    with ctx.phase("publish"):
        dest.mkdir(parents=True, exist_ok=True)
        for f in sorted(web.glob("*.json")):
            copy_atomic(f, dest / f.name)
        if ctx.opt("index", "lifecycle", False):
            # a GCS source by its `gs://` URI (the pull picks the cloud by scheme); `-k`: an unreadable bucket is left out
            # the S3 sources' endpoint from the profile (one per pull: they share a store), not the sweep's env var
            eps = sorted({src.endpoint for src in ctx.profile.sources if src.kind != "gcs" and src.endpoint})
            if len(eps) > 1:
                err(f"WARN: lifecycle pull: the S3 sources name {len(eps)} endpoints; pulling through $SWEEP_S3_ENDPOINT")
                eps = []
            ctx.sh(["dt-cloud", "lifecycle", "pull", "-k", *(["-E", eps[0]] if eps else []),
                    *[a for s in ctx.profile.sources for a in ("-b", f"gs://{s.bucket}" if s.kind == "gcs" else s.bucket)],
                    "-o", dest / "lifecycle.json"], fatal=False, warn="lifecycle pull failed")
        if ctx.opt("index", "rules", False):
            rules = ctx.work / "rules.json"
            ctx.sh(["dt-cloud", "rules", "-o", rules], fatal=False, allowed_codes=(1,), warn="attribution rules failed")
            if rules.exists():
                copy_atomic(rules, ctx.data_root / ctx.snapshots / "rules.json")


def upload(files: Sequence[Path], dest: str) -> None:
    """`files` to the `gs://<bucket>/<prefix>` dir `dest` in order (big ones in parallel chunks: the FUSE mount writes
    ~20-50 MB/s), so a caller lists its skip-if-done mark last."""
    from google.cloud import storage
    from google.cloud.storage import transfer_manager

    u = urlparse(dest)
    bucket = storage.Client().bucket(u.netloc)
    for f in files:
        blob = bucket.blob(f"{u.path.strip('/')}/{f.name}")
        if f.stat().st_size > 256 << 20:
            transfer_manager.upload_chunks_concurrently(str(f), blob, chunk_size=64 << 20, max_workers=16)
        else:
            blob.upload_from_filename(str(f))


def path_index(ctx: Ctx) -> None:
    """`options.index.mode: path-index`: every source's listing (`layout.listing`) + the attribution parquets
    (`options.index.attribution`, keys in the data store) + the access-log shards (`options.index.access`, a key glob;
    left out while none exist), staged to the task's local disk (`dt-cloud stage`; gcsfuse reads ~20-50 MB/s and the
    aggregation makes several passes), aggregated in one pass (`dt-cloud path-index`: the snapshot JSONs and the path
    store's sorts, with the attribution-independent rollups cached at `<listing>/dir-cache`), then the tiers uploaded
    to `<layer2>/index/<gen>/` (`path-index.parquet` last, the skip-if-done mark) and the JSONs published.

    `options.index.age_pyramids`: the age chart's tiers from the `path` sort (`dt-cloud age-pyramid -c`, checked against
    it), beside the sorts. `options.index.verify`: `dt-cloud index-verify` checks the index against the listings it was
    built from (per-bucket and total bytes / objects, the snapshot totals and age histogram, the age pyramids and the
    `bysize` sort, where written) and writes `verification.json` beside the tiers; a mismatch fails the step.
    `options.index.annotate`: a command run on the snapshot after the aggregation, before the checks and the publish
    (`{snap}`: its dir; `{listing}`: the first source's listing dir).

    Sharded (`options.index.parts`): the parts' dirs (`path_index_part`) staged and merged (`dt-cloud
    path-index-merge`: the one pass's outputs, byte for byte) in place of the aggregation; the rest as above, then
    the parts deleted."""
    key_dir = ctx.data_root / ctx.index_key
    parts = index_parts(ctx.profile)
    buckets = [s.bucket for s in ctx.profile.sources]
    if not parts:
        rebuilt = _need_listings(ctx, buckets)
    if ctx.done(key_dir / "path-index.parquet", "index tiers") and ctx.done(ctx.data_root / ctx.snapshots / ctx.scan / "meta.json", "snapshot"):
        return
    pi = ctx.work / "path-index"
    snap = ctx.work / "snap"
    pi.mkdir(parents=True, exist_ok=True)
    if parts:
        part_dirs = [ctx.need(ctx.data_root / _parts_key(ctx) / pid / PART_JSON, f"index {pid}").parent for pid, _ in parts]
        # verify reads the listings themselves: staged only for it
        rebuilt = _need_listings(ctx, buckets) if ctx.opt("index", "verify", False) else set()
        inp = _IndexInputs(ctx, buckets, rebuilt, listings=bool(ctx.opt("index", "verify", False)), extras=False)
        with ctx.phase("path-index merge"):
            inp.prepare()
            if inp.on_disk:
                dirs = part_dirs
            else:
                ctx.sh(["dt-cloud", "stage", "-o", inp.stage, inp.src(f"{_parts_key(ctx)}/*")])
                dirs = [Path(inp.local(inp.src(f"{_parts_key(ctx)}/{pid}"))) for pid, _ in parts]
            search = any(a in ("-S", "--search") for a in ctx.opt("index", "args", []))
            ctx.sh(["dt-cloud", "path-index-merge", "-o", snap, "-P", pi / "path-index.parquet", *(["-S"] if search else []), *dirs])
    else:
        inp = _IndexInputs(ctx, buckets, rebuilt)
        with ctx.phase("path-index"):
            inp.prepare()
            ctx.sh(["dt-cloud", "path-index", "-d", ctx.scan, *inp.args(),
                    "-o", snap, "-c", ctx.data_root / ctx.listing_key("dir-cache"), "-P", pi / "path-index.parquet",
                    *ctx.opt("index", "args", [])], env={"DUCKDB_TMP": str(inp.tmp)})
    on_disk, lglobs, local = inp.on_disk, inp.lglobs, inp.local
    annotate = ctx.opt("index", "annotate")
    if annotate:
        # the deployment's own stamp on the snapshot before it's checked and published (m3: `disk-tree capture-meta`,
        # the capture's physical disk/VM bytes); `{snap}` the snapshot dir, `{listing}` the first source's listing dir
        with ctx.phase("annotate"):
            ctx.sh(ctx.expand(annotate, snap=snap, listing=ctx.listing_dir(ctx.profile.sources[0].bucket)))
    duck = [*(["-m", ctx.opt("index", "mem")] if ctx.opt("index", "mem") else []), "-t", ctx.nproc]
    if ctx.opt("index", "age_pyramids", False):
        with ctx.phase("age-index"):
            ctx.sh(["dt-cloud", "age-pyramid", "-c", *duck, "-o", pi, pi / "path-index.parquet"])
    if ctx.opt("index", "verify", False):
        with ctx.phase("verify"):
            ctx.sh(["dt-cloud", "index-verify", *duck, "-d", ctx.scan, "-g", ctx.gen,
                    *[a for s in ctx.profile.sources for a in ("-b", s.bucket)], *[a for g in lglobs for a in ("-l", local(g))],
                    "-s", snap, "-o", pi / "verification.json", pi / "path-index.parquet"])
    with ctx.phase("path-index upload"):
        files = sorted((f for f in pi.iterdir() if f.is_file() and not f.name.startswith(".")),
                       key=lambda f: (f.name == "path-index.parquet", f.name))
        err(f"uploading {len(files)} files ({sum(f.stat().st_size for f in files) / 1e9:.1f} GB) to {ctx.data_uri}/{ctx.index_key}/")
        if on_disk:
            for f in files:
                copy_atomic(f, key_dir / f.name)
        else:
            upload(files, f"{ctx.data_uri}/{ctx.index_key}")
    _publish(ctx, snap)
    if parts:
        # the merged generation is uploaded: the parts are spent (a rerun skips on its `path-index.parquet`)
        drop_prefix(ctx, _parts_key(ctx))


def _parts_key(ctx: Ctx) -> str:
    """The sharded index's parts under the data store: `<layer2>index-parts/<gen>/<part id>/`, beside (never in) the
    generation dir `publish-r2` and `index-gc` walk."""
    return f"{ctx.layer2}index-parts/{ctx.gen}"


def drop_prefix(ctx: Ctx, key: str) -> None:
    """Delete the data store's `key/` (a local dir, or every object under the GCS prefix); never fatal."""
    try:
        if not urlparse(ctx.profile.data).scheme:
            shutil.rmtree(ctx.data_root / key, ignore_errors=True)
            return
        from google.cloud import storage

        u = urlparse(f"{ctx.data_uri}/{key}/")
        client = storage.Client()
        blobs = list(client.bucket(u.netloc).list_blobs(prefix=u.path.lstrip("/")))
        for i in range(0, len(blobs), 100):
            with client.batch():
                for b in blobs[i:i + 100]:
                    b.delete()
        err(f"deleted {len(blobs)} objects under {ctx.data_uri}/{key}/")
    except Exception as e:  # noqa: BLE001 — leftover parts cost storage, not correctness
        err(f"WARN: couldn't delete {ctx.data_uri}/{key}/: {type(e).__name__}: {e}")


def _need_listings(ctx: Ctx, buckets: Sequence[str]) -> set[str]:
    """Check `buckets`' listings exist; returns those to rebuild: a reprocessed scan whose listing is gone but whose
    layer-2 is kept (a meta store's scans from before it kept listings) is re-cut from a listing rebuilt from that
    layer-2 (`dt-cloud l2-listing`: exact but for `generation`)."""
    rebuilt = {b for b in buckets
               if ctx.reproc and not (ctx.listing_dir(b) / "_SUCCESS.json").exists() and ctx.l2_path(b).exists()}
    for b in buckets:
        if b not in rebuilt:
            ctx.need(ctx.listing_dir(b) / "_SUCCESS.json", "list")
    return rebuilt


class _IndexInputs:
    """`path-index`'s inputs for `buckets`: their listings (`layout.listing`, or a REPROC's rebuilt ones), the
    attribution parquets (`options.index.attribution`) and the access-log shards (`options.index.access`; left out
    while none exist). A local data dir (the local executor's: one machine, no FUSE) is read in place; a GCS one is
    staged to the task's disk first (`dt-cloud stage`; gcsfuse reads ~20-50 MB/s and the aggregation makes several
    passes). `listings` / `extras` False: neither staged nor passed (the merge needs neither)."""

    def __init__(self, ctx: Ctx, buckets: Sequence[str], rebuilt: set[str], listings: bool = True, extras: bool = True):
        self.ctx, self.rebuilt = ctx, rebuilt
        self.on_disk = not urlparse(ctx.profile.data).scheme
        store = ctx.data_uri.split("://", 1)[-1]  # `<bucket>[/<prefix>]`, or the dir
        # `dt-cloud stage`'s glob form
        self.src = (lambda key: str(ctx.data_root / key)) if self.on_disk else (lambda key: f"/gcs/{store}/{key}")
        self.lglobs = [str(ctx.work / "l2-listing" / b / "*.parquet") if b in rebuilt else self.src(f"{ctx.listing_key(b)}/*.parquet")
                       for b in buckets] if listings else []
        self.attr = [self.src(a) for a in ctx.opt("index", "attribution", [])] if extras else []
        access = ctx.opt("index", "access") if extras else None
        self.xglobs = [self.src(access)] if access and next(iter(ctx.data_root.glob(access)), None) else []
        if access and not self.xglobs:
            err(f"no access shards at {access}: the index runs without read recency")
        self.stage = ctx.work / "stage"
        self.tmp = self.stage / ".duckdb-tmp"

    def local(self, g: str) -> str:
        return g if self.on_disk or not g.startswith("/gcs/") else str(self.stage / g.removeprefix("/gcs/"))

    def prepare(self) -> None:
        """Rebuild the REPROC listings, stage the GCS inputs, make the DuckDB spill dir."""
        ctx = self.ctx
        for b in sorted(self.rebuilt):
            err(f"REPROC: {b} has no listing: rebuilding one from its layer-2 (`generation` not kept)")
            ctx.sh(["dt-cloud", "l2-listing", "-b", b, ctx.l2_path(b), ctx.work / "l2-listing" / b / "shard-00000.parquet"])
        staged = [g for g in self.lglobs if g.startswith("/gcs/")]
        if not self.on_disk and (staged or self.attr or self.xglobs):
            ctx.sh(["dt-cloud", "stage", "-o", self.stage, *staged, *[g.replace("*.parquet", "_SUCCESS.json") for g in staged],
                    *self.attr, *self.xglobs])
        self.tmp.mkdir(parents=True, exist_ok=True)

    def args(self) -> list[str]:
        """`path-index`'s `-l` / `-a` / `-x`."""
        return [*[a for g in self.lglobs for a in ("-l", self.local(g))], *[a for g in self.attr for a in ("-a", self.local(g))],
                *[a for g in self.xglobs for a in ("-x", self.local(g))]]


def path_index_part(ctx: Ctx, pid: str) -> None:
    """One part of the sharded index (`options.index.parts`; `scan step index <id>`): its bucket's listing (+ the
    attribution and access inputs), aggregated to its range (`dt-cloud path-index -p <bucket> [-K lo:hi]`, the
    index's `args` but `-S`, which the merge applies), its dir uploaded to `<layer2>/index-parts/<gen>/<id>/`
    (`part.json` last, the skip-if-done mark). Its rollup cache is `<listing>/dir-cache/<id>`."""
    parts = dict(index_parts(ctx.profile))
    if pid not in parts:
        raise StepFailed(2, f"index {pid}: not one of options.index.parts' parts ({', '.join(parts) or 'none'})")
    part = parts[pid]
    key = f"{_parts_key(ctx)}/{pid}"
    if ctx.done(ctx.data_root / ctx.index_key / "path-index.parquet", "index tiers") or ctx.done(ctx.data_root / key / PART_JSON, f"part {pid}"):
        return
    rebuilt = _need_listings(ctx, [part.bucket])
    inp = _IndexInputs(ctx, [part.bucket], rebuilt)
    out = ctx.work / "parts" / pid
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    args = [a for a in ctx.opt("index", "args", []) if a not in ("-S", "--search")]
    with ctx.phase(f"path-index {pid}"):
        inp.prepare()
        ctx.sh(["dt-cloud", "path-index", "-d", ctx.scan, *inp.args(), "-p", part.bucket,
                *(["-K", f"{part.lo or ''}:{part.hi or ''}"] if part.split else []),
                "-c", ctx.data_root / ctx.listing_key("dir-cache") / pid, "-P", out / "path-index.parquet", *args],
               env={"DUCKDB_TMP": str(inp.tmp)})
    with ctx.phase(f"path-index {pid} upload"):
        files = sorted((f for f in out.iterdir() if f.is_file() and not f.name.startswith(".")), key=lambda f: (f.name == PART_JSON, f.name))
        if inp.on_disk:
            for f in files:
                copy_atomic(f, ctx.data_root / key / f.name)
        else:
            upload(files, f"{ctx.data_uri}/{key}")


def _static_check(p: ScanProfile) -> list[str]:
    o = p.opts("static-names")
    if o.get("gen"):
        return [f"options.static-names.{k}: not with gen (the one-machine path)" for k in ("profile", "merge") if k in o]
    problems = [f"options.static-names.{k}: only with gen (the one-machine path)" for k in ("store", "mem", "build_args", "current_key") if k in o]
    if not o.get("profile") and not p.env.get("STATIC_NAMES_PROFILE"):
        problems.append("options.static-names.profile: required (the deployment's static-names profile; or env STATIC_NAMES_PROFILE, "
                        "or gen: the one-machine path, or skip: [static-names])")
    return problems


@step("static-names", stage="serve", soft=True, options={"profile": str, "gen": str, "store": str, "mem": str, "build_args": list, "current_key": str,
                                                          "merge": bool},
      requires=_static_check)
def static_names(ctx: Ctx, arg: str | None) -> None:
    """Append this scan (and any earlier one still pending, in scan-id order) to the static name index as a run
    (`static-names runs add -c`: its stages are Batch jobs of their own; idempotent). Runs beside publish-r2 →
    interval-store (an `overlapped` phase). Due merges and their R2 publication are waited on: a failure records the phase
    failed, alerts, and fails the pipeline. Published data stays valid; a resume retries the missing work.

    `options.static-names.gen`: the one-machine path instead, for a small deployment (`static-names add` in this task:
    every scan in the data store after the generation's newest, read at the layer-2 layout's `path` sorts, appended to
    the generation in `store`, default `$R2_ARCHIVE`); `build` (+ `build_args`) when the store has no such generation."""
    if ctx.reproc:
        err("static-names off under REPROC")
        return
    gen = ctx.opt("static-names", "gen")
    if gen:
        store = ctx.opt("static-names", "store") or ctx.env.get("R2_ARCHIVE")
        if not store:
            err(f"static-names: not appending {ctx.scan} (no store: options.static-names.store or R2)")
            return
        layout = ctx.profile.layout.layer2.replace("{scan}", "{id}") + "index/{gen}/path-index.parquet"
        # a local data dir holds only this scan, so the store (where publish-r2 put it, before this step) is the source
        source = ctx.data_uri if urlparse(ctx.profile.data).scheme else store
        args = ["-g", gen, "-L", layout, "-s", source, "-o", store, "-w", ctx.work / "static-names", "-p", ctx.nproc,
                *(["-u", ctx.opt("static-names", "current_key")] if ctx.opt("static-names", "current_key") else []),
                *(["-M", ctx.opt("static-names", "mem")] if ctx.opt("static-names", "mem") else [])]
        with ctx.phase("static-names", note="overlapped"):
            sub = (["add", *args] if ctx.store_has(store, f"static-names/{gen}/scans.json") or ctx.store_has(store, f"static-names/{gen}/successor.json")
                   else ["build", *args, *ctx.opt("static-names", "build_args", [])])
            ctx.sh(["dt-cloud", "static-names", *sub], fatal=False, warn=f"static-names {sub[0]} failed for {ctx.scan}")
        return
    from .append_runner import BUSY

    prof = ctx.opt("static-names", "profile") or ctx.env["STATIC_NAMES_PROFILE"]
    # exit 4: another coordinator (a catch-up, or an earlier scan's run) or a compaction holds the store's lease; it
    # appends this scan too, else the next run does. Until then search refuses the scan: an explicit skip (the probe
    # reads it from the record and skips those refusals; /scans shows the run `partial`, with this reason)
    with ctx.phase("static-names", note="overlapped"):
        # `merge: false`: the due carries (and a compaction) wait for `runs merge` on its own schedule, never a scan's task
        merge = ctx.opt("static-names", "merge", True)
        ctx.sh(["dt-cloud", "static-names", "runs", "add", "-c", *([] if merge else ["-M"]), ctx.scan], env={"STATIC_NAMES_PROFILE": str(prof)},
               fatal=False, warn=f"static-names runs add failed for {ctx.scan}", allowed_codes=(BUSY,))
        if ctx.last is not None and ctx.last.returncode == BUSY:
            ctx.skip("busy", STATIC_NAMES_BUSY)
    static_lag(ctx)


def static_lag(ctx: Ctx) -> dict | None:
    """One alert per lag episode of the static name index (`static-names runs lag`): more than a scan (or one for 12 h)
    behind the newest published scan, search refusing those. Run after `runs add` and again by `probe`, which a
    deployment skipping its static-names step (cw since 10-22: the outage this is for) or a step dying first still runs;
    the episode's marker keeps it to one alert. Not on the one-machine path (`options.static-names.gen`) nor without a
    static-names profile. Returns `runs lag`'s record (None without one)."""
    prof = ctx.opt("static-names", "profile") or ctx.env.get("STATIC_NAMES_PROFILE")
    if ctx.reproc or ctx.opt("static-names", "gen") or not prof:
        return None
    out = ctx.sh(["dt-cloud", "static-names", "runs", "lag", ctx.scan], env={"STATIC_NAMES_PROFILE": str(prof)}, fatal=False,
                 warn=f"static-names runs lag failed for {ctx.scan}", capture=True)
    lag = json.loads(out) if out else None
    if lag and lag["episode"] == "start":
        why = " (this deployment skips its static-names step)" if "static-names" in ctx.profile.skip else (
            f" (static-names skipped: {r})" if (r := ctx.recorded_skip("static-names")) else "")
        ctx.run.alert("warn", f"static name index lags: its newest scan is {lag['newest']}, {len(lag['behind'])} published scans behind "
                              f"({lag['behind'][0]} … {lag['behind'][-1]}, {lag['s'] / 3600:.1f} h){why}; search refuses them until the "
                              "catch-up appends them")
    return lag


#: Why a scan's static-names step was skipped (`runs add` exit `BUSY`): its phase note's reason, and the probe's.
STATIC_NAMES_BUSY = ("another coordinator or a compaction holds the static-names store's lease; search for this scan "
                     "comes with its append or the next run")


@step("publish-r2", stage="serve", soft=True, options={"args": list, "require": list, "keys": list, "barrier": bool})
def publish_r2(ctx: Ctx, arg: str | None) -> None:
    """The scan's served subset (snapshot JSONs, the layer-2 dir) to R2, idempotent by size + md5; `-a`: every
    `index/<gen>/` under the scan, not D1's (the scan is listed after). Options: `args` (e.g. `-S`: only the small files
    the site reads beside the path store), `require` (keys that must be there and verify, `{snapshots}`, `{scan}`,
    `{index}` filled), `keys` (store-wide keys published beside the scan, each required too), and `barrier`: a site that
    serves these from R2 only fails the run instead (no D1 pointer moves; the previous scan stays visible)."""
    barrier = bool(ctx.opt("publish-r2", "barrier", False))
    with ctx.phase("publish-r2"):
        if not ctx.has(*R2):
            if barrier:
                raise StepFailed(1, "no R2_ENDPOINT/R2_BUCKET: publication blocked (D1 pointers not advanced)", hard=True)
            err("no R2_ENDPOINT/R2_BUCKET: skipping publish-r2")
            return
        fill = dict(snapshots=ctx.snapshots, scan=ctx.scan, index=ctx.index_key)
        req = [a for k in ctx.opt("publish-r2", "require", []) for a in ("-R", k.format(**fill))]
        # A local data dir is its own source; a GCS one is the env's `$DATA_BUCKET`.
        src = [] if urlparse(ctx.profile.data).scheme else ["-b", str(ctx.data_root.resolve())]
        runs = [["dt-cloud", "publish-r2", *src, *ctx.opt("publish-r2", "args", []), "-a", *req, ctx.scan]]
        runs += [["dt-cloud", "publish-r2", *src, "-a", "-p", k, "-R", k, ctx.scan] for k in ctx.opt("publish-r2", "keys", [])]
        for argv in runs:
            if ctx.sh(argv, fatal=False, warn=f"publish-r2 failed for {ctx.scan} (the site keeps serving the data store)") is None and barrier:
                raise StepFailed(1, f"publish-r2 failed for {ctx.scan}: D1 pointers were not advanced, so the previous scan "
                                    "remains visible; repair the R2 copy, then resume", hard=True)


def _iv_check(p: ScanProfile) -> list[str]:
    o = p.opts("interval-store")
    bad = [f"options.interval-store.source: {o['source']!r} (one of store, data)"] if o.get("source", "store") not in ("store", "data") else []
    if o.get("source") and o.get("profile"):
        bad.append("options.interval-store.source: only on the one-machine path (the staged one reads its profile's layouts)")
    if not p.primary and not o.get("store"):
        bad.append("options.interval-store.store: required for a secondary store (an independent namespace)")
    return bad


@step("interval-store", stage="serve", soft=True, options={"store": str, "timeout": str, "mem": str, "profile": str, "merge_wait": int,
                                                             "source": str},
      requires=_iv_check)
def interval_store(ctx: Ctx, arg: str | None) -> None:
    """Append this scan (and any pending) to the change-interval path store beside its current generation, then the
    due carries / compaction (`interval-store add`). Never fatal (`-W`): a failure or the timeout warns, the next run
    catches up. Reads the scans' `path` sorts at `<layer2>/index/<gen>/path-index.parquet` from the store, or with
    `options.interval-store.source: data` from the data store, as `static-names` does (so `publish-r2 -S` needn't copy
    the sorts to the store: specs/lean-scans.md §1.1).
    `options.interval-store.profile`: the staged path instead (`interval-store append -c`, its per-range stages Batch
    jobs of their own, in this task's image; `merge_wait` seconds on the merge job), for a store too big for one task."""
    if ctx.reproc:
        err(f"interval-store: not appending {ctx.scan} (REPROC)")
        return
    from .scan_runs import duration, run_grouped

    timeout = ctx.opt("interval-store", "timeout")
    if ctx.opt("interval-store", "profile"):
        from .append_runner import BUSY

        cmd = ["dt-cloud", "interval-store", "append", "-c", "-w", str(ctx.opt("interval-store", "merge_wait", 0)), ctx.scan]
        env = {"INTERVAL_STORE_PROFILE": str(ctx.opt("interval-store", "profile"))}
        if ctx.env.get("JOB_IMAGE"):
            env["INTERVAL_STORE_IMAGE"] = ctx.env["JOB_IMAGE"]
        ctx.run.begin("interval-store")
        err("+ " + " ".join(cmd))
        with ctx.beating("interval-store") as tail:
            ran = run_grouped(cmd, duration(timeout) if timeout else None, 30.0, env={**ctx.env, **env}, tail=tail)
        if ran.rc == BUSY:
            ctx.run.end("interval-store", 0, note="another coordinator holds the store's lease; it appends this scan")
            return
        ctx.run.end("interval-store", ran.rc, warn=True, msg="the scan reads per-scan until a later run appends it",
                    timed_out=timeout if ran.timed_out else None, terminated=ran.signal if ran.rc else None)
        return
    store = ctx.opt("interval-store", "store") or ctx.env.get("R2_ARCHIVE")
    if not store:
        err(f"interval-store: not appending {ctx.scan} (no store: options.interval-store.store or R2)")
        return
    ctx.run.begin("interval-store")
    cur = ctx.sh(["dt-cloud", "interval-store", "current", "-o", store], fatal=False, capture=True)
    if cur is None:
        ctx.run.end("interval-store", (ctx.failure or (1, ""))[0], warn=True,
                    msg=f"interval-store current failed: not appending {ctx.scan}")
        return
    if not cur.strip():
        err(f"no interval store at {store} yet: not appending {ctx.scan}")
        ctx.run.end("interval-store", 0, note="skipped: no current generation")
        return
    layout = ctx.profile.layout.layer2.replace("{scan}", "{id}") + "index/{gen}/path-index.parquet"
    source = ctx.data_uri if ctx.opt("interval-store", "source", "store") == "data" else store
    cmd = ["dt-cloud", "interval-store", "add", "-L", layout, "-s", source, "-o", store, "-w", str(ctx.work / "interval-store"),
           *(["-M", ctx.opt("interval-store", "mem")] if ctx.opt("interval-store", "mem") else []), "-p", str(ctx.nproc)]
    err("+ " + " ".join(cmd))
    with ctx.beating("interval-store") as tail:
        ran = run_grouped(cmd, duration(timeout) if timeout else None, 30.0, env=ctx.env, tail=tail)
    ctx.run.end("interval-store", ran.rc, warn=True, msg="the scan reads per-scan until a later run appends it",
                timed_out=timeout if ran.timed_out else None, terminated=ran.signal if ran.rc else None)


def _sync_args(ctx: Ctx) -> list[str]:
    """Both halves' arguments: the generation, the store, `options.index-sync.variants` (`-v` each; default every
    variant the generation holds) and `options.index-sync.d1_rows: false` (`-R`: the cold footer files and the pointers,
    no row group in D1; specs/lean-scans.md §1.4). A local `data:` dir (the local executor's) is not what the site reads: `-b` names
    the R2 bucket (`publish-r2` filled it first), where the pointer flip checks the generation is uploaded."""
    site = ["-b", f"r2://{ctx.env['R2_BUCKET']}"] if ctx.profile.data_bucket is None and ctx.has(*R2) else []
    return [*store_args(ctx), *site, "-d", str(ctx.data_root / ctx.index_key), "-g", ctx.gen, "-k", ctx.index_key,
            *[a for v in ctx.opt("index-sync", "variants", []) for a in ("-v", v)],
            *([] if ctx.opt("index-sync", "d1_rows", True) else ["-R"])]


def _sync_check(p: ScanProfile) -> list[str]:
    from .index_footer import INDEX_VARIANTS

    o = p.opts("index-sync")
    return [f"options.index-sync.variants: {v!r} is not an index variant (one of {', '.join(INDEX_VARIANTS)})"
            for v in o.get("variants", []) if v not in INDEX_VARIANTS] + (
        [] if o.get("d1_rows", True) or p.primary else
        [f"store {p.store}: options.index-sync.d1_rows: false is for the primary store (a secondary store never reads the "
         "interval store: every read of it would plan from cold footers)"])


@step("index-rows", skippable=False, stage="serve", soft=True)
def index_rows(ctx: Ctx, arg: str | None) -> None:
    """The index tiers' row groups to D1 (`index-sync -r`), invisible until `index-sync` flips the pointers: overlaps
    static-names and interval-store. Re-runnable (INSERT OR REPLACE)."""
    with ctx.phase("index-rows"):
        if not ctx.has(*CF):
            err("WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID: rows not uploaded")
            return
        ctx.sh(["dt-cloud", "index-sync", "-r", *_sync_args(ctx), ctx.scan], fatal=False, warn="index-sync -r failed (the pointer flip will refuse)")


@step("index-sync", skippable=False, stage="sync", soft=True, options={"retain": int, "variants": list, "d1_rows": bool},
      requires=_sync_check)
def index_sync(ctx: Ctx, arg: str | None) -> None:
    """List the scan: flip its pointers (`index-sync -p`, which checks the rows `index-rows` uploaded), last of the
    serving steps, so a listed scan is whole (searchable, on R2, in the interval store); then drop superseded
    generations' D1 rows (`index-gc <scan>`; `options.index-sync.retain` N: also retire the row groups of every scan
    older than the newest N, read from their cold footer files after, D1's size cap). `options.index-sync.variants`:
    only those variants, in both halves (`index-rows` reads them here too). `options.index-sync.d1_rows: false`: no
    row group in D1, in both halves (`-R`), for a primary store whose site reads the interval store
    (specs/lean-scans.md §1.4)."""
    with ctx.phase("index-sync"):
        if not ctx.has(*CF):
            err("WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID: tiers published but not synced (scan unlisted)")
            return
        ctx.sh(["dt-cloud", "index-sync", "-p", *_sync_args(ctx), "-M", ctx.data_root / ctx.snapshots, ctx.scan],
               fatal=False, warn="index-sync failed (the site keeps serving the previous generation)")
        retain = ctx.opt("index-sync", "retain")
        ctx.sh(["dt-cloud", "index-gc", *store_args(ctx), *(["-r", retain] if retain else []), ctx.scan], fatal=False, warn="index-gc failed")


@step("over-time", soft=True, options={"mem": str, "threads": int})
def over_time(ctx: Ctx, arg: str | None) -> None:
    """Seal every full K-scan run of indexed scans not yet grouped (`over-time-groups`, idempotent), read from and
    published to R2 when set up (else the data store, each new group then copied to R2 by `publish-r2 -L`)."""
    with ctx.phase("over-time"):
        if not ctx.has(*CF):
            err("no CLOUDFLARE_API_TOKEN/ACCOUNT_ID: over-time groups need D1; skipping")
            return
        archive = ctx.env.get("R2_ARCHIVE")
        root = archive or str(ctx.data_root)
        out = ctx.sh(["dt-cloud", "over-time-groups", *store_args(ctx), "-g", ctx.gen, "-o", ctx.work / "over-time", "-p", root,
                      *(["-m", ctx.opt("over-time", "mem")] if ctx.opt("over-time", "mem") else []),
                      *(["-t", ctx.opt("over-time", "threads")] if ctx.opt("over-time", "threads") else [])],
                     capture=True, fatal=False, warn="over-time groups failed (the series uses its configured path store)")
        if out is None or archive:
            return
        for gid in json.loads(out or "{}").get("groups", []):
            if ctx.has(*R2):
                ctx.sh(["dt-cloud", "publish-r2", "-L", gid], fatal=False, warn=f"publish-r2 failed for over-time group {gid}")


@step("index-gc", soft=True, options={"grace": str, "dry": bool})
def index_gc(ctx: Ctx, arg: str | None) -> None:
    """Delete superseded index generations' files (no `index_schema` row of the store names them, older than the grace)
    from the data store and R2, over every scan. `options.index-gc.dry`: list them only (`-n`, what would go, in the
    log), for a store with no soft delete (R2) until a reviewed list."""
    with ctx.phase("index-gc"):
        if not ctx.has(*CF):
            return
        targets = ["-F", ctx.data_uri, *(["-F", "r2"] if ctx.has(*R2) else [])]
        dry = ["-n"] if ctx.opt("index-gc", "dry", False) else []
        ctx.sh(["dt-cloud", "index-gc", *store_args(ctx), "-R", *dry, "-m", ctx.opt("index-gc", "grace", "2d"), *targets], fatal=False,
               warn="index-gc -F failed (superseded generations stay until the next run)")


@step("dups", core=False, soft=True, options={"floor": int, "check": int, "check_objects": int, "suspect_copies": int,
                                               "mem": str, "threads": int, "listing": bool})
def dups(ctx: Ctx, arg: str | None) -> None:
    """The scan's duplicate-candidate groups (`dt-cloud dups`, specs/dups.md §1) beside its index:
    `<layer2>index/<gen>/dups.{parquet,json}` (`dups.json` last, the skip-if-done mark), read from the scan's `bysize`
    sort (its `tot` prunes the read to the paths over the floor), else its `path` sort. Options: `floor` (bytes),
    `suspect_copies`, `check` (spot-check the top N groups' copies on GCS), `check_objects`, `mem`, `threads`,
    `listing` (`-l` each source's raw listing shards in the data store: copies proven `exact` by their objects'
    crc32c, which the listing carries with `options.list.bulk_args: [-c]`). Off unless the profile lists it in `steps`; never fatal."""
    key_dir = ctx.data_root / ctx.index_key
    if ctx.done(key_dir / "dups.json", "dups"):
        return
    bysize = key_dir / "path-index-bysize.parquet"
    src = bysize if bysize.exists() else ctx.need(key_dir / "path-index.parquet", "index")
    out = ctx.work / "dups"
    flags = {"floor": "-f", "check": "-g", "check_objects": "-n", "suspect_copies": "-s", "mem": "-m", "threads": "-t"}
    args = [a for k, f in flags.items() if ctx.opt("dups", k) is not None for a in (f, ctx.opt("dups", k))]
    if ctx.opt("dups", "listing", False):
        if ctx.profile.layout.listing is None:  # an importing profile's listings stay in its list tasks' scratch
            err("dups: options.dups.listing without layout.listing: no exact verdicts")
        else:
            args += [a for src in ctx.profile.sources
                     for a in ("-l", f"{ctx.data_root / ctx.listing_key(src.bucket)}/shard-*.parquet")]
    with ctx.phase("dups"):
        r = ctx.sh(["dt-cloud", "dups", "-d", ctx.scan, *args, "-T", ctx.work / "dups-tmp", "-o", out, src], fatal=False,
                   warn="dups failed (the scan's other outputs stand)")
        if r is None:
            return
        copy_atomic(out / "dups.parquet", key_dir / "dups.parquet")
        copy_atomic(out / "dups.json", key_dir / "dups.json")


def _age_check(p: ScanProfile) -> list[str]:
    o = p.opts("age-store")
    return [f"options.age-store.{key}: required" for key in ("store", "gen") if not o.get(key)]


@step("age-store", core=False, soft=True, options={"store": str, "gen": str, "keep": int, "execute": bool, "copies": list, "threads": int, "mem": str},
      requires=_age_check)
def age_store(ctx: Ctx, arg: str | None) -> None:
    """Append verified histogram cells, then retire covered old pyramids on each copy. Runs before path retirement."""
    if ctx.reproc:
        err("age-store off under REPROC")
        return
    o = ctx.profile.opts("age-store")
    layout = ctx.profile.layout.layer2.replace("{scan}", "{id}") + "index/{gen}/age-pyramid-1h.parquet"
    roots = list(dict.fromkeys([ctx.data_uri, *o.get("copies", [])]))
    argv = ["dt-cloud", "age-store", "-g", o["gen"], "-L", layout, "-o", o["store"], "-s", ctx.data_uri,
            "-T", ctx.profile.store, "-t", ctx.scan, "-w", ctx.work / "age-store", "-k", o.get("keep", 2),
            *(a for root in roots for a in ("-r", root)), *(["-x"] if o.get("execute") else []),
            *(["-p", o["threads"]] if o.get("threads") else []), *(["-M", o["mem"]] if o.get("mem") else [])]
    with ctx.phase("age-store"), cleanup(ctx.work / "age-store"):
        if ctx.sh(argv, fatal=False, warn="age-store failed (uncovered histogram inputs stay)") is not None and o.get("execute"):
            ctx.sh(["dt-cloud", "store-gc", "-t", "interval-store", "-l", "-x", "-o", o["store"]], fatal=False,
                   warn="age-store generation cleanup failed (the next run retries)")


def _retire_check(p: ScanProfile) -> list[str]:
    o = p.opts("retire")
    problems = [] if isinstance(o.get("keep"), int) and o["keep"] >= 1 else ["options.retire.keep: required, ≥ 1 (the newest scans never retired)"]
    if not (o.get("static_gen") or p.opts("static-names").get("gen")):
        problems.append("options.retire.static_gen: required unless options.static-names.gen names the static index's generation")
    return problems


@contextmanager
def cleanup(path: Path) -> Iterator[None]:
    """`path` removed on the way out, whether the block finished or raised."""
    try:
        yield
    finally:
        shutil.rmtree(path, ignore_errors=True)


@step("retire", core=False, soft=True, options={"keep": int, "execute": bool, "static_gen": str, "static_store": str, "store": str,
                                                "layer2": bool, "layer1": bool, "cache": bool, "copies": list, "backup": str, "markers_only": bool,
                                                "verify_jobs": int},
      requires=_retire_check)
def retire(ctx: Ctx, arg: str | None) -> None:
    """Retire the per-scan sorts, footers, search sidecars (and, `layer2: true`, layer-2 listings; `layer1: true`, the raw
    listing shards under `layout.listing`; `cache: true`, its `dir-cache/`) of every scan but the
    newest `keep` that both stores hold and a parity marker vouches for (`dt-cloud scan retire`; specs/lean-scans.md §2).
    Off unless the profile lists it in `steps`; a dry run (the plan in the log) unless `execute: true`. The data store
    first (`$R2_ARCHIVE` when the data store is a local dir), then each of `copies` (a root, or `r2` for `$R2_ARCHIVE`; D1 left
    to the first). `store` / `static_store`: the
    stores (default the interval-store / static-names steps', else `$R2_ARCHIVE`); `static_gen` (default
    `options.static-names.gen`); `backup`: copy what goes there first; `markers_only`: never verify in the task;
    `verify_jobs`: verify that many shares of the scans at once (`-J`; a backlog's retire then costs its slowest share). Never
    fatal: a failure warns, the files stay, the next run retries."""
    if ctx.reproc:
        err("retire off under REPROC")
        return
    o = ctx.profile.opts("retire")
    archive = ctx.env.get("R2_ARCHIVE")
    store = o.get("store") or ctx.opt("interval-store", "store") or archive
    static_gen = o.get("static_gen") or ctx.opt("static-names", "gen")
    static_store = o.get("static_store") or ctx.opt("static-names", "store") or archive or store
    if not store:
        err("retire: no interval store (options.retire.store, options.interval-store.store or R2): nothing retired")
        return
    l2 = ctx.profile.layout.layer2.replace("{scan}", "{id}")
    l1 = []
    if o.get("layer1"):
        if ctx.profile.layout.listing is None:
            err("retire: options.retire.layer1 without layout.listing: raw listings kept")
        else:
            l1 = ["-1", ctx.profile.layout.listing.replace("{scan}", "{id}") + "*/shard-*.parquet"]
    cache = []
    if o.get("cache"):
        if ctx.profile.layout.listing is None:
            err("retire: options.retire.cache without layout.listing: rollup caches kept")
        else:
            # `path-index -c`'s cache (`listing_key("dir-cache")`), one dir per index part under a sharded index
            dc = ctx.profile.layout.listing.replace("{scan}", "{id}") + "dir-cache/"
            cache = ["-c", dc + "*.parquet", "-c", dc + "*/*.parquet"]
    common = ["-G", static_gen, "-k", o["keep"], "-L", l2 + "index/{gen}/path-index.parquet", *(["-l", l2 + "*.parquet"] if o.get("layer2") else []), *l1, *cache,
              *(["-m"] if o.get("markers_only") else []), *(["-J", o["verify_jobs"]] if o.get("verify_jobs") else []), "-o", store, "-S", static_store, *store_args_t(ctx), "-w", ctx.work / "retire",
              *(["-x"] if o.get("execute") else [])]
    # A local data store is the task's scratch (only this scan's files): the scans' files and D1 rows are the archive's.
    first = archive if ctx.profile.data_bucket is None and archive else ctx.data_uri
    roots = [r for r in dict.fromkeys([first, *(archive if c == "r2" else c for c in o.get("copies", []))]) if r]
    # The work dir holds the served store the verify downloads: removed whatever happens, so a failed retire never
    # leaves the steps after it (soft steps on the same disk) without space
    with ctx.phase("retire", note=None if o.get("execute") else "dry run"), cleanup(ctx.work / "retire"):
        for i, root in enumerate(roots):
            # a copy after the D1 pass: its rows are that pass's (`-D`: no "-R leaves D1 rows" warning in a dry run)
            d1 = [] if i == 0 and ctx.has(*CF) else ["-R", *(["-D"] if ctx.has(*CF) else [])]
            backup = ["-b", o["backup"]] if i == 0 and o.get("backup") else []
            ctx.sh(["dt-cloud", "scan", "retire", *backup, *common, *d1, "-s", root], fatal=False,
                   warn=f"retire failed on {root} (its per-scan files stay; the next run retries)")


@step("store-gc", core=False, soft=True, options={"execute": bool, "grace": int, "store": str, "copies": list, "static_gens": list})
def store_gc(ctx: Ctx, arg: str | None) -> None:
    """Superseded interval-store generations (`dt-cloud store-gc -l`): what the current generation was compacted or
    backfilled from, past the grace (`grace` seconds, default 3 days), its parity markers moved first; on the interval
    store (`store`, default the interval-store step's, else `$R2_ARCHIVE`) and on `$R2_ARCHIVE` when that's another
    copy. Off unless the profile lists it in `steps`; a dry run unless `execute: true`. Explicit `copies` overrides the
    archive copy. Optional `static_gens` names every live static lineage root: cleanup protects each root and its
    newest successor, plus the native current pointer, before collecting older generations past the grace."""
    if ctx.reproc:
        err("store-gc off under REPROC")
        return
    archive = ctx.env.get("R2_ARCHIVE")
    store = ctx.opt("store-gc", "store") or ctx.opt("interval-store", "store") or archive
    if not store:
        err("store-gc: no interval store (options.store-gc.store, options.interval-store.store or R2): nothing collected")
        return
    args = ["-t", "interval-store", "-l", *(["-G", ctx.opt("store-gc", "grace")] if ctx.opt("store-gc", "grace") is not None else []),
            *(["-x"] if ctx.opt("store-gc", "execute") else [])]
    with ctx.phase("store-gc", note=None if ctx.opt("store-gc", "execute") else "dry run"):
        for root in dict.fromkeys(r for r in [store, *ctx.opt("store-gc", "copies", [archive])] if r):
            ctx.sh(["dt-cloud", "store-gc", *args, "-o", root], fatal=False, warn=f"store-gc failed on {root} (the generations stay)")
        if ctx.opt("store-gc", "static_gens") and archive:
            static = ["-t", "static-names", *(a for g in ctx.opt("store-gc", "static_gens") for a in ("-k", g)),
                      *(["-x"] if ctx.opt("store-gc", "execute") else [])]
            ctx.sh(["dt-cloud", "store-gc", *static, "-o", archive], fatal=False,
                   warn="static generation cleanup failed (the generations stay)")


def store_args_t(ctx: Ctx) -> list[str]:
    """`-t <store>` for `scan retire` (its `-s` is the scans root): nothing for the primary."""
    return [] if ctx.profile.primary else ["-t", ctx.profile.store]


def _snap_root(ctx: Ctx) -> str:
    return f"{ctx.env.get('R2_ARCHIVE') or ctx.data_uri}/{ctx.snapshots}"


@step("non-live", core=False, stage="side", soft=True, options={"classes": list})
def non_live(ctx: Ctx, arg: str | None) -> None:
    """Each source's non-live bytes (`dt-cloud non-live`, specs/non-live-bytes.md phase 1): per class of
    `options.non-live.classes` (each one the sources' provider has: `soft-deleted` on GCS, `noncurrent` /
    `delete-marker` on S3, `mpu`), the bytes the listing of current objects misses, totaled per bucket into
    `<layer2>non-live/<bucket>.parquet` (a few rows: kept per scan, the totals' history) and the scan's snapshot
    `non-live.json` (every bucket's rows: what the site reads beside `meta.json`). A `side` step: its own task from the
    scan's start, beside `list` and `index`, which never wait on it (gcs's soft-deleted listings take ~18 min); only
    `publish-r2` (its snapshot dir reaches R2 with the scan's others) and the post steps do. Read-only; off unless the
    profile lists it in `steps`. Never fatal: a failure warns, the scan's other outputs stand."""
    classes = ctx.opt("non-live", "classes", [])
    if not classes:
        err("non-live: no options.non-live.classes: nothing measured")
        return
    from .non_live import PROVIDER_CLASSES, split_uri

    with ctx.phase("non-live"):
        # each source measures the classes its provider has (an hf:// source none: the Hub's held bytes are in its
        # listing, `@history`); a deployment of mixed providers lists the union. Sources sharing classes and endpoint
        # go in one call, measured `-P` at once (gcs's buckets one after another took ~45 min of `pre`).
        groups: dict[tuple[tuple[str, ...], str | None], list[str]] = {}
        for src in ctx.profile.sources:
            has = [] if src.kind == "hf" else [c for c in classes if c in PROVIDER_CLASSES[split_uri(src.uri)[0]]]
            if not has:
                continue
            endpoint = src.endpoint or (ctx.env.get("R2_ENDPOINT") if src.kind == "r2" else None)
            groups.setdefault((tuple(has), endpoint), []).append(src.uri)
        for (has, endpoint), uris in groups.items():
            ctx.sh(["dt-cloud", "non-live", "-c", ",".join(has), *(["-E", endpoint] if endpoint else []),
                    "-j", ctx.data_root / ctx.snapshots / ctx.scan / "non-live.json",
                    "-o", ctx.data_root / ctx.layer2 / "non-live" / "{bucket}.parquet", *uris],
                   fatal=False, warn=f"non-live failed on {', '.join(uris)} (the scan's other outputs stand)")


@step("healthcheck", core=False, soft=True)
def healthcheck(ctx: Ctx, arg: str | None) -> None:
    """Is the site serving the scan (`dt-cloud healthcheck -d`: fresh, subtree + the data JSONs serve)? A failure
    warns: the data is fine, serving may be degraded."""
    with ctx.phase("healthcheck"):
        if ctx.reproc or not ctx.has("SITE_TOKEN", "SITE_URL"):
            err("no SITE_TOKEN/SITE_URL, or REPROC: skipping the health check")
            return
        ctx.sh(["dt-cloud", "healthcheck", "-d", ctx.scan], fatal=False,
               warn=f"the live site's health check failed for {ctx.scan} (data is fine; serving may be degraded)")


@step("warm-cache", soft=True, requires=primary_only("warm-cache", "its requests carry no `store=`: it would warm the primary's views"))
def warm_cache(ctx: Ctx, arg: str | None) -> None:
    """Warm the site's subtree + diff caches for the scan (its job grant, `SITE_TOKEN`)."""
    with ctx.phase("warm-cache"):
        if ctx.reproc or not (ctx.has("SITE_TOKEN") or ctx.has("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")):
            err("no warm-cache auth (SITE_TOKEN, or CF_ACCESS_CLIENT_ID+SECRET), or REPROC: skipping cache warm-up")
            return
        ctx.sh(["dt-cloud", "warm-cache", "-d", ctx.scan, "-r", _snap_root(ctx)], fatal=False, warn=f"cache warm-up failed for {ctx.scan}")


@step("probe", soft=True, requires=primary_only("probe", "its requests carry no `store=`: it would probe the primary's views"))
def probe(ctx: Ctx, arg: str | None) -> None:
    """Replay the site's page loads uncached (`probe -c`): one cold-latency record per scan under `probes/<subdir>/`.
    When this run's record says its static-names step was skipped (`Ctx.recorded_skip`), the probe gets that reason
    (`-i`) and skips the search's `scan-not-indexed` refusals for it; otherwise they fail it. The phase's note is the
    probe's verdict (`n passed, n skipped (reason), n failed`), on a failure too. When every failure was that refusal
    (the static name index lags the scan: its data is published), the phase is done, its note `search behind: …` in
    plain words (/scans: "search behind", not "partial"); `static_lag` alerts once per lag episode."""
    lagged = False
    try:
        with ctx.phase("probe"):
            if ctx.reproc or not ctx.has("SITE_TOKEN", "SITE_URL"):
                return
            sub = ctx.profile.layout.snapshots
            unindexed = ctx.recorded_skip("static-names")
            ctx.sh(["dt-cloud", "probe", "-c", "-u", ctx.env["SITE_URL"], "-o", f"{ctx.data_uri}/probes/{sub + '/' if sub else ''}",
                    "-j", *(["-i", unindexed] if unindexed else [])], fatal=False, capture=True,
                   warn=f"serving probe failed for {ctx.scan} (an HTTP or transport error; data is fine)")
            summary = _probe_summary(ctx.last.stdout if ctx.last is not None else None)
            verdict = ctx.note = summary["line"] if summary else None
            if ctx.failure and summary and summary.get("failed") and summary.get("lag") == summary["failed"]:
                lagged = True
                ctx.failure, ctx.note = None, behind_note(static_lag(ctx), summary)
            elif ctx.failure and verdict:
                ctx.failure = (ctx.failure[0], f"serving probe failed for {ctx.scan}: {verdict}")
    finally:
        # whatever the probe did, or without a site to probe
        if not lagged:
            static_lag(ctx)


def _probe_summary(stdout: str | None) -> dict | None:
    """`probe -j`'s record's summary (`passed`, `skipped`, `failed`, `lag`, the verdict `line`); None when it printed
    no record (it crashed first)."""
    try:
        return dict(json.loads(stdout or "")["summary"])
    except (ValueError, KeyError, TypeError):
        return None


def behind_note(lag: dict | None, summary: dict) -> str:
    """The probe phase's note when search lags the scan: `search behind: <the index's newest scan, how far behind>; <n>
    of <m> search requests refused (not indexed yet)`."""
    n = summary["passed"] + summary["skipped"] + summary["failed"]
    where = (f"the static name index covers scans through {lag['newest']}, {len(lag['behind'])} published "
             f"scan{'s' * (len(lag['behind']) != 1)} behind ({lag['s'] / 3600:.1f} h)") if lag and lag.get("behind") else "the static name index doesn't cover this scan yet"
    return f"search behind: {where}; {summary['lag']} of {n} probe requests refused as not indexed yet"


#: A digest platform → the env its transport needs (`dt-cloud digest -P`).
DIGEST_TRANSPORTS = {"slack": ("SLACK_BOT_TOKEN", "SLACK_CHANNEL"), "discord": ("DISCORD_GCS_USAGE_WEBHOOK", "DISCORD_BOT_TOKEN")}


def _digest_check(p: ScanProfile) -> list[str]:
    o = p.opts("digest")
    problems = [f"options.digest.{k}: required (or skip: [digest])" for k in ("template", "config") if not o.get(k)]
    return problems + [f"options.digest.platforms: {x!r} is not one of {', '.join(DIGEST_TRANSPORTS)}"
                       for x in o.get("platforms", []) if x not in DIGEST_TRANSPORTS]


@step("digest", soft=True, options={"template": str, "config": str, "args": list, "platforms": list, "root": str}, requires=_digest_check)
def digest(ctx: Ctx, arg: str | None) -> None:
    """Converge the usage-digest thread on each platform (`dt-cloud digest -T <template> -C <config> -P <platform>`;
    `options.digest.platforms`, default slack), each skipped without its transport's env. The plot's upload
    (`wrangler pages deploy`) authenticates with `CLOUDFLARE_API_TOKEN`: `$CF_PAGES_TOKEN` when set, for a deployment
    whose D1 token can't deploy Pages. `options.digest.root` (`{snapshots}` filled): the snapshots root the digest reads
    and keeps its thread state beside (`<root>/digest/…`), when that isn't the data store's (`<data>/<snapshots>`), e.g.
    a deployment whose live state and earlier snapshots are on R2: a wrong root finds no state and posts a second OP."""
    if ctx.reproc:
        err("REPROC: skipping the usage digest")
        return
    with ctx.phase("digest"):
        for platform in ctx.opt("digest", "platforms", ["slack"]):
            if not ctx.has(*DIGEST_TRANSPORTS[platform]):
                err(f"no {platform} transport ({'+'.join(DIGEST_TRANSPORTS[platform])}): skipping its digest")
                continue
            ctx.sh(["dt-cloud", "digest", "-T", ctx.opt("digest", "template"), "-C", ctx.opt("digest", "config"), "-P", platform,
                    *ctx.opt("digest", "args", []), "-r", _digest_root(ctx)], fatal=False,
                   warn=f"{platform} usage-digest step failed",
                   env={"CLOUDFLARE_API_TOKEN": ctx.env["CF_PAGES_TOKEN"]} if ctx.has("CF_PAGES_TOKEN") else None)


def _digest_root(ctx: Ctx) -> str:
    """The digest's snapshots root: `options.digest.root` (`{snapshots}` filled), else the data store's."""
    root = ctx.opt("digest", "root", None)
    return root.format(snapshots=ctx.snapshots) if root else f"{ctx.data_uri}/{ctx.snapshots}"


#: Where the daily usage snapshots go, under the data store (and the R2 archive): one key space per deployment, beside
#: the scans' `snapshots/<subdir>/` (`usage` is never a scan id).
USAGE_PREFIX = "snapshots/usage"


@step("cf-usage", core=False, soft=True, options={"config": str, "days": int},
      requires=primary_only("cf-usage", "the usage snapshots are the deployment's, written once by its primary"))
def cf_usage(ctx: Ctx, arg: str | None) -> None:
    """The deployment's Cloudflare usage per day (`dt-cloud cf-usage -o`, specs/usage-metrics.md): `<data>/snapshots/
    usage/<date>.json` (+ the R2 archive's copy, which the site's `/admin/usage` reads under `SERVED_R2`) for each of
    the `options.cf-usage.days` (7) whole UTC days before today that a copy lacks: a missed day is backfilled the next
    run. `options.cf-usage.config`: the deployment's Pages wrangler config in the image (`site/wrangler.toml`). Skipped
    without `CF_ANALYTICS_TOKEN` (Account Analytics: Read; the profile's `secrets`) or on REPROC; never fatal. Not core:
    a deployment lists it in `steps` once its token is a secret (a core step would need every store profile to skip it)."""
    if ctx.reproc or not ctx.has("CF_ANALYTICS_TOKEN"):
        err("no CF_ANALYTICS_TOKEN, or REPROC: skipping the Cloudflare usage snapshots")
        return
    with ctx.phase("cf-usage"):
        outs = [f"{ctx.data_uri}/{USAGE_PREFIX}"]
        if ctx.env.get("R2_ARCHIVE"):
            outs.append(f"{ctx.env['R2_ARCHIVE']}/{USAGE_PREFIX}")
        ctx.sh(["dt-cloud", "cf-usage", "-c", ctx.opt("cf-usage", "config", "site/wrangler.toml"),
                "-d", str(ctx.opt("cf-usage", "days", 7)), *(a for o in outs for a in ("-o", o))],
               fatal=False, warn="cf-usage snapshots failed (the scan's outputs stand; the next run backfills)")


#: Where the daily GCP cost snapshots go, beside the usage ones: one key space per cloud, `snapshots/cost/<cloud>/`
#: (`cost` is never a scan id).
GCP_COST_PREFIX = "snapshots/cost/gcp"


def deployment_selector(env: Mapping[str, str]) -> str | None:
    """`$DISKY_LABELS`' `app` and `deployment` as a Batch job selector (every job the deployment submits carries
    them, `cost_labels`); None when it doesn't name both."""
    from .cost_labels import REQUIRED, env_labels

    labels = env_labels(dict(env))
    return ",".join(f"{k}={labels[k]}" for k in REQUIRED) if all(labels.get(k) for k in REQUIRED) else None


@step("gcp-cost", core=False, soft=True,
      options={"buckets": list, "bucket-labels": str, "job-labels": str, "locations": list, "project": str,
               "window": str, "horizons": str, "prices": str},
      requires=primary_only("gcp-cost", "the cost snapshots are the deployment's, written once by its primary"))
def gcp_cost(ctx: Ctx, arg: str | None) -> None:
    """The deployment's GCP storage and Batch compute cost, now and projected (`dt-cloud cost -o`): today's
    `<data>/snapshots/cost/gcp/<date>.json` (+ the R2 archive's copy, which the site's `/admin/cost/gcp` reads under
    `SERVED_R2`); a second run the same day replaces it. Storage: `options.gcp-cost.buckets` (`-b`) and / or
    `bucket-labels` (`-l`); compute: `job-labels` (`-J`), else `$DISKY_LABELS`' `app` + `deployment`. `locations`
    (`-L`), `project` (`-p`; default `$GCP_PROJECT`, else ADC's), `window` (30d), `horizons` (1y,2y), `prices` (a
    YAML in the image, `-P`). Skipped with nothing to measure (no GCP: e.g. the r2 demo) or on REPROC; never fatal.
    Not core, as `cf-usage`."""
    if ctx.reproc:
        err("REPROC: skipping the GCP cost snapshot")
        return
    buckets = ctx.opt("gcp-cost", "buckets", [])
    bucket_labels = ctx.opt("gcp-cost", "bucket-labels", None)
    job_labels = ctx.opt("gcp-cost", "job-labels", None) or deployment_selector(ctx.env)
    if not (buckets or bucket_labels or job_labels):
        err("gcp-cost: nothing to measure (options.gcp-cost.buckets / bucket-labels / job-labels, or $DISKY_LABELS): "
            "skipping the GCP cost snapshot")
        return
    with ctx.phase("gcp-cost"):
        outs = [f"{ctx.data_uri}/{GCP_COST_PREFIX}"]
        if ctx.env.get("R2_ARCHIVE"):
            outs.append(f"{ctx.env['R2_ARCHIVE']}/{GCP_COST_PREFIX}")
        argv = ["dt-cloud", "cost", "-n", ctx.profile.deployment,
                "-w", ctx.opt("gcp-cost", "window", "30d"), "-H", ctx.opt("gcp-cost", "horizons", "1y,2y")]
        if project := ctx.opt("gcp-cost", "project", None):
            argv += ["-p", project]
        argv += [a for b in buckets for a in ("-b", b)]
        if bucket_labels:
            argv += ["-l", bucket_labels]
        if job_labels:
            argv += ["-J", job_labels]
        argv += [a for loc in ctx.opt("gcp-cost", "locations", []) for a in ("-L", loc)]
        if prices := ctx.opt("gcp-cost", "prices", None):
            argv += ["-P", prices]
        ctx.sh([*argv, *(a for o in outs for a in ("-o", o))],
               fatal=False, warn="gcp-cost snapshot failed (the scan's outputs stand; the next run writes its day)")


#: Core steps a profile may `skip`.
SKIPPABLE = tuple(n for n, s in STEPS.items() if s.core and s.skippable)


# The environment.


def setup_env(profile: ScanProfile, env: dict[str, str]) -> None:
    """Fill `env` in place: the profile's `env` under what's already set (a job spec's own wins), the layout's
    variables, `SCAN_ID`, secrets whitespace-stripped (a Secret Manager version made with `--data-file=-` carries a
    newline), REPROC's record switch, and the R2 archive's credentials profile."""
    for k, v in profile.env.items():
        env.setdefault(k, v)
    env["LAYER2_PREFIX"] = profile.layout.layer2
    env["SNAPSHOTS_SUBDIR"] = profile.layout.snapshots
    if profile.data_bucket:
        env.setdefault("DATA_BUCKET", profile.data_bucket)
    env["SCAN_ID"] = env["SNAP_ID"]
    for n in sorted(profile.secret_names()):
        if n in env:
            env[n] = env[n].strip()
    if env.get("REPROC") == "1":
        env["DT_RECORD"] = "off"
    r2_profile(env)


def r2_profile(env: dict[str, str], home: Path | None = None) -> None:
    """The R2 archive through blobfs (`R2_ARCHIVE=r2://$R2_BUCKET`): R2's own credentials profile (`r2`) + a
    `buckets.yml` entry with its endpoint, so a source's `AWS_*` keys (another provider's) stay the lister's. Never
    `DISKY_R2_ENDPOINT_URL`: it would override every bucket's endpoint."""
    if not all(env.get(k) for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID")):
        return
    creds = Path(env.get("TMPDIR") or "/tmp") / "r2-credentials"
    old = os.umask(0o077)
    try:
        creds.write_text(f"[r2]\naws_access_key_id = {env['R2_ACCESS_KEY_ID']}\naws_secret_access_key = {env.get('R2_SECRET_ACCESS_KEY', '')}\n")
    finally:
        os.umask(old)
    env["AWS_SHARED_CREDENTIALS_FILE"] = str(creds)
    cfg = (home or Path(env.get("HOME") or Path.home())) / ".config" / "disk-tree"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "buckets.yml").write_text(f"buckets:\n  - uri: r2://{env['R2_BUCKET']}\n    endpoint_url: {env['R2_ENDPOINT']}\n    profile: r2\n")
    env["R2_ARCHIVE"] = f"r2://{env['R2_BUCKET']}"


#: The prefix of a base64-encoded `$DT_SCAN_PROFILE_JSON` (`profile_env_value`).
PROFILE_B64 = "base64:"


def profile_env_value(profile_json: str) -> str:
    """The head's `$DT_SCAN_PROFILE_JSON` for its tasks: base64, since Nextflow writes an `env` scope value into each
    task's script as `export NAME="<value>"` with inner double quotes unescaped (raw JSON reached the task as
    `{data:…}`); base64's alphabet means the same inside double quotes."""
    import base64

    return PROFILE_B64 + base64.b64encode(profile_json.encode()).decode()


def profile_env_json(env: Mapping[str, str]) -> str:
    """`$DT_SCAN_PROFILE_JSON` as JSON: decoded when the head encoded it (`profile_env_value`), as is when set by hand."""
    import base64

    v = env[PROFILE_JSON_ENV]
    return base64.b64decode(v.removeprefix(PROFILE_B64), validate=True).decode() if v.startswith(PROFILE_B64) else v


def load_profile_env(env: Mapping[str, str], path: str | None = None) -> ScanProfile:
    from . import scan_profile

    if path:
        return scan_profile.load(path)
    if env.get(PROFILE_JSON_ENV):
        return scan_profile.parse(json.loads(profile_env_json(env)), where=f"${PROFILE_JSON_ENV}")
    if env.get(PROFILE_PATH_ENV):
        return scan_profile.load(env[PROFILE_PATH_ENV])
    raise StepFailed(2, f"no profile: pass -p FILE, or set ${PROFILE_JSON_ENV} (the head does) or ${PROFILE_PATH_ENV}")


def make_ctx(profile: ScanProfile, env: dict[str, str]) -> Ctx:
    missing = [k for k in IDENTITY if not env.get(k)]
    if missing:
        raise StepFailed(2, f"the scan's identity is stamped by the head: {', '.join('$' + k for k in missing)} unset")
    setup_env(profile, env)
    from .scan_runs import PROFILE_ENV, JobRun

    run = JobRun(None, None, False, env.get("DT_RUN_ID") or None, None, env=env) if env.get(PROFILE_ENV) else _Printer(env)
    if isinstance(run, JobRun) and not profile.primary:
        # a task's phases write the head's run row, whose store a write sets (upsert): the scan profile's, not the
        # scan-runs profile's default
        from dataclasses import replace

        run.profile = replace(run.profile, store=profile.store)
    work = Path(env.get("TMPDIR") or "/tmp") / "dt-scan" / env["SNAP_ID"]
    work.mkdir(parents=True, exist_ok=True)
    return Ctx(profile, env["SNAP_ID"], env["GEN"], env, work, run)


def run_step(name: str, arg: str | None, env: dict[str, str] | None = None, profile_path: str | None = None) -> int:
    """`dt-cloud scan step NAME [ARG]`'s body: the exit code."""
    env = dict(os.environ) if env is None else env
    s = STEPS.get(name)
    if s is None:
        err(f"scan step: unknown step {name!r} (one of {', '.join(STEPS)})")
        return 2
    if s.arg and not arg:
        err(f"scan step {name}: needs its argument (a source URI)")
        return 2
    if env.get(FAIL_ENV) == name:
        err(f"scan step {name}: failing ({FAIL_ENV})")
        return 1
    if env.get(FAKE_ENV):
        with open(env[FAKE_ENV], "a") as f:
            f.write(f"{name} {arg or ''}".rstrip() + "\n")
        return 0
    try:
        profile = load_profile_env(env, profile_path)
        ctx = make_ctx(profile, env)
    except StepFailed as e:
        err(f"scan step {name}: {e}")
        return e.rc or 1
    try:
        s.fn(ctx, arg)
        return 0
    except StepFailed as e:
        err(f"scan step {name}: {e}")
        rc = e.rc or 1
        if e.hard:
            return rc
    except Exception as e:  # noqa: BLE001 — a soft step's crash is a warning
        if not s.soft:
            raise
        err(f"scan step {name}: {type(e).__name__}: {e}")
        rc = 1
    if s.soft:
        err(f"WARN: {name} failed (exit {rc}); soft step, the scan goes on")
        return 0
    return rc
