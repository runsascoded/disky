"""Scan runs (specs/done/scan-runs-ui.md): what each scan job did — when it ran, its phases, what it wrote — recorded
in the site's D1 (`scan_runs`, `scan_run_phases`, `scan_run_outputs`; migration `0016_scan_runs.sql` in the cw
lineage) and shown at `/scans`.

Two writers share one record shape and one SQL path:
- `record`: the job itself, incrementally — `-S` at start, `-P <phase>` as each phase ends (measuring the outputs
  the profile ties to it), `-x <rc>` from its exit trap — so a failed run still leaves its record.
- `backfill` (`scan_runs_backfill`): past runs, reconstructed from the Batch API, Cloud Logging's phase markers and
  the data bucket.

Nothing here assumes a deployment: the job kinds, markers, outputs and prices come from a profile (`Profile`), a JSON
file named by `-c` / `$SCAN_RUNS_PROFILE`, which each deployment branch checks in.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from functools import partial
from pathlib import Path
from typing import Protocol

from .d1_schema import table_ddl
from .scan_id import check_scan_id

err = partial(print, file=sys.stderr)

PROFILE_ENV = "SCAN_RUNS_PROFILE"
TABLES = ("scan_runs", "scan_run_phases", "scan_run_outputs")
STATUSES = ("running", "succeeded", "failed", "nop")


# ── The record ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Run:
    run_id: str
    scan: str
    kind: str
    status: str
    source: str = "job"
    store: str = "primary"
    parent: str | None = None
    failed_phase: str | None = None
    error: str | None = None
    started_ts: int | None = None
    finished_ts: int | None = None
    job_name: str | None = None
    region: str | None = None
    machine: str | None = None
    spot: int | None = None
    tasks: int | None = None
    image: str | None = None
    cost_usd: float | None = None
    gen: str | None = None


@dataclass(frozen=True)
class Phase:
    phase: str
    seq: int
    started_ts: int | None
    finished_ts: int | None
    status: str = "done"
    note: str | None = None


def phase_status(note: str | None) -> str:
    """Live notes and legacy `wall, failed …` markers carry nonfatal phase failures."""
    return "failed" if re.search(r"(?:^|,\s*)failed\b", note or "", re.IGNORECASE) else "done"


@dataclass(frozen=True)
class Output:
    key: str
    uri: str
    bytes: int | None = None
    objects: int | None = None
    rows: int | None = None
    gen: str | None = None


@dataclass(frozen=True)
class Record:
    """One run as written: the row, its phases, its outputs."""
    run: Run
    phases: tuple[Phase, ...] = ()
    outputs: tuple[Output, ...] = ()


def lit(v: object) -> str:
    """A SQL literal (the D1 HTTP API takes one SQL string; values are inlined, quoted)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(round(v, 4))
    return "'" + str(v).replace("'", "''") + "'"


# Columns an upsert never clears: a later partial write (a phase, the exit trap) leaves what it doesn't name alone.
_KEEP = ("parent", "failed_phase", "error", "started_ts", "finished_ts", "job_name", "region", "machine", "spot", "tasks",
         "image", "cost_usd", "gen")


def run_sql(run: Run, now: int) -> str:
    """Upsert a run row. Never `INSERT OR REPLACE` (it deletes the row, which the children's FKs refuse): on conflict
    the status, kind and scan move to this write's, a NULL field keeps the stored value, and `source` keeps the first
    writer's (a backfill completing a job-written run stays `job`)."""
    if run.status not in STATUSES:
        raise ValueError(f"scan run {run.run_id}: status {run.status!r} not one of {STATUSES}")
    cols = [f.name for f in fields(Run)] + ["updated_ts"]
    vals = [getattr(run, c) for c in cols[:-1]] + [now]
    sets = ["scan = excluded.scan", "kind = excluded.kind", "status = excluded.status", "store = excluded.store",
            "updated_ts = excluded.updated_ts"]
    sets += [f"{c} = COALESCE(excluded.{c}, scan_runs.{c})" for c in _KEEP]
    return (f"INSERT INTO scan_runs ({', '.join(cols)}) VALUES ({', '.join(lit(v) for v in vals)}) "
            f"ON CONFLICT(run_id) DO UPDATE SET {', '.join(sets)}")


def phase_sql(run_id: str, p: Phase) -> str:
    cols = ["run_id", "phase", "seq", "started_ts", "finished_ts", "status", "note"]
    vals = [run_id, p.phase, p.seq, p.started_ts, p.finished_ts, p.status, p.note]
    return (f"INSERT INTO scan_run_phases ({', '.join(cols)}) VALUES ({', '.join(lit(v) for v in vals)}) "
            "ON CONFLICT(run_id, phase) DO UPDATE SET seq = excluded.seq, status = excluded.status, "
            "started_ts = COALESCE(excluded.started_ts, scan_run_phases.started_ts), "
            "finished_ts = COALESCE(excluded.finished_ts, scan_run_phases.finished_ts), "
            "note = COALESCE(excluded.note, scan_run_phases.note)")


def output_sql(run_id: str, o: Output) -> str:
    cols = ["run_id", "key", "uri", "bytes", "objects", "rows", "gen"]
    vals = [run_id, o.key, o.uri, o.bytes, o.objects, o.rows, o.gen]
    return (f"INSERT INTO scan_run_outputs ({', '.join(cols)}) VALUES ({', '.join(lit(v) for v in vals)}) "
            "ON CONFLICT(run_id, key) DO UPDATE SET uri = excluded.uri, bytes = excluded.bytes, "
            "objects = excluded.objects, rows = excluded.rows, gen = COALESCE(excluded.gen, scan_run_outputs.gen)")


def record_sql(rec: Record, now: int) -> list[str]:
    """The statements writing `rec`: the run first (its children reference it)."""
    return ([run_sql(rec.run, now)]
            + [phase_sql(rec.run.run_id, p) for p in rec.phases]
            + [output_sql(rec.run.run_id, o) for o in rec.outputs])


# ── Sinks: D1 (the job, the backfill), a local SQLite file (dry runs, tests), stdout (`-n`) ──────────────────


class Sink(Protocol):
    def execute(self, statements: Sequence[str]) -> None: ...

    def query(self, sql: str) -> list[dict]: ...


class D1Sink:
    """The deployment's D1 over the HTTP API (`index_footer._d1_query`: `$D1_DB_ID`, `CLOUDFLARE_API_TOKEN` +
    `CLOUDFLARE_ACCOUNT_ID`), as `index-sync` writes it."""

    def __init__(self, db_id: str | None = None):
        from . import index_footer as f

        self.db_id = db_id or f.D1_DB_ID
        if not self.db_id:
            raise SystemExit("scan-run: no D1 database: set $D1_DB_ID to this deployment's D1 (or pass --sqlite / -n)")
        self._tok, self._acct = f._creds()
        self._q = f._d1_query

    def execute(self, statements: Sequence[str]) -> None:
        for s in statements:
            self._q(s, self._acct, self._tok, self.db_id)

    def query(self, sql: str) -> list[dict]:
        return self._q(sql, self._acct, self._tok, self.db_id)


class SqliteSink:
    """A local SQLite file with foreign keys ON (as D1 enforces them); the tables are created from the D1 schema
    (`site/schema.sql`, the one source of it) when absent."""

    def __init__(self, path: str | Path, ddl: str | None = None):
        self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        if not self.con.execute("SELECT 1 FROM sqlite_master WHERE name = 'scan_runs'").fetchone():
            self.con.executescript(ddl if ddl is not None else table_ddl(TABLES))

    def execute(self, statements: Sequence[str]) -> None:
        with self.con:
            for s in statements:
                self.con.execute(s)

    def query(self, sql: str) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql).fetchall()]


class PrintSink:
    """`-n`: each statement on stdout (`;`-terminated, so the output is a file `wrangler d1 execute --file` runs);
    queries see an empty store."""

    def __init__(self, out=None):
        self.out = out or sys.stdout

    def execute(self, statements: Sequence[str]) -> None:
        for s in statements:
            print(f"{s};", file=self.out)

    def query(self, sql: str) -> list[dict]:
        return []


# ── The profile ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JobKind:
    """A class of Batch jobs: `where` matches a job (every given field must match), `scan` says where its scan id is.

    `where` keys (regexes, `re.search`): `name`, `image`, `entrypoint`, `commands` (joined by spaces); `env` (each
    named var must be set), `no_env` (each must be unset). `scan` sources, tried in order: `{"env": VAR}`,
    `{"commands": REGEX}` / `{"log": REGEX}` (group 1 is the id), `{"started": STRFTIME}` (the task's start, UTC),
    `{"created": STRFTIME}` (the job's creation)."""
    kind: str
    where: Mapping[str, object]
    scan: tuple[Mapping[str, str], ...]
    #: A downstream kind: linked to the orchestrating run of the same scan whose window holds it (`parent`).
    downstream: bool = False


@dataclass(frozen=True)
class OutputSpec:
    """A structure a run writes: `uri` a prefix (ends `/`) or an object, templated by `{scan}`, `{gen}` (the run's index
    generation) and `{static_gen:<profile>}` (that static-names profile's live generation: `<profile>` a JSON file —
    relative to the scan-run profile's own dir, e.g. `{static_gen:static-names.json}` beside `job/scan-runs.json` — or
    an example's name with `STATIC_NAMES_GEN` set).

    - `after`: the phase whose end measures it (`record -P`; unset = the run's successful end); `kinds`: the run kinds that write it (empty = all).
    - `exclude`: sub-prefixes (relative) left out of the total.
    - `split`: `dir` = also one output per immediate subdir (`<key>/<dir>`), `stem` = also one per file stem
      (`<key>/<name up to its first .>`), each a sum of its files.
    - `rows`: `{"json": NAME, "field": F}` = a split part's rows are field F of its JSON file NAME (a listing's
      `_SUCCESS.json` `objects`); `"groups"` = a stem's rows are the last `row_end` of its `<stem>.groups.parquet`.
    - `manifest`: the uri is a glob for a static-names-style manifest JSON: `<key>` totals its `runs[]` (`objects` =
      the run count), each of which is also a `<key>/<run key>` output (its own rows/bytes); `gen` is its generation."""
    key: str
    uri: str
    after: str | None = None
    kinds: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    split: str | None = None
    rows: object = None
    manifest: bool = False


@dataclass(frozen=True)
class Profile:
    store: str = "primary"
    project: str | None = None
    regions: tuple[str, ...] = ()
    jobs: tuple[JobKind, ...] = ()
    outputs: tuple[OutputSpec, ...] = ()
    #: A phase marker in the task log: groups `name` and (optional) `note` (`PHASE <name>: <s>s (<note>)`).
    phase_marker: str = r"^PHASE (?P<name>[^:]+): -?\d+s(?: \((?P<note>[^)]*)\))?"
    phase_start_marker: str = r"^PHASE-START (?P<name>[^:]+): \d+s"
    #: Markers that are not phases (a final total).
    phase_ignore: tuple[str, ...] = ("total",)
    #: Phases that run beside the others (from the start, or from `record -b`; their marker is where they joined), by name.
    overlapped: tuple[str, ...] = ()
    #: Phases that run beside each other after the serial phase before them: each starts where the last phase
    #: outside this set (and `overlapped`) ended, and the next serial phase starts where the last of them ended.
    concurrent: tuple[str, ...] = ()
    #: A log line meaning the run did nothing (an already-published scan): group 1 the scan id.
    nop_marker: str | None = None
    #: Log lines naming a failure (the ERR trap's xtrace), groups `rc`, `line`, `cmd`; the first that matches wins. The
    #: profile may give one regex or a list (a job whose trap changed shape keeps the old one for its older logs).
    error_marker: tuple[str, ...] = ()
    #: On-demand $/h by machine type; `spot_factor` scales it for SPOT VMs. A machine not here has no cost estimate.
    prices: Mapping[str, float] = field(default_factory=dict)
    spot_factor: float | None = None
    #: How far back the backfill reads (Cloud Logging keeps 30 d by default; Batch keeps its jobs).
    log_days: int = 30
    #: The env vars `record` reads the run id from when `-r` isn't given; the first one set wins (the job's own id
    #: under its scheduler).
    run_id_env: tuple[str, ...] = ("BATCH_JOB_UID",)
    #: Phases that bill no compute (a run that starts before its job does: the capture it ingests, its queue wait).
    #: A run's cost at `record -x` leaves them out.
    unbilled: tuple[str, ...] = ()
    #: The job wrapper's alerts (specs/scan-job-wrapper.md): `label` names the deployment in each alert; `logs_url` is
    #: a template over the run's job facts (`{job_uid}`, `{project}`, `{region}`, `{job}`, `{scan}`, `{kind}`) linked
    #: from each alert, and an alert whose run lacks a fact it names carries no link.
    alerts: Mapping[str, str] = field(default_factory=dict)
    #: Env vars whose values never leave the job: each is replaced by `<NAME>` in any alert, note or error text.
    secrets: tuple[str, ...] = ()

    def kind(self, name: str) -> JobKind | None:
        return next((k for k in self.jobs if k.kind == name), None)


def load_profile(path: str | Path | None = None) -> Profile:
    """The profile JSON at `path` (else `$SCAN_RUNS_PROFILE`). No defaults: a deployment names its own."""
    path = path or os.environ.get(PROFILE_ENV)
    if not path:
        raise SystemExit(f"scan-run: no profile: pass -c FILE or set ${PROFILE_ENV} (the deployment's job/scan-runs.json)")
    return parse_profile(json.loads(Path(path).read_text()), base=Path(path).parent)


def parse_profile(d: Mapping, base: Path | None = None) -> Profile:
    """A profile from its JSON. `base`: the file's dir, which a relative `{static_gen:<file>}` resolves against."""
    known = {f.name for f in fields(Profile)}
    if bad := sorted(set(d) - known - {"name", "comment"}):
        raise ValueError(f"scan-run profile: unknown fields {bad}")
    kw = {k: v for k, v in d.items() if k in known}
    kw["regions"] = tuple(d.get("regions", ()))
    kw["phase_ignore"] = tuple(d.get("phase_ignore", Profile.phase_ignore))
    kw["overlapped"] = tuple(d.get("overlapped", ()))
    kw["concurrent"] = tuple(d.get("concurrent", ()))
    kw["run_id_env"] = tuple(d.get("run_id_env", Profile.run_id_env))
    kw["unbilled"] = tuple(d.get("unbilled", ()))
    kw["secrets"] = tuple(d.get("secrets", ()))
    em = d.get("error_marker", ())
    kw["error_marker"] = (em,) if isinstance(em, str) else tuple(em)
    kw["alerts"] = dict(d.get("alerts", {}))
    if bad := sorted(set(kw["alerts"]) - {"label", "logs_url", "comment"}):
        raise ValueError(f"scan-run profile: unknown alerts fields {bad}")
    kw["jobs"] = tuple(JobKind(kind=j["kind"], where=j.get("where", {}), scan=tuple(j.get("scan", ())),
                               downstream=bool(j.get("downstream", False))) for j in d.get("jobs", ()))
    kw["outputs"] = tuple(OutputSpec(key=o["key"], uri=anchor_static_gen(o["uri"], base), after=o.get("after"), kinds=tuple(o.get("kinds", ())),
                                     exclude=tuple(o.get("exclude", ())), split=o.get("split"), rows=o.get("rows"),
                                     manifest=bool(o.get("manifest", False))) for o in d.get("outputs", ()))
    for o in kw["outputs"]:
        if o.split not in (None, "dir", "stem"):
            raise ValueError(f"scan-run profile: output {o.key}: split {o.split!r} (want dir | stem)")
    return Profile(**kw)


# ── Measuring outputs ───────────────────────────────────────────────────────


class ObjectStore(Protocol):
    def list(self, uri: str) -> list[tuple[str, int]]:
        """Every object under the prefix `uri` (or the one object), as `(full uri, bytes)`."""

    def read(self, uri: str) -> bytes: ...

    def last_row_end(self, uri: str) -> int | None:
        """The `row_end` of the last row group listed in a `.groups.parquet` (the tier's row count)."""


def split_uri(uri: str) -> tuple[str, str, str]:
    m = re.fullmatch(r"([a-z0-9]+)://([^/]+)/?(.*)", uri)
    if not m:
        raise ValueError(f"not a store URI: {uri!r}")
    return m.group(1), m.group(2), m.group(3)


class GcsStore:
    def __init__(self):
        from google.cloud import storage

        self.client = storage.Client()

    def list(self, uri: str) -> list[tuple[str, int]]:
        _, b, key = split_uri(uri)
        return [(f"gs://{b}/{o.name}", o.size) for o in self.client.list_blobs(b, prefix=key)]

    def read(self, uri: str) -> bytes:
        _, b, key = split_uri(uri)
        return self.client.bucket(b).blob(key).download_as_bytes()

    def last_row_end(self, uri: str) -> int | None:
        import pyarrow.parquet as pq

        _, b, key = split_uri(uri)
        with self.client.bucket(b).blob(key).open("rb", chunk_size=256 << 10) as f:
            pf = pq.ParquetFile(f)
            n = pf.metadata.num_row_groups
            if not n:
                return 0
            return max(pf.read_row_group(n - 1, columns=["row_end"]).column("row_end").to_pylist())


class FsspecStore:
    """r2:// / s3:// (and anything fsspec reads) through `disk_tree.blobfs`."""

    def list(self, uri: str) -> list[tuple[str, int]]:
        from disk_tree import blobfs

        fs, path = blobfs.fs_for(uri)
        scheme = uri.split("://", 1)[0]
        found = fs.find(path, detail=True) if uri.endswith("/") else {path: fs.info(path)} if fs.exists(path) else {}
        return [(f"{scheme}://{k}", int(v.get("size") or 0)) for k, v in sorted(found.items())]

    def read(self, uri: str) -> bytes:
        from disk_tree import blobfs

        fs, path = blobfs.fs_for(uri)
        return fs.cat_file(path)

    def last_row_end(self, uri: str) -> int | None:
        import pyarrow.parquet as pq
        from disk_tree import blobfs

        f = blobfs.open_read(uri)
        try:
            pf = pq.ParquetFile(f)
            n = pf.metadata.num_row_groups
            return max(pf.read_row_group(n - 1, columns=["row_end"]).column("row_end").to_pylist()) if n else 0
        finally:
            f.close()


class AnyStore:
    """Dispatch by scheme: gs:// → `GcsStore`, else `FsspecStore`."""

    def __init__(self):
        self._gcs: GcsStore | None = None
        self._fs = FsspecStore()

    def _for(self, uri: str) -> ObjectStore:
        if uri.startswith("gs://"):
            self._gcs = self._gcs or GcsStore()
            return self._gcs
        return self._fs

    def list(self, uri):
        return self._for(uri).list(uri)

    def read(self, uri):
        return self._for(uri).read(uri)

    def last_row_end(self, uri):
        return self._for(uri).last_row_end(uri)


STATIC_GEN = re.compile(r"\{static_gen:([^{}]+)\}")


def anchor_static_gen(template: str, base: Path | None) -> str:
    """`template` with each relative `{static_gen:<file>}` made absolute against `base` (the scan-run profile's dir),
    so it names the same file from any working dir. An example's name stays a name."""
    if base is None:
        return template
    from .static_profile_examples import EXAMPLES

    def anchor(m: re.Match) -> str:
        sel = m.group(1)
        if sel in EXAMPLES or Path(sel).is_absolute():
            return m.group(0)
        return f"{{static_gen:{(base / sel).resolve()}}}"
    return STATIC_GEN.sub(anchor, template)


def static_gen(sel: str, env: Mapping[str, str] | None = None) -> str:
    """The static name index generation of profile `sel` (`static_profile.load_profile`: a JSON file or an example's
    name, `STATIC_NAMES_GEN` over it), so an output template follows the live generation instead of pinning one. No gen
    (an example without `STATIC_NAMES_GEN`) is an error naming the variable."""
    from .static_profile import load_profile
    env = dict(os.environ if env is None else env)
    return load_profile({**env, "STATIC_NAMES_PROFILE": sel}).need("gen")


def fill(template: str, scan: str, gen: str | None) -> str | None:
    """`template` with `{scan}` / `{gen}` / `{static_gen:<profile>}`; None when it needs a gen the run has none of."""
    if "{gen}" in template and not gen:
        return None
    template = STATIC_GEN.sub(lambda m: static_gen(m.group(1)), template)
    return template.replace("{scan}", scan).replace("{gen}", gen or "")


def _stem(name: str) -> str:
    return name.split(".", 1)[0]


def measure(spec: OutputSpec, scan: str, gen: str | None, store: ObjectStore) -> list[Output]:
    """The outputs `spec` names for this scan, measured from the store: the total, then its split parts (sorted). An
    absent prefix measures nothing (no output: the run did not write it)."""
    uri = fill(spec.uri, scan, gen)
    if uri is None:
        return []
    if spec.manifest:
        return _manifest_outputs(spec, uri, store)
    objs = [(u, n) for u, n in store.list(uri)
            if not u.endswith("/") and not any(u.startswith(uri + x) for x in spec.exclude)]  # `…/` = a folder placeholder
    if not objs:
        return []
    out = [Output(spec.key, uri, bytes=sum(n for _, n in objs), objects=len(objs), gen=gen if "{gen}" in spec.uri else None)]
    if spec.split and uri.endswith("/"):
        parts: dict[str, list[tuple[str, int]]] = {}
        for u, n in objs:
            rel = u[len(uri):]
            if spec.split == "dir":
                if "/" not in rel:
                    continue
                parts.setdefault(rel.split("/", 1)[0], []).append((u, n))
            else:
                parts.setdefault(_stem(rel.rsplit("/", 1)[-1]), []).append((u, n))
        rows_total = 0
        for name in sorted(parts):
            rows = _part_rows(spec, uri, name, store)
            if rows is not None:
                rows_total += rows
            ps = parts[name]
            out.append(Output(f"{spec.key}/{name}", f"{uri}{name}/" if spec.split == "dir" else f"{uri}{name}",
                              bytes=sum(n for _, n in ps), objects=len(ps), rows=rows, gen=out[0].gen))
        if spec.split == "dir" and isinstance(spec.rows, Mapping) and len(out) > 1 and all(o.rows is not None for o in out[1:]):
            out[0] = replace(out[0], rows=rows_total)
    return out


def _part_rows(spec: OutputSpec, uri: str, name: str, store: ObjectStore) -> int | None:
    if isinstance(spec.rows, Mapping) and spec.split == "dir":
        try:
            return int(json.loads(store.read(f"{uri}{name}/{spec.rows['json']}"))[spec.rows["field"]])
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001 — a missing/odd marker is "rows unknown", named on stderr
            if type(e).__name__ != "NotFound":
                err(f"scan-run: {uri}{name}/{spec.rows['json']}: {type(e).__name__}: {e}")
            return None
    if spec.rows == "groups" and spec.split == "stem" and not name.endswith("groups"):
        groups = f"{uri}{name}.groups.parquet"
        if any(u == groups for u, _ in store.list(groups)):
            return store.last_row_end(groups)
    return None


def _manifest_outputs(spec: OutputSpec, pattern: str, store: ObjectStore) -> list[Output]:
    """Every manifest the glob matches (one per index generation the scan was appended to): its runs as outputs."""
    star = pattern.find("*")
    base = pattern[:star] if star >= 0 else pattern
    rx = re.compile(re.escape(pattern).replace(r"\*", "[^/]+") + "$")
    out = []
    for u, _ in sorted(store.list(base)):
        if not rx.match(u):
            continue
        m = json.loads(store.read(u))
        gen = m.get("gen")
        root = u.rsplit("/manifests/", 1)[0] + "/"
        runs = m.get("runs", ())
        out.append(Output(spec.key, u, bytes=sum(r.get("bytes") or 0 for r in runs), rows=sum(r.get("rows") or 0 for r in runs),
                          objects=len(runs), gen=gen))
        for r in runs:
            out.append(Output(f"{spec.key}/{r['key']}", f"{root}{r['key']}/", bytes=r.get("bytes"),
                              objects=r.get("objects"), rows=r.get("rows"), gen=gen))
    return out


ALL = object()


def measure_after(profile: Profile, phase: object, kind: str, scan: str, gen: str | None,
                  store: ObjectStore) -> list[Output]:
    """The outputs the profile ties to `phase` (None: those measured at the run's end, `ALL`: every one) for a run
    of `kind`."""
    out = []
    for spec in profile.outputs:
        if phase is not ALL and spec.after != phase:
            continue
        if spec.kinds and kind not in spec.kinds:
            continue
        out.extend(measure(spec, scan, gen, store))
    return out


# ── Cost ────────────────────────────────────────────────────────────────────


def cost(profile: Profile, machine: str | None, spot: bool | None, tasks: int | None, seconds: float | None) -> float | None:
    """The run's estimated compute $: the machine's $/h (× `spot_factor` on SPOT) × hours × tasks. None when the
    profile prices no such machine."""
    if not machine or seconds is None or machine not in profile.prices:
        return None
    rate = profile.prices[machine] * (profile.spot_factor if spot and profile.spot_factor is not None else 1.0)
    return round(rate * seconds / 3600 * (tasks or 1), 4)


# ── `record`: the job's incremental writes ──────────────────────────────────


def env_run_id(profile: Profile, env: Mapping[str, str] | None = None) -> str | None:
    """The run id from the first of the profile's `run_id_env` vars that is set."""
    env = os.environ if env is None else env
    return next((v for k in profile.run_id_env if (v := env.get(k, "").strip())), None)


#: The job facts `record` takes directly (a scheduler with no Batch-API lookup states its own).
JOB_FACTS = ("job_name", "region", "machine", "spot", "tasks", "image")


def billed_secs(profile: Profile, started: int | None, finished: int | None, phases: Iterable[Mapping | Phase]) -> int | None:
    """The run's billed wall time: start to end, less its `unbilled` phases."""
    if started is None or finished is None:
        return None
    unbilled = 0
    for p in phases:
        name, s, e = (p.phase, p.started_ts, p.finished_ts) if isinstance(p, Phase) else (p["phase"], p["started_ts"], p["finished_ts"])
        if name in profile.unbilled and s is not None and e is not None:
            unbilled += e - s
    return max(0, finished - started - unbilled)


def stored(sink: Sink, run_id: str) -> tuple[dict | None, list[dict]]:
    """The run row and its phases as the sink holds them (both empty under `-n`)."""
    rows = sink.query(f"SELECT * FROM scan_runs WHERE run_id = {lit(run_id)}")
    phases = sink.query(f"SELECT * FROM scan_run_phases WHERE run_id = {lit(run_id)} ORDER BY seq")
    return (rows[0] if rows else None), phases


def record(
    sink: Sink,
    profile: Profile,
    scan: str,
    run_id: str,
    now: int,
    store: ObjectStore | None = None,
    kind: str = "scan",
    start: bool = False,
    phase: str | None = None,
    phase_start: bool = False,
    note: str | None = None,
    began: int | None = None,
    outputs: Sequence[tuple[str, str]] = (),
    gen: str | None = None,
    exit_code: int | None = None,
    nop: bool = False,
    failed_phase: str | None = None,
    error: str | None = None,
    parent: str | None = None,
    job: Mapping | None = None,
    facts: Mapping[str, object] | None = None,
) -> Record:
    """One incremental write of run `run_id` (scan `scan`): start it, start/close a phase (measuring completed outputs tied to
    it), measure named outputs, finish it (`exit_code`, or `nop`). The run row is upserted every time, so any call
    may come first. Returns what was written.

    `facts`: job facts the caller states (`JOB_FACTS`; a None is "unknown", never a clear). With a machine known (here
    or stored) and no Batch `job`, the finishing write estimates the run's cost over its billed time (`billed_secs`)."""
    check_scan_id(scan)
    if phase_start and not phase:
        raise ValueError("scan-run: a phase start needs a phase name")
    row, phases = stored(sink, run_id)
    started = row.get("started_ts") if row else None
    status = row["status"] if row and row["status"] in ("nop", "failed", "succeeded") else "running"
    if nop:
        status = "nop"
    elif exit_code is not None and not (exit_code == 0 and status == "nop"):  # the exit trap after a NOP keeps it
        status = "succeeded" if exit_code == 0 else "failed"
    elif start or (phase and not phase_start):
        status = "running" if status != "nop" else status
    run = Run(run_id=run_id, scan=scan, kind=kind, status=status, store=profile.store, parent=parent,
              started_ts=now if start and not started else None, gen=gen, source="job")
    if exit_code is not None or nop:
        run = replace(run, finished_ts=now)
        if exit_code:
            run = replace(run, failed_phase=failed_phase, error=error or f"exit {exit_code}")
    if facts:
        if bad := sorted(set(facts) - set(JOB_FACTS)):
            raise ValueError(f"scan-run: unknown job facts {bad}")
        run = replace(run, **{k: (int(v) if k == "spot" else v) for k, v in facts.items() if v is not None})
    if job:
        # only what the job knows: one read just after start has no RUNNING event yet, and its None must not clear
        # the start `-S` stamped
        run = replace(run, **{k: v for k, v in job_fields(profile, job, now).items() if v is not None})
    new_phases: list[Phase] = []
    if phase:
        existing = next((p for p in phases if p["phase"] == phase), None)
        skip = profile.overlapped + (profile.concurrent if phase in profile.concurrent else ())
        prev_end = max((p["finished_ts"] for p in phases if p.get("finished_ts") and p["phase"] not in skip), default=None)
        begin = run.started_ts or started
        p_start = began if began is not None else existing["started_ts"] if existing and existing["started_ts"] is not None else (
            now if phase_start else begin if phase in profile.overlapped else (prev_end or begin))
        seq = next((p["seq"] for p in phases if p["phase"] == phase), len(phases))
        if not phase_start or not existing or existing["finished_ts"] is None:
            new_phases.append(Phase(phase, seq, p_start, None if phase_start else now,
                                    "running" if phase_start else phase_status(note), note))
    if exit_code:
        new_phases.extend(Phase(p["phase"], p["seq"], p["started_ts"], now, "failed", f"failed (exit {exit_code})")
                          for p in phases if p["status"] == "running" and p["finished_ts"] is None)
    machine = run.machine or (row.get("machine") if row else None)
    if (exit_code is not None or nop) and not job and machine:
        spot = run.spot if run.spot is not None else (row.get("spot") if row else None)
        tasks = run.tasks if run.tasks is not None else (row.get("tasks") if row else None)
        merged = {p["phase"]: p for p in phases} | {p.phase: p for p in new_phases}  # this write's phases over the stored
        secs = billed_secs(profile, run.started_ts or started, now, merged.values())
        run = replace(run, cost_usd=cost(profile, machine, bool(spot), tasks, secs))
    outs: list[Output] = []
    gen_now = gen or (row.get("gen") if row else None)
    if store is not None and phase and not phase_start:
        outs += measure_after(profile, phase, kind, scan, gen_now, store)
    if store is not None and exit_code is not None and not exit_code:
        outs += measure_after(profile, None, kind, scan, gen_now, store)  # outputs with no `after`: the run's end
    for key, uri in outputs:
        if store is None:
            raise SystemExit("scan-run: -o needs a store to measure (it isn't available under this sink)")
        outs += measure(OutputSpec(key=key, uri=uri, split=None), scan, gen_now, store)
    rec = Record(run, tuple(new_phases), tuple(outs))
    sink.execute(record_sql(rec, now))
    return rec


def job_fields(profile: Profile, job: Mapping, now: int | None = None) -> dict:
    """The run's Batch facts from its job resource: name, region, machine, SPOT, tasks, image, the task's start and
    end, and the cost estimate."""
    name = job["name"].rsplit("/", 1)[-1]
    region = job["name"].split("/locations/", 1)[1].split("/", 1)[0] if "/locations/" in job["name"] else None
    tg = (job.get("taskGroups") or [{}])[0]
    inst = (job.get("allocationPolicy", {}).get("instances") or [{}])[0].get("policy", {})
    machine = inst.get("machineType")
    spot = inst.get("provisioningModel") == "SPOT"
    st = job.get("status", {})
    for g in (st.get("taskGroups") or {}).values():
        for i in g.get("instances", ()):
            machine = machine or i.get("machineType")
            spot = spot or i.get("provisioningModel") == "SPOT"
    tasks = int(tg.get("taskCount", 1))
    par = int(tg.get("parallelism", tasks) or tasks)
    started = finished = None
    for ev in st.get("statusEvents", ()):
        d = ev.get("description", "")
        if " to RUNNING" in d and started is None:
            started = _epoch(ev["eventTime"])
        if re.search(r" to (SUCCEEDED|FAILED|CANCELLED)", d):
            finished = _epoch(ev["eventTime"])
    secs = (finished or now) - started if started and (finished or now) else None
    image = ((tg.get("taskSpec", {}).get("runnables") or [{}])[0].get("container", {}).get("imageUri") or "").rsplit("/", 1)[-1] or None
    return dict(job_name=name, region=region, machine=machine, spot=int(spot), tasks=tasks, image=image,
                started_ts=started, finished_ts=finished, cost_usd=cost(profile, machine, spot, min(tasks, par), secs))


def recovery_record(
    sink: Sink,
    profile: Profile,
    parent: str,
    phase: str,
    job: Mapping,
    now: int,
    note: str | None = None,
) -> Record:
    """Attach one completed Batch attempt, preserving the parent's failed phase and clock."""
    from .scan_runs_backfill import BATCH_STATES, failure_of

    row, phases = stored(sink, parent)
    if row is None or row["store"] != profile.store:
        raise ValueError(f"scan-run recovery: no parent {parent!r} in store {profile.store!r}")
    original = next((p for p in phases if p["phase"] == phase), None)
    if original is None or (original["status"] != "failed" and phase_status(original.get("note")) != "failed"):
        raise ValueError(f"scan-run recovery: parent has no failed phase {phase!r}")
    state = job.get("status", {}).get("state")
    if state not in ("SUCCEEDED", "FAILED", "CANCELLED"):
        raise ValueError(f"scan-run recovery: Batch attempt is not terminal ({state!r})")
    jf = job_fields(profile, job, now)
    start, end = jf["started_ts"], jf["finished_ts"]
    if start is None or end is None or end < start or (original["finished_ts"] is not None and end <= original["finished_ts"]):
        raise ValueError("scan-run recovery: missing or invalid Batch attempt times")
    uid = job["uid"]
    existing, existing_phases = stored(sink, uid)
    if existing and (existing["parent"], existing["scan"], existing["store"], existing["kind"], [p["phase"] for p in existing_phases]) != (
        parent, row["scan"], profile.store, "phase-recovery", [phase],
    ):
        raise ValueError(f"scan-run recovery: conflicting existing attempt {uid!r}")
    run = Run(run_id=uid, scan=row["scan"], kind="phase-recovery", parent=parent, status=BATCH_STATES[state],
              store=profile.store, source="backfill", error=failure_of(profile, job, []), **jf)
    attempt = Phase(phase, 0, start, end, "done" if state == "SUCCEEDED" else "failed", note)
    return Record(run, (attempt,))


def _epoch(ts: str) -> int:
    import datetime as dt

    s = re.sub(r"(\.\d{6})\d*", r"\1", ts.replace("Z", "+00:00"))
    return int(dt.datetime.fromisoformat(s).timestamp())


def as_json(rec: Record) -> dict:
    return {"run": asdict(rec.run), "phases": [asdict(p) for p in rec.phases], "outputs": [asdict(o) for o in rec.outputs]}


def sink_for(dry_run: bool, sqlite: str | None, d1: str | None) -> Sink:
    if dry_run:
        return PrintSink()
    if sqlite:
        return SqliteSink(sqlite)
    return D1Sink(d1)


def write_all(sink: Sink, recs: Iterable[Record], now: int) -> int:
    n = 0
    for rec in recs:
        sink.execute(record_sql(rec, now))
        n += 1
    return n


def kinds_filter(kinds: Iterable[str]) -> Callable[[str], bool]:
    ks = set(kinds)
    return (lambda k: k in ks) if ks else (lambda k: True)


# ── CLI ─────────────────────────────────────────────────────────────────────

from click import IntRange, argument, group, option  # noqa: E402


@group("scan-run")
def cli() -> None:
    """Scan runs: what each scan job did (specs/done/scan-runs-ui.md), recorded in the site's D1."""


@cli.command("record")
@option("-A", "--phase-start", is_flag=True, help="With -P: mark the phase running now; measure no phase outputs until completion")
@option("-b", "--began", type=int, default=None, help="With -P: when the phase began (epoch seconds), for one run beside the main sequence (list it in the profile's `overlapped`)")
@option("-B", "--from-batch", is_flag=True, help="Fill the Batch facts (machine, SPOT, tasks, cost) from the job whose uid is the run id, found in the profile's regions")
@option("-c", "--profile", "profile_path", default=None, help=f"The deployment's profile JSON (default ${PROFILE_ENV})")
@option("-d", "--d1", default=None, help="D1 database id (default $D1_DB_ID)")
@option("-e", "--error", default=None, help="With a non-zero -x: the failure's one-line reason (default `exit <rc>`)")
@option("-F", "--failed-phase", default=None, help="With a non-zero -x: the phase that was running")
@option("-g", "--gen", default=None, help="The index generation this run writes (fills `{gen}` in output templates)")
@option("-i", "--image", default=None, help="Job fact: the image the run ran")
@option("-j", "--job-name", default=None, help="Job fact: the job's name (stated, not looked up; see -J)")
@option("-J", "--job", "batch_job_name", default=None, help="Fill the Batch facts from this GCP Batch job name (with -R)")
@option("-k", "--kind", default="scan", help="The run's kind (a profile job kind; default scan)")
@option("-K", "--store", "store_key", default=None, help="The store the run belongs to (`scan_runs.store`; default the profile's `store`)")
@option("-m", "--machine", default=None, help="Job fact: the machine; a key of the profile's `prices` gets the run a cost estimate when it finishes")
@option("-M", "--no-measure", is_flag=True, help="Don't measure outputs (record timings only)")
@option("-n", "--dry-run", is_flag=True, help="Print the SQL instead of writing it")
@option("-N", "--nop", is_flag=True, help="Finish the run as a no-op (the scan was already published)")
@option("-o", "--output", "outputs", multiple=True, help="Measure KEY=URI as an output too (repeatable)")
@option("-p", "--parent", default=None, help="The orchestrating run (a downstream job's)")
@option("-P", "--phase", default=None, help="A phase just ended: record it (it began where the last one ended) and measure the outputs the profile ties to it")
@option("-q", "--note", default=None, help="With -P: a note on the phase")
@option("-r", "--run-id", default=None, help="The run id (default the first set of the profile's `run_id_env` vars: $BATCH_JOB_UID unless it names others)")
@option("-R", "--region", default=None, help="The job's region: a job fact, or with -J / -B where to look the job up (default the profile's first)")
@option("-S", "--start", is_flag=True, help="Mark the run started (status running), at -t or now")
@option("-s", "--sqlite", default=None, help="Write to this local SQLite file (created from the migration) instead of D1")
@option("--spot/--no-spot", default=None, help="Job fact: the run ran on spot capacity (priced with the profile's `spot_factor`)")
@option("-t", "--at", type=int, default=None, help="This step's time, epoch seconds (default now): the start with -S, the phase's end with -P, the run's end with -x")
@option("-T", "--tasks", type=int, default=None, help="Job fact: the job's task count")
@option("-x", "--exit", "exit_code", type=IntRange(0, 255), default=None, help="Finish the run with this exit code (0 = succeeded)")
@argument("scan")
def record_cmd(phase_start, began, from_batch, profile_path, d1, error, failed_phase, gen, image, job_name, batch_job_name, kind, store_key, machine,
               no_measure, dry_run, nop, outputs, parent, phase, note, run_id, region, start, sqlite, spot, at, tasks, exit_code, scan) -> None:
    """Record one step of SCAN's run, from the job: `-S` at its start, `-P NAME` as each phase ends, `-x RC` from
    its exit trap. Never fails the job it reports on: an error is printed and the exit status is 0."""
    try:
        profile = load_profile(profile_path)
        if store_key:
            profile = replace(profile, store=store_key)
        run_id = run_id or env_run_id(profile)
        if not run_id:
            raise SystemExit(f"scan-run: no run id: pass -r or set {' / '.join('$' + k for k in profile.run_id_env)}")
        sink = sink_for(dry_run, sqlite, d1)
        store = None if no_measure else AnyStore()
        job = lookup_job(profile, run_id, batch_job_name, from_batch, region)
        pairs = [tuple(o.split("=", 1)) for o in outputs]
        facts = dict(job_name=job_name, region=None if (batch_job_name or from_batch) else region, machine=machine, spot=spot,
                     tasks=tasks, image=image)
        rec = record(sink, profile, scan, run_id, _now() if at is None else at, store=store, kind=kind, start=start,
                     phase=phase, phase_start=phase_start, note=note, began=began, outputs=pairs, gen=gen, exit_code=exit_code, nop=nop,
                     failed_phase=failed_phase, error=error, parent=parent, job=job, facts=facts)
        report_record(rec, phase)
    except (Exception, SystemExit) as e:  # noqa: BLE001 — reporting must never fail the scan
        report_failure(e)


def _now() -> int:
    """The clock every job-side write stamps (the seam tests pin)."""
    import time

    return int(time.time())


def lookup_job(profile: Profile, run_id: str, batch_job_name: str | None, from_batch: bool, region: str | None) -> Mapping | None:
    """The run's Batch job: `-J` by name, `-B` by the run id (its uid) in the profile's regions; else None."""
    if batch_job_name:
        from .gcp import batch_job

        return batch_job(batch_job_name, profile.project, region or (profile.regions[0] if profile.regions else "us-central1"))
    if from_batch:
        from .scan_runs_backfill import fetch_jobs

        job = next((j for j in fetch_jobs(profile.project or "", [region] if region else profile.regions) if j.get("uid") == run_id), None)
        if job is None:
            err(f"scan-run: no Batch job with uid {run_id} in {region or profile.regions}")
        return job
    return None


def report_record(rec: Record, phase: str | None) -> None:
    err(f"scan-run: {rec.run.scan} {rec.run.run_id} {rec.run.status}" + (f" · phase {phase}" if phase else "")
        + (f" · {len(rec.outputs)} outputs" if rec.outputs else ""))


def report_failure(e: BaseException) -> None:
    err(f"scan-run record failed (the scan continues): {type(e).__name__}: {e}")


@cli.command("recovery")
@option("-c", "--profile", "profile_path", default=None, help=f"Deployment profile (default ${PROFILE_ENV})")
@option("-d", "--d1", default=None, help="D1 database id (default $D1_DB_ID)")
@option("-J", "--job", "job_name", required=True, help="Completed Batch job: its UID, times, status and links are read from Batch")
@option("-n", "--dry-run", is_flag=True, help="Validate against D1 and print SQL instead of writing it")
@option("-p", "--parent", required=True, help="Original scan run id (never overwritten)")
@option("-P", "--phase", required=True, help="The failed phase this attempt reran")
@option("-q", "--note", default=None, help="Describe the scope of this attempt, including reused work")
@option("-R", "--region", required=True, help="Batch job region")
@option("-s", "--sqlite", default=None, help="Use a local scan-run SQLite database instead of D1")
def recovery_cmd(
    profile_path: str | None,
    d1: str | None,
    job_name: str,
    dry_run: bool,
    parent: str,
    phase: str,
    note: str | None,
    region: str,
    sqlite: str | None,
) -> None:
    """Link a completed phase re-run to its original scan. No job is launched; safe to repeat."""
    import time

    from .gcp import batch_job

    profile = load_profile(profile_path)
    sink = sink_for(False, sqlite, d1)
    now = int(time.time())
    rec = recovery_record(sink, profile, parent, phase, batch_job(job_name, profile.project, region), now, note)
    write_all(PrintSink() if dry_run else sink, [rec], now)
    err(f"scan-run recovery: {rec.run.run_id} {rec.run.status} · {parent} / {phase}")


@cli.command("backfill")
@option("-c", "--profile", "profile_path", default=None, help=f"The deployment's profile JSON (default ${PROFILE_ENV})")
@option("-d", "--d1", default=None, help="D1 database id (default $D1_DB_ID)")
@option("-j", "--jobs-json", default=None, help="Read the Batch jobs from this JSON file (a list) instead of the API")
@option("-l", "--logs-json", default=None, help="Read the log entries from this JSON file instead of the API")
@option("-M", "--no-measure", is_flag=True, help="Don't measure outputs from the store")
@option("-n", "--dry-run", is_flag=True, help="Print the SQL instead of writing it")
@option("-o", "--summary", is_flag=True, help="Print one line per reconstructed run to stderr")
@option("-S", "--since", default=None, help="Only scans with ids ≥ this")
@option("-s", "--sqlite", default=None, help="Write to this local SQLite file (created from the migration) instead of D1")
@option("-w", "--save-inputs", default=None, help="Also save the fetched jobs + log entries to DIR/{jobs,logs}.json (replay with -j/-l)")
def backfill_cmd(profile_path, d1, jobs_json, logs_json, no_measure, dry_run, summary, since, sqlite, save_inputs) -> None:
    """Reconstruct past scan runs from the Batch API, Cloud Logging's markers and the data bucket (read-only), and
    upsert them. Idempotent; a run the job recorded itself keeps its own phases and source."""
    import time

    from . import scan_runs_backfill as bf

    profile = load_profile(profile_path)
    now = int(time.time())
    project = profile.project or ""
    if not project and not (jobs_json and logs_json):
        raise SystemExit("scan-run backfill: the profile names no project")
    jobs = json.loads(Path(jobs_json).read_text()) if jobs_json else bf.fetch_jobs(project, profile.regions)
    logs = json.loads(Path(logs_json).read_text()) if logs_json else bf.fetch_logs(profile, project, now)
    if save_inputs:
        Path(save_inputs).mkdir(parents=True, exist_ok=True)
        (Path(save_inputs) / "jobs.json").write_text(json.dumps(jobs))
        (Path(save_inputs) / "logs.json").write_text(json.dumps(logs))
    sink = sink_for(dry_run, sqlite, d1)  # before the store reads: a misconfigured sink fails fast
    recs = bf.reconstruct(profile, jobs, logs, None if no_measure else AnyStore(), now, since=since)
    write_all(sink, recs, now)
    if summary:
        for r in recs:
            ph = len(r.phases)
            err(f"{r.run.scan:16} {r.run.kind:8} {r.run.status:9} {r.run.job_name or r.run.run_id:44} phases={ph} outputs={len(r.outputs)}")
    err(f"scan-run backfill: {len(recs)} runs over {len({r.run.scan for r in recs})} scans "
        f"({len(jobs)} jobs, {len(logs)} log lines)")


# ── The job wrapper: `start`, `phase-begin`/`phase-end`, `phase -- CMD`, `finish` (specs/scan-job-wrapper.md) ─────
#
# A job script's recording, PHASE markers, redaction and failure alerts, so each deployment's script keeps only its
# step list, two one-line traps and its per-step messages. Every command reads the run id like `record` (`-r`, else
# the profile's `run_id_env`); with none (not a scheduled job) or `$DT_RECORD=off`, nothing is recorded, while the
# markers still print and alerts still post. `start` keeps the run's scan, kind, parent, gen and job facts in a state
# file beside the "already alerted" marker, under `${TMPDIR:-/tmp}/dt-scan-run/`, which the phases (background
# subshells included) and `finish` read.

JOB_START_ENV = "DT_JOB_START"
#: `stderr`: alerts print as `alert (suppressed): …` and nothing posts.
ALERTS_ENV = "DT_ALERTS"
#: `off` (or `0`): record nothing (a scratch / gate run); markers and alerts are unaffected.
RECORD_ENV = "DT_RECORD"
#: Set by `supervise` for its job: where `start` says which run it started (`finish` then leaves the state to it).
SUPERVISE_ENV = "DT_SUPERVISE"
#: Set by `supervise`: the most a `phase -T` group gets after a received TERM/INT before KILL (inside the supervisor's).
TERM_GRACE_ENV = "DT_TERM_GRACE"
#: The container's cgroup (default `/sys/fs/cgroup`), where `finish` reads the memory limit, peak and OOM kills.
CGROUP_ENV = "DT_CGROUP"
#: An alert's ERR-trap text past this many characters keeps its head and its `(file:line)`.
ALERT_ERROR_CHARS = 240


def read_int(path: Path) -> int | None:
    """A cgroup counter file's integer (`max`, unreadable or absent: None)."""
    try:
        v = path.read_text().strip()
    except OSError:
        return None
    return int(v) if v.isdigit() else None


def read_keyed(path: Path, key: str) -> int | None:
    """One `key value` line's integer from a cgroup stats file (`memory.events`, `memory.oom_control`)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for ln in lines:
        k, _, v = ln.partition(" ")
        if k == key and v.strip().isdigit():
            return int(v)
    return None


def gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


def oom_note(cgroup: Path) -> str:
    """Why a command died of a SIGKILL nobody here sent (exit 137): the kernel's OOM killer, when the container's
    cgroup counted a kill (v2 `memory.events`, v1 `memory.oom_control`), else most likely it; with the memory limit
    and peak where the cgroup keeps them (v2 `memory.max`/`memory.peak`, v1 `memory.limit_in_bytes`/
    `memory.max_usage_in_bytes`)."""
    kills = read_keyed(cgroup / "memory.events", "oom_kill")
    if kills is None:
        kills = read_keyed(cgroup / "memory.oom_control", "oom_kill")
    limit = read_int(cgroup / "memory.max") or read_int(cgroup / "memory.limit_in_bytes")
    peak = read_int(cgroup / "memory.peak") or read_int(cgroup / "memory.max_usage_in_bytes")
    if limit is not None and limit >= 2**60:  # v1's "unlimited"
        limit = None
    mem = (f"peak {gib(peak)} of {gib(limit)}" if peak and limit else
           f"peak {gib(peak)}" if peak else f"limit {gib(limit)}" if limit else None)
    what = "out of memory (OOM-killed)" if kills else "killed (SIGKILL), most likely out of memory"
    return f"{what}{f', {mem}' if mem else ''}"


def clip_error(error: str, n: int = ALERT_ERROR_CHARS) -> str:
    """ERROR shortened for an alert past N characters: its head, `…`, and its trailing `(file:line)` if it has one."""
    if len(error) <= n:
        return error
    m = re.search(r" \([^()]+:\d+\)$", error)
    tail = m.group(0) if m else ""
    return f"{error[:n - len(tail) - 1].rstrip()}…{tail}"


def phase_line(name: str, secs: int, note: str | None = None, start: bool = False) -> str:
    """A PHASE marker, byte-identical to the job scripts' (`PHASE <name>: <N>s (wall[, note])`; the backfill parses
    them from the logs: `Profile.phase_marker` / `phase_start_marker`)."""
    if start:
        return f"PHASE-START {name}: {secs}s (wall)"
    return f"PHASE {name}: {secs}s (wall{f', {note}' if note else ''})"


def redact(text: str, names: Iterable[str], env: Mapping[str, str]) -> str:
    """`text` with each named env var's value (trailing whitespace stripped: an injected secret's newline is not part
    of it, and must not swallow the text's own) replaced by `<NAME>`; longer values first, so one secret containing
    another is replaced whole."""
    vals = sorted({(v, n) for n in names if (v := (env.get(n) or "").rstrip())}, key=lambda p: -len(p[0]))
    for v, n in vals:
        text = text.replace(v, f"<{n}>")
    return text


def duration(s: str) -> float:
    """`90`, `90s`, `120m`, `2h`, `1d` → seconds (coreutils `timeout`'s forms)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", s.strip())
    if not m:
        raise ValueError(f"not a duration: {s!r} (want N[s|m|h|d])")
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def _off(v: str | None) -> bool:
    return (v or "").strip().lower() in ("0", "off", "false", "no")


class JobRun:
    """One job's view of its run: profile, run id, sink, state file and alerted marker."""

    def __init__(
        self,
        profile_path: str | None,
        d1: str | None,
        dry_run: bool,
        run_id: str | None,
        sqlite: str | None,
        env: Mapping[str, str] | None = None,
    ):
        self.env = dict(os.environ) if env is None else dict(env)
        try:
            self.profile = load_profile(profile_path)
        except (Exception, SystemExit) as e:  # noqa: BLE001 — the wrapper never fails the job for its own reasons
            report_failure(e)
            self.profile = Profile()
        self.run_id = run_id or env_run_id(self.profile, self.env)
        self.recording = bool(self.run_id) and not _off(self.env.get(RECORD_ENV))
        self.d1, self.dry_run, self.sqlite = d1, dry_run, sqlite
        key = self.run_id or f"local-{self.env.get(JOB_START_ENV, '').strip() or 'run'}"
        self.key = re.sub(r"[^\w.-]", "_", key)
        self.dir = Path(self.env.get("TMPDIR") or "/tmp") / "dt-scan-run"

    # state shared across the job's processes

    @property
    def state_path(self) -> Path:
        return self.dir / f"{self.key}.json"

    @property
    def alerted_path(self) -> Path:
        return self.dir / f"{self.key}.alerted"

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, fresh: bool = False, **kw: object) -> None:
        """Merge `kw` (None values skipped) into the state file, atomically and under an exclusive lock: phases in
        background subshells share it, and an unlocked read-merge-write lets a concurrent one drop another's field (a
        background phase's `failed_phase` lost to a foreground `phase-end`)."""
        import fcntl
        import tempfile

        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / f"{self.key}.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            st = {} if fresh else self.state()
            st.update({k: v for k, v in kw.items() if v is not None})
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{self.key}.")
            with os.fdopen(fd, "w") as f:
                json.dump(st, f)
            os.replace(tmp, self.state_path)

    def alerted(self) -> bool:
        return self.alerted_path.exists()

    def mark_alerted(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.alerted_path.touch()

    def clear(self) -> None:
        for p in (self.state_path, self.alerted_path, self.dir / f"{self.key}.lock"):
            p.unlink(missing_ok=True)

    @property
    def scan(self) -> str | None:
        return self.state().get("scan") or self.env.get("SCAN_ID") or None

    def redact(self, text: str) -> str:
        return redact(text, self.profile.secrets, self.env)

    # the record

    def write(self, at: int | None = None, **kw) -> Record | None:
        """One `record` write of this run, at `at` (epoch s; default now). Never raises: a failure is printed, the job
        continues."""
        if not self.recording or not self.run_id:
            return None
        try:
            st = self.state()
            profile = replace(self.profile, store=st["store"]) if st.get("store") else self.profile
            if kw.get("gen") is None:
                kw["gen"] = st.get("gen")
            rec = record(sink_for(self.dry_run, self.sqlite, self.d1), profile, self.scan or "", self.run_id, _now() if at is None else at,
                         kind=st.get("kind", "scan"), parent=st.get("parent"), **kw)
            report_record(rec, kw.get("phase"))
            return rec
        except (Exception, SystemExit) as e:  # noqa: BLE001 — reporting must never fail the scan
            report_failure(e)
            return None

    # alerts

    def facts(self) -> dict[str, str]:
        st = self.state()
        f = {"job_uid": self.run_id, "project": self.profile.project, "region": st.get("region"), "job": st.get("job_name"),
             "scan": self.scan, "kind": st.get("kind", "scan"), "store": st.get("store") or self.profile.store}
        return {k: str(v) for k, v in f.items() if v}

    def logs_url(self) -> str | None:
        t = self.profile.alerts.get("logs_url")
        if not t:
            return None
        try:
            return t.format_map(self.facts())
        except (KeyError, IndexError, ValueError):
            return None

    def alert(self, level: str, message: str) -> None:
        """Post a `fail` / `warn` alert through `disk-tree notify`'s channels (or print it, under `DT_ALERTS=stderr`).
        Never raises: alerting must never mask the failure it reports."""
        try:
            from disk_tree.notify import push

            label = self.profile.alerts.get("label") or "scan"
            title = self.redact(f"{'❌' if level == 'fail' else '⚠️'} {label} {self.scan or ''}".rstrip())
            message = self.redact(message)
            if (self.env.get(ALERTS_ENV) or "").strip() == "stderr":
                err(f"alert (suppressed): {title}: {message}")
                return
            chans = push.configured(self.env)
            if not chans:
                err(f"alert (no channel configured): {title}: {message}")
                return
            url = self.logs_url()
            notice = push.Notice(title, message, url, "task logs" if url else None, 1 if level == "fail" else 0)
            for c, e in push.send(notice, chans, env=self.env, post=push.http_post):
                err(self.redact(f"alert: {c}: {e or 'sent'}"))
        except Exception as e:  # noqa: BLE001
            err(self.redact(f"alert failed (the job continues): {type(e).__name__}: {e}"))

    def finish(self, rc: int, error: str | None = None) -> None:
        """Record the run finished with `rc` (`error` its one-line reason), and on a failure post the `fail` alert
        unless one was already posted (a phase's, or an earlier `finish`'s)."""
        error = self.redact(error) if error else None
        # A SIGKILL from outside (a child's exit 137 in the ERR trap's text, or `supervise`'s CMD killed): the OOM killer's.
        if rc == 137 and (error is None or error == "killed (SIGKILL)" or error.startswith("exit 137")):
            note = oom_note(Path(self.env.get(CGROUP_ENV) or "/sys/fs/cgroup"))
            error = f"{note}: {error}" if error and error.startswith("exit") else note
        st = self.state()
        # A failing `phase-end` names its phase; a plain command failing inside a begun phase (`set -e`, the ERR trap)
        # doesn't, so the one phase still running is the failure's. Several running (background phases): none named.
        running = st.get("running", [])
        failed_phase = st.get("failed_phase") or (running[0] if rc and len(running) == 1 else None)
        self.write(exit_code=rc, error=error, failed_phase=failed_phase)
        if rc and not self.alerted():
            self.mark_alerted()
            self.alert("fail", f"run failed (exit {rc}){f' in {failed_phase}' if failed_phase else ''}{f': {clip_error(error)}' if error else ''}")

    # under `supervise`

    @property
    def supervised(self) -> bool:
        return bool((self.env.get(SUPERVISE_ENV) or "").strip())

    def point_supervisor(self, profile_path: str | None, d1: str | None, dry_run: bool, sqlite: str | None) -> None:
        """Tell an enclosing `supervise` which run this job started, and how it is recorded, so it can finish it."""
        path = (self.env.get(SUPERVISE_ENV) or "").strip()
        if not path:
            return
        ptr = dict(run_id=self.run_id, recording=self.recording, profile=profile_path or self.env.get(PROFILE_ENV),
                   d1=d1, dry_run=dry_run, sqlite=str(Path(sqlite).absolute()) if sqlite else None, dir=str(self.dir),
                   key=self.key, env={k: self.env[k] for k in (ALERTS_ENV, "SCAN_ID") if k in self.env})
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(ptr))
        except OSError as e:
            err(f"scan-run: can't tell the supervisor the run ({path}): {e}")

    @classmethod
    def pointed(cls, path: Path) -> JobRun | None:
        """The run a supervised job's `start` pointed at (None: it started none)."""
        try:
            ptr = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        env = {k: v for k, v in os.environ.items() if k != SUPERVISE_ENV} | ptr["env"]
        j = cls(ptr["profile"], ptr["d1"], ptr["dry_run"], ptr["run_id"], ptr["sqlite"], env=env)
        j.recording, j.dir, j.key = ptr["recording"], Path(ptr["dir"]), ptr["key"]
        return j

    # the steps

    def clock(self, at: int | None = None) -> int:
        """Seconds from `$DT_JOB_START` to `at` (default now; negative for an instant before the job, e.g. a phase that
        ended before it existed); exits 2 when it isn't set (a PHASE line never prints another clock)."""
        v = (self.env.get(JOB_START_ENV) or "").strip()
        if not v.isdigit():
            err(f"scan-run: ${JOB_START_ENV} is not set: export {JOB_START_ENV}=$(date +%s) at the job's top")
            sys.exit(2)
        return (_now() if at is None else at) - int(v)

    def begin(self, name: str, began: int | None = None, gen: str | None = None) -> None:
        err(phase_line(name, self.clock(began), start=True))
        self.save(running=[*(r for r in self.state().get("running", []) if r != name), name], **({"gen": gen} if gen else {}))
        self.write(phase=name, phase_start=True, began=began, gen=gen)

    def end(
        self,
        name: str,
        rc: int = 0,
        warn: bool = False,
        msg: str | None = None,
        resume: str | None = None,
        note: str | None = None,
        outputs: Sequence[str] = (),
        measure: bool = True,
        timed_out: str | None = None,
        terminated: int | None = None,
        at: int | None = None,
    ) -> int:
        """Close phase `name` with exit code `rc` at `at` (epoch s; default now): the PHASE line, its record, and on a
        failure its alert. Returns the exit status: `rc` for a fatal failure, else 0."""
        secs = self.clock(at)
        self.save(running=[r for r in self.state().get("running", []) if r != name])
        if rc:
            note = f"failed (exit {rc})" + (f", {note}" if note else "")
        note = self.redact(note) if note else None
        err(phase_line(name, secs, note))
        pairs = [tuple(o.split("=", 1)) for o in outputs]
        # a failed phase's outputs are not measured: what it left is partial
        self.write(at, phase=name, note=note, store=AnyStore() if measure and not rc else None, outputs=() if rc else pairs)
        if not rc:
            return 0
        why = f", timed out after {timed_out}" if timed_out else f", terminated by {signame(terminated)}" if terminated else ""
        text = f"{name} failed (exit {rc}{why})"
        if msg:
            text += f": {msg}"
        if resume:
            text += f"\nResume: `{resume}`"
        if warn:
            self.alert("warn", text)
            return 0
        self.save(failed_phase=name)
        self.mark_alerted()  # first: a `finish` racing this post must not send a second
        self.alert("fail", text)
        return rc


@dataclass(frozen=True)
class Ran:
    """How a supervised command ended: `rc` (a signal death as 128+N; a timeout 124), whether `-T` timed it out, the
    first SIGTERM/SIGINT this process received (and forwarded), and whether its group was KILLed after the grace."""

    rc: int
    timed_out: bool = False
    signal: int | None = None
    killed: bool = False
    died_of: int | None = None  # the signal that ended CMD itself, if one did


def signame(sig: int) -> str:
    import signal

    return signal.Signals(sig).name


def run_grouped(cmd: Sequence[str], timeout: float | None, grace: float, session: bool | None = None,
                signal_grace: float | None = None) -> Ran:
    """Run `cmd`, in its own session when `session` (default: with `timeout`). Past `timeout`, TERM its whole process
    group, wait `grace` for the group to empty, then KILL what's left (coreutils `timeout -k`, but group-wide:
    multiprocessing spawn workers and Batch submitters must not outlive it); a timeout is rc 124, a missing CMD 127.

    A SIGTERM / SIGINT this process receives is forwarded at once: to CMD's group when it has its own session (nothing
    else can reach it), else to CMD (whose group the sender is presumably signalling already). With its own session,
    the group is then given `signal_grace` (default `grace`) and KILLed; without one, the sender owns escalation."""
    import signal
    import subprocess
    import time

    session = timeout is not None if session is None else session
    p: subprocess.Popen | None = None
    got: list[int] = []

    def send(sig: int) -> None:
        if p is None:
            return
        try:
            if session:
                os.killpg(p.pid, sig)
            else:
                p.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass

    def handle(sig: int, _frame) -> None:
        got.append(sig)
        send(sig)

    prev = {s: signal.signal(s, handle) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            p = subprocess.Popen(list(cmd), start_new_session=session)
        except OSError as e:
            err(f"scan-run: {cmd[0]}: {e.strerror or e}")
            return Ran(127, signal=got[0] if got else None)
        if got:  # arrived before CMD existed
            send(got[0])

        def group_alive() -> bool:
            try:
                os.killpg(p.pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True

        stop_at = time.monotonic() + timeout if timeout is not None else None
        kill_at: float | None = None
        timed_out = killed = False
        while True:
            now = time.monotonic()
            if got and kill_at is None and session:
                kill_at = now + (grace if signal_grace is None else min(grace, signal_grace))
            if stop_at is not None and not timed_out and not got and now >= stop_at:
                timed_out = True
                send(signal.SIGTERM)
                kill_at = now + grace
            rc = p.poll()  # reaps the leader, or its zombie keeps the group "alive"
            stopping = session and (timed_out or bool(got))
            if rc is not None and not (stopping and group_alive()):
                break
            if kill_at is not None and now >= kill_at and not killed:
                killed = True
                if session:
                    send(signal.SIGKILL)
                else:
                    p.kill()
            if rc is None and stop_at is None and kill_at is None:
                try:
                    p.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            else:
                time.sleep(0.05)
        raw = p.returncode
        rc = 124 if timed_out else (128 - raw if raw < 0 else raw)
        return Ran(rc, timed_out, got[0] if got else None, killed, -raw if raw < 0 else None)
    finally:
        for s, h in prev.items():
            signal.signal(s, h)


def _wrapper_opts(f):
    """The options every wrapper command takes: where the profile and the record live, and the run id."""
    for o in reversed([
        option("-c", "--profile", "profile_path", default=None, help=f"The deployment's profile JSON (default ${PROFILE_ENV})"),
        option("-d", "--d1", default=None, help="D1 database id (default $D1_DB_ID)"),
        option("-n", "--dry-run", is_flag=True, help="Print the SQL instead of writing it"),
        option("-r", "--run-id", default=None, help="The run id (default the first set of the profile's `run_id_env` vars)"),
        option("-s", "--sqlite", default=None, help="Write to this local SQLite file instead of D1"),
    ]):
        f = o(f)
    return f


def _phase_end_opts(f):
    """`phase-end`'s options, shared by the `phase -- CMD` sugar."""
    for o in reversed([
        option("-m", "--msg", default=None, help="What a failure means for users; posted in the alert (fatal or -W)"),
        option("-M", "--no-measure", is_flag=True, help="Don't measure the phase's outputs"),
        option("-o", "--output", "outputs", multiple=True, help="Measure KEY=URI as an output too (repeatable)"),
        option("-q", "--note", default=None, help="A note on the phase (after `failed (exit N)` on a failure)"),
        option("-R", "--resume", default=None, help="A hand-written resume command, quoted in the alert (opt-in; never the argv)"),
        option("-W", "--warn", is_flag=True, help="A failure is non-fatal: record it, post a `warn` alert, exit 0"),
    ]):
        f = o(f)
    return f


@cli.command("start")
@option("-B", "--from-batch", is_flag=True, help="Fill the Batch facts from the job whose uid is the run id")
@option("-g", "--gen", default=None, help="The index generation this run writes")
@option("-i", "--image", default=None, help="Job fact: the image the run ran")
@option("-j", "--job-name", default=None, help="Job fact: the job's name")
@option("-J", "--job", "batch_job_name", default=None, help="Fill the Batch facts from this GCP Batch job name (with -R)")
@option("-k", "--kind", default="scan", help="The run's kind: scan | reproc | compute | access | … (default scan)")
@option("-K", "--store", "store_key", default=None, help="The store the run belongs to (default the profile's `store`)")
@option("-m", "--machine", default=None, help="Job fact: the machine (a key of the profile's `prices` gets a cost)")
@option("-p", "--parent", default=None, help="The orchestrating run's id")
@option("-R", "--region", default=None, help="Job fact: the region (or with -J / -B where to look the job up)")
@option("--spot/--no-spot", default=None, help="Job fact: the run ran on spot capacity")
@option("-t", "--at", type=int, default=None, help="When the run started (epoch seconds; default now): e.g. its capture's start, before the job")
@option("-T", "--tasks", type=int, default=None, help="Job fact: the job's task count")
@_wrapper_opts
@argument("scan", required=False)
def start_cmd(from_batch, gen, image, job_name, batch_job_name, kind, store_key, machine, parent, region, spot, at, tasks,
              profile_path, d1, dry_run, run_id, sqlite, scan) -> None:
    """Start the job's run of SCAN (default $SCAN_ID): record it running (`record -S`) and keep its scan, kind,
    parent and job facts for the phases and `finish`. Never fails the job."""
    j = JobRun(profile_path, d1, dry_run, run_id, sqlite)
    scan = scan or j.env.get("SCAN_ID")
    j.clear()
    j.save(fresh=True, scan=scan, kind=kind, parent=parent, gen=gen, store=store_key, region=region, job_name=job_name)
    job = None
    if j.recording and j.run_id:
        try:
            job = lookup_job(j.profile, j.run_id, batch_job_name, from_batch, region)
        except (Exception, SystemExit) as e:  # noqa: BLE001
            report_failure(e)
    facts = dict(job_name=job_name, region=None if (batch_job_name or from_batch) else region, machine=machine, spot=spot,
                 tasks=tasks, image=image)
    rec = j.write(at, start=True, job=job, facts=facts)
    if rec is not None:
        j.save(region=rec.run.region, job_name=rec.run.job_name)
    j.point_supervisor(profile_path, d1, dry_run, sqlite)


@cli.command("phase-begin")
@option("-b", "--began", type=int, default=None, help="When the phase began (epoch seconds; one beside the sequence)")
@option("-g", "--gen", default=None, help="The index generation the run writes, from here on")
@_wrapper_opts
@argument("name")
def phase_begin_cmd(began, gen, profile_path, d1, dry_run, run_id, sqlite, name) -> None:
    """Phase NAME begins: `PHASE-START <name>: <N>s (wall)` on stderr (N since $DT_JOB_START; exit 2 if unset), and
    the phase recorded running."""
    JobRun(profile_path, d1, dry_run, run_id, sqlite).begin(name, began, gen)


@cli.command("phase-end")
@_phase_end_opts
@option("-t", "--at", type=int, default=None, help="When the phase ended (epoch seconds; default now): e.g. a phase that ended before the job")
@option("-x", "--exit", "rc", type=IntRange(0, 255), default=0, help="The phase's exit code (non-zero: it failed)")
@_wrapper_opts
@argument("name")
def phase_end_cmd(msg, no_measure, outputs, note, resume, warn, at, rc, profile_path, d1, dry_run, run_id, sqlite, name) -> None:
    """Phase NAME ended with exit code -x: `PHASE <name>: <N>s (wall[, note])` on stderr, the phase recorded (its
    outputs measured). A failure records the phase failed and alerts: fatal (default) posts `fail`, marks the run
    alerted and exits RC; `-W` posts `warn` and exits 0."""
    j = JobRun(profile_path, d1, dry_run, run_id, sqlite)
    sys.exit(j.end(name, rc, warn, msg, resume, note, outputs, not no_measure, at=at))


@cli.command("phase")
@_phase_end_opts
@option("-g", "--gen", default=None, help="The index generation the run writes, from here on")
@option("-G", "--grace", default="30s", help=f"With -T: how long the group has after TERM before KILL (default 30s; after a received TERM/INT at most ${TERM_GRACE_ENV})")
@option("-T", "--timeout", default=None, help="Run CMD in its own session; past this (N[s|m|h|d]) TERM then KILL its process group, exit 124")
@_wrapper_opts
@argument("name")
@argument("cmd", nargs=-1, required=True)
def phase_cmd(msg, no_measure, outputs, note, resume, warn, gen, grace, timeout, profile_path, d1, dry_run, run_id, sqlite, name, cmd) -> None:
    """`phase-begin NAME`, run CMD (after `--`), `phase-end NAME -x <its exit code>` with the same options; exits as
    `phase-end` does. A SIGTERM / SIGINT it receives goes on to CMD (to its process group, under -T, which is then
    KILLed after the grace), and the phase ends failed with CMD's status (a signal death as 128+N)."""
    j = JobRun(profile_path, d1, dry_run, run_id, sqlite)
    try:
        limit = duration(timeout) if timeout is not None else None
        grace_s = duration(grace)
    except ValueError as e:
        err(f"scan-run phase: {e}")
        sys.exit(2)
    j.begin(name, gen=gen)
    sig_grace = j.env.get(TERM_GRACE_ENV)
    ran = run_grouped(cmd, limit, grace_s, signal_grace=float(sig_grace) if sig_grace else None)
    sys.exit(j.end(name, ran.rc, warn, msg, resume, note, outputs, not no_measure, timeout if ran.timed_out else None,
                   ran.signal if ran.rc else None))


@cli.command("nop")
@_wrapper_opts
def nop_cmd(profile_path, d1, dry_run, run_id, sqlite) -> None:
    """Mark the run a no-op (its scan was already published; `record -N`). The EXIT trap's `finish -x 0` keeps it a
    NOP; a non-zero `finish` still fails it. Never fails the job."""
    JobRun(profile_path, d1, dry_run, run_id, sqlite).write(nop=True)


@cli.command("finish")
@option("-e", "--error", default=None, help="With a non-zero -x: the failure's one-line reason (the ERR trap's)")
@option("-x", "--exit", "rc", type=IntRange(0, 255), required=True, help="The job's exit code (0 = succeeded)")
@_wrapper_opts
def finish_cmd(error, rc, profile_path, d1, dry_run, run_id, sqlite) -> None:
    """The job's EXIT trap: record the run finished (`record -x`; ERR as `scan_runs.error`), and on a non-zero RC
    post the `fail` alert unless a phase already did. Never fails the job. Under `supervise`, the state and the
    alerted marker are left for it."""
    j = JobRun(profile_path, d1, dry_run, run_id, sqlite)
    j.finish(rc, error)
    if j.supervised:
        j.save(finished=rc)  # `supervise` reads it (and clears the state) once the job has exited
    else:
        j.clear()


@cli.command("supervise")
@option("-G", "--grace", default="20s", help="After a TERM/INT: how long CMD's group has before KILL (default 20s, under the usual 30s platform stop timeout)")
@argument("cmd", nargs=-1, required=True)
def supervise_cmd(grace, cmd) -> None:
    """The job's entrypoint (`supervise -- bash job.sh`): run CMD in its own session and exit with its status (a
    signal death as 128+N). A SIGTERM / SIGINT goes to CMD's whole process group at once (bash, as PID 1, would defer
    it until its foreground child exits, and the platform signals only PID 1), and what's left after the grace is
    KILLed.

    If CMD then exits non-zero, the run its `start` began is recorded failed (exit 143, or 130 for INT; error
    `terminated (SIGTERM)`) over whatever its own EXIT trap recorded: a termination wins, even over a recorded success.
    The `fail` alert posts unless one already did. CMD exiting 0 despite the signal had finished: its record
    (`succeeded`, or `nop`) stands. Without a signal, a non-zero exit that never reached `finish` (an OOM kill, no
    trap) is recorded failed too."""
    try:
        grace_s = duration(grace)
    except ValueError as e:
        err(f"scan-run supervise: {e}")
        sys.exit(2)
    ptr = Path(os.environ.get("TMPDIR") or "/tmp") / "dt-scan-run" / f"supervise-{os.getpid()}.json"
    ptr.unlink(missing_ok=True)
    os.environ[SUPERVISE_ENV] = str(ptr)
    os.environ[TERM_GRACE_ENV] = f"{grace_s / 2:g}"
    ran = run_grouped(cmd, None, grace_s, session=True)
    try:
        supervised_finish(ptr, ran, grace_s)
    except Exception as e:  # noqa: BLE001 — the job's own status passes through regardless
        report_failure(e)
    sys.exit(ran.rc)


def supervised_finish(ptr: Path, ran: Ran, grace: float) -> None:
    """`supervise`'s end: record the run failed when CMD was terminated (or died without `finish`); clear its state."""
    j = JobRun.pointed(ptr)
    ptr.unlink(missing_ok=True)
    if j is None:
        return
    finished = j.state().get("finished")
    if ran.rc and ran.signal:
        why = f"terminated ({signame(ran.signal)})" + (f"; killed after the {grace:g}s grace" if ran.killed else "")
        j.finish(128 + ran.signal, why)
    elif ran.rc and finished is None:
        j.finish(ran.rc, f"killed ({signame(ran.died_of)})" if ran.died_of else f"exit {ran.rc}, without `finish`")
    j.clear()
