"""Scan runs (specs/scan-runs-ui.md): what each scan job did — when it ran, its phases, what it wrote — recorded
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

from .scan_id import check_scan_id

err = partial(print, file=sys.stderr)

PROFILE_ENV = "SCAN_RUNS_PROFILE"
MIGRATION = "0016_scan_runs.sql"
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


def migration_sql(site: Path | None = None) -> str:
    """The tables' DDL, from the migration file (the one source of the schema)."""
    if site is None:
        site = Path(__file__).resolve().parents[3] / "site"  # the repo checkout this module runs from
        if not site.is_dir():
            from .index_footer import _site_dir

            site = _site_dir()
    return (site / "migrations" / "cw" / MIGRATION).read_text()


class SqliteSink:
    """A local SQLite file with foreign keys ON (as D1 enforces them); the tables are created from the migration
    when absent."""

    def __init__(self, path: str | Path, ddl: str | None = None):
        self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        if not self.con.execute("SELECT 1 FROM sqlite_master WHERE name = 'scan_runs'").fetchone():
            self.con.executescript(ddl if ddl is not None else migration_sql())

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
    phase_marker: str = r"^PHASE (?P<name>[^:]+): \d+s(?: \((?P<note>[^)]*)\))?"
    #: Markers that are not phases (a final total).
    phase_ignore: tuple[str, ...] = ("total",)
    #: Phases that run beside the others (from the start, or from `record -b`; their marker is where they joined), by name.
    overlapped: tuple[str, ...] = ()
    #: Phases that run beside each other after the serial phase before them: each starts where the last phase
    #: outside this set (and `overlapped`) ended, and the next serial phase starts where the last of them ended.
    concurrent: tuple[str, ...] = ()
    #: A log line meaning the run did nothing (an already-published scan): group 1 the scan id.
    nop_marker: str | None = None
    #: A log line naming a failure (the ERR trap's xtrace), groups `rc`, `line`, `cmd`.
    error_marker: str | None = None
    #: On-demand $/h by machine type; `spot_factor` scales it for SPOT VMs. A machine not here has no cost estimate.
    prices: Mapping[str, float] = field(default_factory=dict)
    spot_factor: float | None = None
    #: How far back the backfill reads (Cloud Logging keeps 30 d by default; Batch keeps its jobs).
    log_days: int = 30

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
) -> Record:
    """One incremental write of run `run_id` (scan `scan`): start it, close a phase (measuring the outputs tied to
    it), measure named outputs, finish it (`exit_code`, or `nop`). The run row is upserted every time, so any call
    may come first. Returns what was written."""
    check_scan_id(scan)
    row, phases = stored(sink, run_id)
    started = row.get("started_ts") if row else None
    status = row["status"] if row and row["status"] in ("nop", "failed", "succeeded") else "running"
    if nop:
        status = "nop"
    elif exit_code is not None and not (exit_code == 0 and status == "nop"):  # the exit trap after a NOP keeps it
        status = "succeeded" if exit_code == 0 else "failed"
    elif start or phase:
        status = "running" if status != "nop" else status
    run = Run(run_id=run_id, scan=scan, kind=kind, status=status, store=profile.store, parent=parent,
              started_ts=now if start and not started else None, gen=gen, source="job")
    if exit_code is not None or nop:
        run = replace(run, finished_ts=now)
        if exit_code:
            run = replace(run, failed_phase=failed_phase, error=error or f"exit {exit_code}")
    if job:
        # only what the job knows: one read just after start has no RUNNING event yet, and its None must not clear
        # the start `-S` stamped
        run = replace(run, **{k: v for k, v in job_fields(profile, job, now).items() if v is not None})
    new_phases: list[Phase] = []
    if phase:
        skip = profile.overlapped + (profile.concurrent if phase in profile.concurrent else ())
        prev_end = max((p["finished_ts"] for p in phases if p.get("finished_ts") and p["phase"] not in skip), default=None)
        begin = run.started_ts or started
        p_start = began if began is not None else begin if phase in profile.overlapped else (prev_end or begin)
        seq = next((p["seq"] for p in phases if p["phase"] == phase), len(phases))
        new_phases.append(Phase(phase, seq, p_start, now, "done", note))
    outs: list[Output] = []
    gen_now = gen or (row.get("gen") if row else None)
    if store is not None and phase:
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
    """Scan runs: what each scan job did (specs/scan-runs-ui.md), recorded in the site's D1."""


@cli.command("record")
@option("-b", "--began", type=int, default=None, help="With -P: when the phase began (epoch seconds), for one run beside the main sequence (list it in the profile's `overlapped`)")
@option("-c", "--profile", "profile_path", default=None, help=f"The deployment's profile JSON (default ${PROFILE_ENV})")
@option("-d", "--d1", default=None, help="D1 database id (default $D1_DB_ID)")
@option("-e", "--error", default=None, help="With a non-zero -x: the failure's one-line reason (default `exit <rc>`)")
@option("-F", "--failed-phase", default=None, help="With a non-zero -x: the phase that was running")
@option("-g", "--gen", default=None, help="The index generation this run writes (fills `{gen}` in output templates)")
@option("-B", "--from-batch", is_flag=True, help="Fill the Batch facts (machine, SPOT, tasks, cost) from the job whose uid is the run id, found in the profile's regions")
@option("-J", "--job", "job_name", default=None, help="Fill the Batch facts from this job name (with -R)")
@option("-k", "--kind", default="scan", help="The run's kind (a profile job kind; default scan)")
@option("-M", "--no-measure", is_flag=True, help="Don't measure outputs (record timings only)")
@option("-n", "--dry-run", is_flag=True, help="Print the SQL instead of writing it")
@option("-N", "--nop", is_flag=True, help="Finish the run as a no-op (the scan was already published)")
@option("-o", "--output", "outputs", multiple=True, help="Measure KEY=URI as an output too (repeatable)")
@option("-p", "--parent", default=None, help="The orchestrating run (a downstream job's)")
@option("-P", "--phase", default=None, help="A phase just ended: record it (it began where the last one ended) and measure the outputs the profile ties to it")
@option("-q", "--note", default=None, help="With -P: a note on the phase")
@option("-r", "--run-id", default=None, help="The run id (default $BATCH_JOB_UID)")
@option("-R", "--region", default=None, help="The job's Batch region (with -J; default the profile's first)")
@option("-S", "--start", is_flag=True, help="Mark the run started now (status running)")
@option("-s", "--sqlite", default=None, help="Write to this local SQLite file (created from the migration) instead of D1")
@option("-x", "--exit", "exit_code", type=IntRange(0, 255), default=None, help="Finish the run with this exit code (0 = succeeded)")
@argument("scan")
def record_cmd(profile_path, began, d1, from_batch, error, failed_phase, gen, job_name, kind, no_measure, dry_run, nop, outputs, parent,
               phase, note, run_id, region, start, sqlite, exit_code, scan) -> None:
    """Record one step of SCAN's run, from the job: `-S` at its start, `-P NAME` as each phase ends, `-x RC` from
    its exit trap. Never fails the job it reports on: an error is printed and the exit status is 0."""
    import time

    try:
        profile = load_profile(profile_path)
        run_id = run_id or os.environ.get("BATCH_JOB_UID", "").strip()
        if not run_id:
            raise SystemExit("scan-run: no run id: pass -r or run inside a Batch job ($BATCH_JOB_UID)")
        sink = sink_for(dry_run, sqlite, d1)
        store = None if no_measure else AnyStore()
        job = None
        if job_name:
            from .gcp import batch_job

            job = batch_job(job_name, profile.project, region or (profile.regions[0] if profile.regions else "us-central1"))
        elif from_batch:
            from .scan_runs_backfill import fetch_jobs

            job = next((j for j in fetch_jobs(profile.project or "", [region] if region else profile.regions) if j.get("uid") == run_id), None)
            if job is None:
                err(f"scan-run: no Batch job with uid {run_id} in {region or profile.regions}")
        pairs = [tuple(o.split("=", 1)) for o in outputs]
        rec = record(sink, profile, scan, run_id, int(time.time()), store=store, kind=kind, start=start, phase=phase,
                     note=note, began=began, outputs=pairs, gen=gen, exit_code=exit_code, nop=nop, failed_phase=failed_phase,
                     error=error, parent=parent, job=job)
        err(f"scan-run: {scan} {run_id} {rec.run.status}" + (f" · phase {phase}" if phase else "")
            + (f" · {len(rec.outputs)} outputs" if rec.outputs else ""))
    except (Exception, SystemExit) as e:  # noqa: BLE001 — reporting must never fail the scan
        err(f"scan-run record failed (the scan continues): {type(e).__name__}: {e}")


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
