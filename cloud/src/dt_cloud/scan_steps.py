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
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

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
    sh_impl: Callable[..., subprocess.CompletedProcess] = subprocess.run
    #: The open phase's first `fails_phase` failure (exit code, warning): the phase ends failed with a warn alert
    failure: tuple[int, str] | None = None

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
        return len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1

    def expand(self, args: Sequence[str], **extra: object) -> list[str]:
        """`args` with `{nproc}`, `{scan}`, `{date}`, `{gen}`, `{work}`, `{data}` (the data store's local root,
        `data_root`) (+ `extra`) filled; an arg that is exactly `{<key>}` of a list value expands to its items."""
        vals: dict[str, object] = dict(nproc=self.nproc, scan=self.scan, date=self.date, gen=self.gen, work=self.work, **extra)
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
           capture: bool = False, fails_phase: bool = False) -> str | None:
        """Run `argv` (logged as `+ …`; secrets only ever in the env). A failure raises `StepFailed` when `fatal`, else
        prints `WARN: <warn>` and returns None; returns stdout when `capture`, else "". `fails_phase`: a non-fatal failure
        still ends the open phase failed (warn-alerted, never failing the run), so /scans doesn't show it done."""
        argv = [str(a) for a in argv]
        err("+ " + " ".join(argv))
        r = self.sh_impl(argv, env={**self.env, **(env or {})}, stdout=subprocess.PIPE if capture else None, text=True)
        if r.returncode:
            if fatal:
                raise StepFailed(r.returncode, f"exit {r.returncode}: {' '.join(argv[:3])}")
            what = warn or " ".join(argv[:3]) + " failed"
            err(f"WARN: {what} (exit {r.returncode})")
            if fails_phase and self.failure is None:
                self.failure = (r.returncode, what)
            return None
        return r.stdout if capture else ""

    @contextmanager
    def phase(self, name: str, note: str | None = None) -> Iterator[None]:
        """A `scan_runs` phase: begun, then ended (failed, with the exit code, when the body raises; failed with a warn
        alert after a `fails_phase` command's failure)."""
        self.run.begin(name)
        self.failure = None
        try:
            yield
        except StepFailed as e:
            self.run.end(name, e.rc or 1)
            raise
        except BaseException:
            self.run.end(name, 1)
            raise
        finally:
            failure, self.failure = self.failure, None
        if failure:
            self.run.end(name, failure[0], warn=True, msg=failure[1], note=note)
        else:
            self.run.end(name, 0, note=note)

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
        text = " ".join(found)
        err(f"WARN: unscanned-bucket check: {text}")
        ctx.sh(["disk-tree", "notify", "-t", f"⚠️ {ctx.profile.deployment} {ctx.scan}",
                f"non-empty bucket(s) not in the profile's sources: `{text}`"], fatal=False, warn="unscanned-bucket alert not sent")


@step("access-ingest", core=False, stage="pre", soft=True, options={"args": list})
def access_ingest(ctx: Ctx, arg: str | None) -> None:
    """The buckets' access logs → the read-recency shards (`dt-cloud access ingest`, incremental and watermarked: its
    settings in the env), beside `list`. Soft: `index` runs without the newest shards, and the next run catches up."""
    if ctx.reproc:
        err("access-ingest off under REPROC")
        return
    with ctx.phase("access-ingest"):
        ctx.sh(["dt-cloud", "access", "ingest", *ctx.opt("access-ingest", "args", [])], fatal=False,
               warn="access ingest failed (read recency lags until the next run's ingest)")


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
            "-l", f"{listing}/shard-*.parquet", "-b", bucket], env={"DISK_TREE_ROOT": str(root)})
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
                *(["-E", src.endpoint] if src.endpoint else []), "-o", ctx.listing_out(b),
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
        if urlparse(p.data).scheme not in ("gs", "gcs"):
            problems.append("data: a gs:// bucket with options.index.mode path-index (its inputs are staged from GCS)")
        return problems
    return [f"options.index.{k}: only with mode path-index" + (" (index-write writes the age pyramids itself)" if k == "age_pyramids" else "")
            for k in ("attribution", "access", "rules", "age_pyramids", "verify") if k in o]


@step("index", skippable=False, stage="index", requires=_index_check,
      options={"mode": str, "webdata": list, "lifecycle": bool, "mem": str, "threads": int, "args": list,
               "attribution": list, "access": str, "rules": bool, "age_pyramids": bool, "verify": bool})
def index(ctx: Ctx, arg: str | None) -> None:
    """Every source's layer-2 (imported here from the listings unless `list` did), the snapshot JSONs
    (`options.index.webdata`: the deployment's command, `{web}` its out dir, `{sources}` the `<bucket>=<layer-2>` args),
    published to `snapshots/<subdir>/<scan>/` (+ `lifecycle.json`), and the index tiers (`dt-cloud index-write`) under
    `<layer2>/index/<gen>/`, `path-index.parquet` copied last: the skip-if-done mark. `options.index.mode: path-index`
    builds both from the listings in one pass instead (`path_index`; with `age_pyramids` and `verify`).
    `options.index.mem`: DuckDB's memory limit (index-write's; under path-index, the age pyramids' and the check's)."""
    if ctx.opt("index", "mode", "layer2") == "path-index":
        path_index(ctx)
        return
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
            ctx.sh(["dt-cloud", "rules", "-o", rules], fatal=False, warn="attribution rules have findings")
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
    `bysize` sort, where written) and writes `verification.json` beside the tiers; a mismatch fails the step."""
    store = ctx.data_uri.split("://", 1)[1]  # `<bucket>[/<prefix>]`
    key_dir = ctx.data_root / ctx.index_key
    for s in ctx.profile.sources:
        ctx.need(ctx.listing_dir(s.bucket) / "_SUCCESS.json", "list")
    if ctx.done(key_dir / "path-index.parquet", "index tiers") and ctx.done(ctx.data_root / ctx.snapshots / ctx.scan / "meta.json", "snapshot"):
        return
    gcs = lambda key: f"/gcs/{store}/{key}"  # noqa: E731 — `dt-cloud stage`'s glob form
    lglobs = [gcs(f"{ctx.listing_key(s.bucket)}/*.parquet") for s in ctx.profile.sources]
    attr = [gcs(a) for a in ctx.opt("index", "attribution", [])]
    access = ctx.opt("index", "access")
    xglobs = [gcs(access)] if access and next(iter(ctx.data_root.glob(access)), None) else []
    if access and not xglobs:
        err(f"no access shards at {access}: the index runs without read recency")
    stage = ctx.work / "stage"
    local = lambda g: str(stage / g.removeprefix("/gcs/"))  # noqa: E731
    pi = ctx.work / "path-index"
    snap = ctx.work / "snap"
    pi.mkdir(parents=True, exist_ok=True)
    with ctx.phase("path-index"):
        ctx.sh(["dt-cloud", "stage", "-o", stage, *lglobs, *[g.replace("*.parquet", "_SUCCESS.json") for g in lglobs], *attr, *xglobs])
        tmp = stage / ".duckdb-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        ctx.sh(["dt-cloud", "path-index", "-d", ctx.scan, *[a for g in lglobs for a in ("-l", local(g))],
                *[a for g in attr for a in ("-a", local(g))], *[a for g in xglobs for a in ("-x", local(g))],
                "-o", snap, "-c", ctx.data_root / ctx.listing_key("dir-cache"), "-P", pi / "path-index.parquet",
                *ctx.opt("index", "args", [])], env={"DUCKDB_TMP": str(tmp)})
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
        upload(files, f"{ctx.data_uri}/{ctx.index_key}")
    _publish(ctx, snap)


def _static_check(p: ScanProfile) -> list[str]:
    o = p.opts("static-names")
    if o.get("gen"):
        return [f"options.static-names.{k}: not with gen (the one-machine path)" for k in ("profile",) if k in o]
    problems = [f"options.static-names.{k}: only with gen (the one-machine path)" for k in ("store", "mem", "build_args") if k in o]
    if not o.get("profile") and not p.env.get("STATIC_NAMES_PROFILE"):
        problems.append("options.static-names.profile: required (the deployment's static-names profile; or env STATIC_NAMES_PROFILE, "
                        "or gen: the one-machine path, or skip: [static-names])")
    return problems


@step("static-names", stage="serve", soft=True, options={"profile": str, "gen": str, "store": str, "mem": str, "build_args": list},
      requires=_static_check)
def static_names(ctx: Ctx, arg: str | None) -> None:
    """Append this scan (and any earlier one still pending, in scan-id order) to the static name index as a run
    (`static-names runs add -c`: its stages are Batch jobs of their own; idempotent). Runs beside publish-r2 →
    interval-store (an `overlapped` phase); a failure still lists the scan, a later run's `-c` catches it up.

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
        args = ["-g", gen, "-L", layout, "-s", ctx.data_uri, "-o", store, "-w", ctx.work / "static-names", "-p", ctx.nproc,
                *(["-M", ctx.opt("static-names", "mem")] if ctx.opt("static-names", "mem") else [])]
        with ctx.phase("static-names", note="overlapped"):
            sub = (["add", *args] if ctx.store_has(store, f"static-names/{gen}/scans.json")
                   else ["build", *args, *ctx.opt("static-names", "build_args", [])])
            ctx.sh(["dt-cloud", "static-names", *sub], fatal=False, warn=f"static-names {sub[0]} failed for {ctx.scan}")
        return
    prof = ctx.opt("static-names", "profile") or ctx.env["STATIC_NAMES_PROFILE"]
    with ctx.phase("static-names", note="overlapped"):
        ctx.sh(["dt-cloud", "static-names", "runs", "add", "-c", ctx.scan], env={"STATIC_NAMES_PROFILE": str(prof)},
               fatal=False, warn=f"static-names runs add failed for {ctx.scan}")


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
        runs = [["dt-cloud", "publish-r2", *ctx.opt("publish-r2", "args", []), "-a", *req, ctx.scan]]
        runs += [["dt-cloud", "publish-r2", "-a", "-p", k, "-R", k, ctx.scan] for k in ctx.opt("publish-r2", "keys", [])]
        for argv in runs:
            if ctx.sh(argv, fatal=False, warn=f"publish-r2 failed for {ctx.scan} (the site keeps serving the data store)") is None and barrier:
                raise StepFailed(1, f"publish-r2 failed for {ctx.scan}: D1 pointers were not advanced, so the previous scan "
                                    "remains visible; repair the R2 copy, then resume", hard=True)


@step("interval-store", stage="serve", soft=True, options={"store": str, "timeout": str, "mem": str, "profile": str, "merge_wait": int},
      requires=primary_only("interval-store", "the site serves another store's scans per scan, and the interval store's "
                                              "`interval-store/CURRENT.json` is the primary's"))
def interval_store(ctx: Ctx, arg: str | None) -> None:
    """Append this scan (and any pending) to the change-interval path store beside its current generation, then the
    due carries / compaction (`interval-store add`). Never fatal (`-W`): a failure or the timeout warns, the next run
    catches up. Reads the scans' `path` sorts at `<layer2>/index/<gen>/path-index.parquet` from the store.
    `options.interval-store.profile`: the staged path instead (`interval-store append -c`, its per-range stages Batch
    jobs of their own, in this task's image; `merge_wait` seconds on the merge job), for a store too big for one task."""
    if ctx.reproc:
        err(f"interval-store: not appending {ctx.scan} (REPROC)")
        return
    from .scan_runs import duration, run_grouped

    timeout = ctx.opt("interval-store", "timeout")
    if ctx.opt("interval-store", "profile"):
        cmd = ["dt-cloud", "interval-store", "append", "-c", "-w", str(ctx.opt("interval-store", "merge_wait", 0)), ctx.scan]
        env = {"INTERVAL_STORE_PROFILE": str(ctx.opt("interval-store", "profile"))}
        if ctx.env.get("JOB_IMAGE"):
            env["INTERVAL_STORE_IMAGE"] = ctx.env["JOB_IMAGE"]
        ctx.run.begin("interval-store")
        err("+ " + " ".join(cmd))
        ran = run_grouped(cmd, duration(timeout) if timeout else None, 30.0, env={**ctx.env, **env})
        ctx.run.end("interval-store", ran.rc, warn=True, msg="the scan reads per-scan until a later run appends it",
                    timed_out=timeout if ran.timed_out else None, terminated=ran.signal if ran.rc else None)
        return
    store = ctx.opt("interval-store", "store") or ctx.env.get("R2_ARCHIVE")
    if not store:
        err(f"interval-store: not appending {ctx.scan} (no store: options.interval-store.store or R2)")
        return
    cur = ctx.sh(["dt-cloud", "interval-store", "current", "-o", store], fatal=False, capture=True)
    if cur is None:
        err(f"WARN: interval-store current failed: not appending {ctx.scan}")
        return
    if not cur.strip():
        err(f"no interval store at {store} yet: not appending {ctx.scan}")
        return
    layout = ctx.profile.layout.layer2.replace("{scan}", "{id}") + "index/{gen}/path-index.parquet"
    cmd = ["dt-cloud", "interval-store", "add", "-L", layout, "-s", store, "-o", store, "-w", str(ctx.work / "interval-store"),
           *(["-M", ctx.opt("interval-store", "mem")] if ctx.opt("interval-store", "mem") else []), "-p", str(ctx.nproc)]
    ctx.run.begin("interval-store")
    err("+ " + " ".join(cmd))
    ran = run_grouped(cmd, duration(timeout) if timeout else None, 30.0, env=ctx.env)
    ctx.run.end("interval-store", ran.rc, warn=True, msg="the scan reads per-scan until a later run appends it",
                timed_out=timeout if ran.timed_out else None, terminated=ran.signal if ran.rc else None)


def _sync_args(ctx: Ctx) -> list[str]:
    """Both halves' arguments: the generation, the store, and `options.index-sync.variants` (`-v` each; default every
    variant the generation holds)."""
    return [*store_args(ctx), "-d", str(ctx.data_root / ctx.index_key), "-g", ctx.gen, "-k", ctx.index_key,
            *[a for v in ctx.opt("index-sync", "variants", []) for a in ("-v", v)]]


def _sync_check(p: ScanProfile) -> list[str]:
    from .index_footer import INDEX_VARIANTS

    return [f"options.index-sync.variants: {v!r} is not an index variant (one of {', '.join(INDEX_VARIANTS)})"
            for v in p.opts("index-sync").get("variants", []) if v not in INDEX_VARIANTS]


@step("index-rows", skippable=False, stage="serve", soft=True)
def index_rows(ctx: Ctx, arg: str | None) -> None:
    """The index tiers' row groups to D1 (`index-sync -r`), invisible until `index-sync` flips the pointers: overlaps
    static-names and interval-store. Re-runnable (INSERT OR REPLACE)."""
    with ctx.phase("index-rows"):
        if not ctx.has(*CF):
            err("WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID: rows not uploaded")
            return
        ctx.sh(["dt-cloud", "index-sync", "-r", *_sync_args(ctx), ctx.scan], fatal=False, warn="index-sync -r failed (the pointer flip will refuse)")


@step("index-sync", skippable=False, stage="sync", soft=True, options={"retain": int, "variants": list}, requires=_sync_check)
def index_sync(ctx: Ctx, arg: str | None) -> None:
    """List the scan: flip its pointers (`index-sync -p`, which checks the rows `index-rows` uploaded), last of the
    serving steps, so a listed scan is whole (searchable, on R2, in the interval store); then drop superseded
    generations' D1 rows (`index-gc <scan>`; `options.index-sync.retain` N: also retire the row groups of every scan
    older than the newest N, read from their cold footer files after, D1's size cap). `options.index-sync.variants`:
    only those variants, in both halves (`index-rows` reads them here too)."""
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
                     capture=True, fatal=False, warn="over-time groups failed (the series keeps its per-scan reads)")
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


def _snap_root(ctx: Ctx) -> str:
    return f"{ctx.env.get('R2_ARCHIVE') or ctx.data_uri}/{ctx.snapshots}"


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
    """Replay the site's page loads uncached (`probe -c`): one cold-latency record per scan under `probes/<subdir>/`."""
    with ctx.phase("probe"):
        if ctx.reproc or not ctx.has("SITE_TOKEN", "SITE_URL"):
            return
        sub = ctx.profile.layout.snapshots
        ctx.sh(["dt-cloud", "probe", "-c", "-u", ctx.env["SITE_URL"], "-o", f"{ctx.data_uri}/probes/{sub + '/' if sub else ''}"],
               fatal=False, warn=f"serving probe failed for {ctx.scan} (a 5xx or a dropped request; data is fine)")


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
                   warn=f"{platform} usage-digest step failed", fails_phase=True,
                   env={"CLOUDFLARE_API_TOKEN": ctx.env["CF_PAGES_TOKEN"]} if ctx.has("CF_PAGES_TOKEN") else None)


def _digest_root(ctx: Ctx) -> str:
    """The digest's snapshots root: `options.digest.root` (`{snapshots}` filled), else the data store's."""
    root = ctx.opt("digest", "root", None)
    return root.format(snapshots=ctx.snapshots) if root else f"{ctx.data_uri}/{ctx.snapshots}"


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
    `DISK_TREE_R2_ENDPOINT_URL`: it would override every bucket's endpoint."""
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


def load_profile_env(env: Mapping[str, str], path: str | None = None) -> ScanProfile:
    from . import scan_profile

    if path:
        return scan_profile.load(path)
    if env.get(PROFILE_JSON_ENV):
        return scan_profile.parse(json.loads(env[PROFILE_JSON_ENV]), where=f"${PROFILE_JSON_ENV}")
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
