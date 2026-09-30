"""Sheet mirror — opt-in "tabular site data → one Google Sheet tab, on a
schedule" (specs/done/sheet-mirror.md).

Three layers, each exercised offline by the tests:

- **Exporters** (``SOURCES``): named, thin readers of endpoints the site
  already serves, each with a fixed column contract. ``dt-cloud export``.
- **The writer's plan** (``plan_sheet``): the key-aware, cell-level diff
  ``dt-cloud sheet-push`` writes, so a tab's Version History shows one-row
  changes rather than full-range rewrites.
- **Config** (``load_config``): a deployment's ``sheet-mirror.yml`` → the
  per-mirror lines ``deploy/sheet-mirror/sync.sh`` loops over, and the
  variables ``deploy.sh`` / ``build.sh`` read. ``dt-cloud sheet-mirror``.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

import yaml
from click import ClickException

from .site import SiteError

# (endpoint path, query params) → decoded JSON. `dt-cloud export` binds it to
# `site.get_json` (token + base URL); tests inject canned responses.
Getter = Callable[[str, dict | None], Any]


class ExportError(Exception):
    """An export that can't produce its column contract (bad args, or the site
    answered with something the contract can't be read from)."""


class ConfigError(ClickException):
    """A ``sheet-mirror.yml`` that doesn't describe a runnable mirror set. A
    ``ClickException``, so the CLI reports it as one ``Error: …`` line."""


# --------------------------------------------------------------------------
# Exporters
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportArgs:
    """Knobs shared by every source (each reads only the ones it needs)."""
    date: str | None = None  # scan (YYYY-MM-DD[THHMM]); default: newest in scans.json
    executor: str | None = None  # `runs`: the site's executor route family
    subdir: str = ""  # the store's snapshot subdir under /data/ (`cw` on cw-s3)
    unit: str = "B"  # byte columns as raw bytes (`B`) or rounded `GiB` / `TiB`


@dataclass(frozen=True)
class Source:
    name: str
    columns: tuple[str, ...]
    desc: str
    fetch: Callable[[Getter, ExportArgs], list[list[Any]]]
    # Columns holding byte counts: what `unit` rescales (and relabels
    # `<col> (<unit>)`); the rest of the contract is unit-independent.
    byte_columns: tuple[str, ...] = ()


# `/api/owners` `mix` class ids → column names (STANDARD = "1"; the site's
# `CLASS_NAMES`). Fixed, so the owners contract doesn't depend on which classes
# a given scan happens to hold.
CLASS_COLUMNS: dict[str, str] = {"1": "standard", "2": "nearline", "3": "coldline", "4": "archive"}

# `Store.executor` (site/src/stores.ts) → the route family `/staged` dispatches
# to. The value isn't served by the API (it's baked into the SPA build), and
# both route families exist on every deployment — each filters Batch jobs by
# its own name prefix, so the wrong one answers an empty list rather than an
# error. Hence explicit, never guessed.
EXECUTORS: tuple[str, ...] = ("sweep", "plan-sweep")


def latest_scan(get: Getter, subdir: str = "") -> str:
    """The newest scan in the store's ``scans.json`` (newest-first)."""
    sub = f"{subdir.strip('/')}/" if subdir.strip("/") else ""
    scans = get(f"/data/{sub}scans.json", None)
    if not isinstance(scans, list) or not scans:
        raise ExportError(f"/data/{sub}scans.json lists no scans (pass -d, or -s for a store under a subdir)")
    return scans[0]


def _int(v: Any) -> int:
    return round(float(v))


def fmt_ts(epoch: int | float) -> str:
    """Epoch seconds → ``YYYY-MM-DD HH:MM`` (UTC), the sheet's time format."""
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_iso(iso: str) -> str:
    """An RFC 3339 timestamp (Batch's ``createTime``, UTC) → ``YYYY-MM-DD HH:MM``."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", iso or "")
    if not m or not iso.endswith("Z"):
        raise ExportError(f"unexpected timestamp {iso!r} (want RFC 3339 UTC)")
    return f"{m.group(1)} {m.group(2)}"


def export_owners(get: Getter, a: ExportArgs) -> list[list[Any]]:
    """Per-user owned bytes (+ storage-class split) for one scan, biggest first."""
    date = a.date or latest_scan(get, a.subdir)
    body = get("/api/owners", {"date": date})
    rows = []
    for user, owned in body["users"].items():
        mix = owned.get("mix") or {}
        unknown = sorted(set(mix) - set(CLASS_COLUMNS))
        if unknown:
            raise ExportError(f"/api/owners: user {user!r} has unknown storage class id(s) {unknown}")
        rows.append([user, _int(owned["b"]), *(_int(mix.get(k, 0)) for k in CLASS_COLUMNS)])
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def index_path(prefix: str) -> str:
    """A canonical plan prefix (``gs://bucket/dir/``) → the index path the
    subtree API takes (``bucket/dir``)."""
    return re.sub(r"^[a-z0-9]+://", "", prefix).rstrip("/")


def prefix_bytes(get: Getter, date: str, prefix: str) -> int | str:
    """Bytes under a staged prefix in ``date``'s index; ``""`` when the prefix
    isn't in that scan (404: already deleted, or staged after the scan)."""
    try:
        body = get("/api/subtree", {"date": date, "path": index_path(prefix), "w": 128, "h": 128, "depth": 1})
    except SiteError as e:
        if e.status == 404:
            return ""
        raise
    return _int(body["tree"]["b"])


def export_staged(get: Getter, a: ExportArgs) -> list[list[Any]]:
    """The shared open plan's staged prefixes, oldest first, each sized from
    the scan's index. No open plan → no rows."""
    body = get("/api/plans/staged", None)
    items = body.get("items") or []
    if not items:
        return []
    date = a.date or latest_scan(get, a.subdir)
    items = sorted(items, key=lambda it: (it["added_ts"], it["prefix"]))
    return [
        [it["prefix"], it["added_by"], fmt_ts(it["added_ts"]), it.get("note") or "", prefix_bytes(get, date, it["prefix"])]
        for it in items
    ]


def _run_key(executor: str, *, job: dict | None = None, run: dict | None = None) -> str:
    """The join key between an executor job and its D1 ``deletion_runs`` row:
    gcs (``sweep``) records the run under ``<date>-p<plan>/<stamp>`` with the
    job's run dir as ``plan``; cw (``plan-sweep``) records it under the job id."""
    if executor == "sweep":
        return (job["plan"] if job is not None else run["plan"]).rstrip("/")
    return job["job_id"] if job is not None else run["run_id"]


def export_runs(get: Getter, a: ExportArgs) -> list[list[Any]]:
    """The executor's recent Batch jobs (live state), oldest first, joined to
    their D1 ``deletion_runs`` rows (the open plan's, via ``/api/plans/staged``)
    for the dispatcher and the deleted (dry: would-delete) bytes. A job whose
    D1 row doesn't exist yet (still listing) has an empty ``deleted_bytes``."""
    if a.executor not in EXECUTORS:
        raise ExportError(f"`runs` needs -e/--executor, one of {', '.join(EXECUTORS)} (the site's `Store.executor`)")
    body = get(f"/api/{a.executor}/jobs", None)
    if not body.get("configured", False):
        raise ExportError(f"/api/{a.executor}/jobs: the executor bridge isn't configured on this site")
    staged = get("/api/plans/staged", None)
    runs = {}
    for r in staged.get("runs") or []:
        if a.executor == "sweep" and not r.get("plan"):
            continue
        runs[_run_key(a.executor, run=r)] = r
    rows = []
    for job in sorted(body["jobs"], key=lambda j: (j["created"], j["job_id"])):
        run = runs.get(_run_key(a.executor, job=job), {})
        rows.append([
            job["job_id"],
            job["mode"],
            job.get("by") or run.get("actor") or "",
            fmt_iso(job["created"]),
            job["state"],
            _int(run["deleted_bytes"]) if run.get("deleted_bytes") is not None else "",
        ])
    return rows


SOURCES: dict[str, Source] = {
    s.name: s for s in (
        Source(
            "owners",
            ("user", "bytes", *CLASS_COLUMNS.values()),
            "GET /api/owners?date=<scan>: per-user owned bytes + storage-class split (/users)",
            export_owners,
            ("bytes", *CLASS_COLUMNS.values()),
        ),
        Source(
            "staged",
            ("prefix", "staged_by", "staged_at", "note", "bytes"),
            "GET /api/plans/staged (+ /api/subtree per prefix for bytes): the shared open deletion plan (/staged)",
            export_staged,
            ("bytes",),
        ),
        Source(
            "runs",
            ("run", "mode", "by", "started", "state", "deleted_bytes"),
            "GET /api/<executor>/jobs joined to /api/plans/staged runs: recent deletion runs (needs -e)",
            export_runs,
            ("deleted_bytes",),
        ),
    )
}


# Byte-column units: divisor + the decimals kept. Rounded numbers (not
# "235 TiB" strings), so a sheet can still sort and sum them.
UNITS: dict[str, tuple[int, int]] = {"B": (1, 0), "GiB": (2**30, 1), "TiB": (2**40, 2)}


def scale_bytes(v: Any, unit: str) -> Any:
    """A byte count in ``unit``; blanks (a size that couldn't be read) stay blank."""
    div, places = UNITS[unit]
    if div == 1 or v == "" or v is None:
        return v
    return round(int(v) / div, places)


def export(source: str, get: Getter, a: ExportArgs) -> tuple[tuple[str, ...], list[list[Any]]]:
    if source not in SOURCES:
        raise ExportError(f"unknown source {source!r} (one of {', '.join(SOURCES)})")
    if a.unit not in UNITS:
        raise ExportError(f"unknown unit {a.unit!r} (one of {', '.join(UNITS)})")
    s = SOURCES[source]
    rows = s.fetch(get, a)
    for r in rows:
        if len(r) != len(s.columns):
            raise ExportError(f"{source}: row {r!r} doesn't match columns {s.columns}")
    if a.unit == "B":
        return s.columns, rows
    idx = [i for i, c in enumerate(s.columns) if c in s.byte_columns]
    columns = tuple(f"{c} ({a.unit})" if c in s.byte_columns else c for c in s.columns)
    return columns, [[scale_bytes(v, a.unit) if i in idx else v for i, v in enumerate(r)] for r in rows]


def write_csv(columns: Iterable[str], rows: Iterable[Iterable[Any]], out: IO[str]) -> None:
    w = csv.writer(out, lineterminator="\n")
    w.writerow(columns)
    w.writerows(rows)


def list_sources() -> list[str]:
    """``export --list``: one line per source, ``<name>: <columns>`` + its endpoint."""
    return [f"{s.name}: {', '.join(s.columns)}\n    {s.desc}" for s in SOURCES.values()]


# --------------------------------------------------------------------------
# The writer's plan (`sheet-push`)
# --------------------------------------------------------------------------

STAMP = re.compile(r"last change (\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)")

Grid = list[list[str]]


def at(grid: Grid, r: int, c: int) -> str:
    return grid[r][c] if r < len(grid) and c < len(grid[r]) else ""


def norm(v: str) -> tuple[str, object]:
    """Compare numerically where possible: RAW-writing ``"0.0"`` makes Sheets
    store 0 (displayed ``"0"``), so a string compare would flag every such cell
    as changed on every run."""
    v = (v or "").strip()
    try:
        return ("n", float(v))
    except ValueError:
        return ("s", v)


def blank(row: list[str]) -> bool:
    return all(not (c or "").strip() for c in row)


def split_existing(existing: Grid) -> tuple[Grid, int | None]:
    """The tab's current contents → (header + data block, footer row index).

    The footer is the last non-blank row when its column A carries a "last
    change" stamp; the block is everything above it minus the one blank
    separator row, so blank rows *inside* the block (holes a key-aware push
    left for removed keys) survive, trailing ones included. Without a footer,
    trailing blank rows are dropped."""
    last = max((i for i, row in enumerate(existing) if not blank(row)), default=None)
    if last is not None and STAMP.search(at(existing, last, 0)):
        end = last - 1 if last >= 1 and blank(existing[last - 1]) else last
        return existing[:end], last
    return (existing[: last + 1] if last is not None else []), None


def place(rows: Grid, key: str, block: Grid) -> Grid:
    """Key-aware target block (header + data): existing rows keep their
    positions (by ``key``), a removed key's row becomes a hole (``[]``,
    cleared), and new keys are appended in their source order. No usable
    prior layout (empty tab, or its header lacks ``key``) → source order."""
    header, data = rows[0], rows[1:]
    if key not in header:
        raise ValueError(f"key column {key!r} not in header {header}")
    k = header.index(key)
    by_key: dict[tuple[str, object], list[str]] = {}
    for r in data:
        kv = norm(at([r], 0, k))
        if kv == ("s", ""):
            raise ValueError(f"row {r} has an empty {key!r}")
        if kv in by_key:
            raise ValueError(f"duplicate {key!r} {at([r], 0, k)!r}")
        by_key[kv] = r
    old_header = [c.strip() for c in block[0]] if block else []
    slots: list[tuple[str, object] | None] = []
    placed: set[tuple[str, object]] = set()
    if key in old_header:
        ok = old_header.index(key)
        for r in block[1:]:
            kv = norm(at([r], 0, ok))
            keep = kv in by_key and kv not in placed
            slots.append(kv if keep else None)
            if keep:
                placed.add(kv)
    slots += [kv for kv in by_key if kv not in placed]
    return [list(header), *(list(by_key[s]) if s is not None else [] for s in slots)]


@dataclass(frozen=True)
class SheetPlan:
    """What one push writes: ``cells`` are 1-based ``(row, col, value)``,
    only those whose value differs from the tab's (empty = a no-op run)."""
    cells: list[tuple[int, int, str]]
    target: Grid
    data_rows: int
    data_changed: bool
    compacted: bool = False
    holes: int = 0


def plan_sheet(
    existing: Grid,
    rows: Grid,
    now: str,
    key: str | None = None,
    disclaimer: str | None = None,
) -> SheetPlan:
    """Diff ``rows`` (header + data) against the tab's ``existing`` values.

    Without ``key`` the layout is positional (the table as given, at A1).
    With ``key`` it is ``place``'s: one added key = one new row, one removed
    key = one cleared row. Holes are compacted away only on a run whose data
    is otherwise unchanged, so the layout shift lands as its own version.

    ``disclaimer`` goes two rows below the block with a "; last change <ts>"
    stamp that advances (to ``now``) only when the data block changed —
    parsed back from the prior footer otherwise — so a no-op run writes
    nothing at all."""
    block, _ = split_existing(existing)
    target_block = place(rows, key, block) if key else [list(r) for r in rows]
    width = max((len(r) for r in (*target_block, *block)), default=0)
    data_changed = any(
        norm(at(target_block, r, c)) != norm(at(block, r, c))
        for r in range(max(len(target_block), len(block)))
        for c in range(width)
    )
    holes = sum(1 for r in target_block[1:] if not r)
    compacted = bool(key) and not data_changed and holes > 0
    if compacted:
        target_block = [target_block[0], *(r for r in target_block[1:] if r)]
        holes = 0

    target: Grid = [list(r) for r in target_block]
    if disclaimer:
        prior = next((m.group(1) for row in existing for cell in row if (m := STAMP.search(cell or ""))), None)
        stamp = now if (data_changed or prior is None) else prior
        target += [[], [f"{disclaimer}; last change {stamp}"]]

    n_rows = max(len(target), len(existing))
    n_cols = max((len(r) for r in (*target, *existing)), default=0)
    cells = [
        (r + 1, c + 1, at(target, r, c))
        for r in range(n_rows)
        for c in range(n_cols)
        if norm(at(target, r, c)) != norm(at(existing, r, c))
    ]
    return SheetPlan(
        cells=cells,
        target=target,
        data_rows=len(rows) - 1,
        data_changed=data_changed,
        compacted=compacted,
        holes=holes,
    )


class Worksheet(Protocol):
    """The slice of ``gspread.Worksheet`` a push uses."""
    title: str

    def get_all_values(self) -> Grid: ...

    def update_cells(self, cells: list, value_input_option: str = ...) -> Any: ...


def push(
    ws: Worksheet,
    rows: Grid,
    now: str,
    key: str | None = None,
    disclaimer: str | None = None,
    cell: Callable[[int, int, str], Any] = lambda r, c, v: (r, c, v),
) -> SheetPlan:
    """Plan against ``ws``'s current values and write only the changed cells
    (RAW; ``cell`` builds each — ``gspread.Cell`` in the CLI)."""
    plan = plan_sheet(ws.get_all_values(), rows, now, key=key, disclaimer=disclaimer)
    if plan.cells:
        ws.update_cells([cell(r, c, v) for r, c, v in plan.cells], value_input_option="RAW")
    return plan


# --------------------------------------------------------------------------
# Config (`sheet-mirror.yml`)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Mirror:
    sheet: str
    tab: str
    source: str
    key: str
    footer: str = ""
    executor: str = ""
    unit: str = "B"


@dataclass(frozen=True)
class Gcp:
    project: str
    service_account: str
    job: str
    region: str = "us-central1"
    image: str = ""
    trigger: str = ""

    @property
    def image_ref(self) -> str:
        return self.image or f"{self.region}-docker.pkg.dev/{self.project}/cloud-run-source-deploy/{self.job}:latest"

    @property
    def trigger_name(self) -> str:
        return self.trigger or f"{self.job}-trigger"


@dataclass(frozen=True)
class MirrorConfig:
    site: str
    token_secret: str
    schedule: str
    mirrors: tuple[Mirror, ...]
    subdir: str = ""
    gcp: Gcp | None = None


TOP_KEYS = {"site", "token_secret", "schedule", "subdir", "gcp", "mirrors"}
MIRROR_KEYS = {"sheet", "tab", "source", "key", "footer", "executor", "unit"}
GCP_KEYS = {"project", "region", "service_account", "job", "image", "trigger"}


def _str(d: dict, k: str, where: str, required: bool = True) -> str:
    v = d.get(k)
    if v is None:
        if required:
            raise ConfigError(f"{where}: `{k}` is required")
        return ""
    if not isinstance(v, (str, int)) or isinstance(v, bool):
        raise ConfigError(f"{where}: `{k}` must be a string, got {v!r}")
    v = str(v)
    if "\n" in v or "\r" in v:
        raise ConfigError(f"{where}: `{k}` must be one line")
    return v


def _keys(d: Any, allowed: set[str], where: str) -> dict:
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: expected a mapping, got {d!r}")
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown} (allowed: {sorted(allowed)})")
    return d


ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(doc: Any, env: Mapping[str, str]) -> Any:
    """Substitute ``${NAME}`` in every string value of a parsed config, from
    ``env``. Strict: an unset NAME is a :class:`ConfigError` (listing every
    missing one), never a silent blank — a mirror aimed at sheet ``""`` would
    only fail later, in the job. Values, not raw YAML text, so a substituted
    value can't inject structure. This is how a public repo's config names a
    sheet without committing its id (``sheet: ${GCS_SHEET_ID}``, the value in
    the deployer's untracked ``.envrc``)."""
    missing: set[str] = set()

    def sub(v: Any) -> Any:
        if isinstance(v, str):
            def one(m: re.Match) -> str:
                if m.group(1) not in env:
                    missing.add(m.group(1))
                    return ""
                return env[m.group(1)]
            return ENV_REF.sub(one, v)
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v

    out = sub(doc)
    if missing:
        raise ConfigError(f"sheet-mirror.yml: unset environment variable(s) {sorted(missing)}")
    return out


def render_config(text: str, env: Mapping[str, str] | None = None) -> str:
    """The config with ``${NAME}``s substituted, as YAML — what ``deploy.sh``
    bakes into the job (validated first, so a bad render never ships)."""
    import os  # noqa: PLC0415

    doc = expand_env(yaml.safe_load(text), os.environ if env is None else env)
    out = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    load_config(out, env={})
    return out


def load_config(text: str, env: Mapping[str, str] | None = None) -> MirrorConfig:
    """Parse + validate a ``sheet-mirror.yml``, substituting ``${NAME}`` from
    ``env`` (default: the process environment). Every mirror must name a known
    source, a ``key`` among that source's columns, and (``runs`` only) an
    executor; no two mirrors may write the same (sheet, tab)."""
    import os  # noqa: PLC0415

    doc = _keys(expand_env(yaml.safe_load(text), os.environ if env is None else env), TOP_KEYS, "sheet-mirror.yml")
    site = _str(doc, "site", "sheet-mirror.yml").rstrip("/")
    raw = doc.get("mirrors")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("sheet-mirror.yml: `mirrors` must be a non-empty list (no mirrors = don't deploy one)")
    mirrors = []
    seen: set[tuple[str, str]] = set()
    for i, m in enumerate(raw):
        where = f"mirrors[{i}]"
        _keys(m, MIRROR_KEYS, where)
        mirror = Mirror(
            sheet=_str(m, "sheet", where),
            tab=_str(m, "tab", where),
            source=_str(m, "source", where),
            key=_str(m, "key", where),
            footer=_str(m, "footer", where, required=False).replace("{site}", site),
            executor=_str(m, "executor", where, required=False),
            unit=_str(m, "unit", where, required=False) or "B",
        )
        if mirror.unit not in UNITS:
            raise ConfigError(f"{where}: unknown unit {mirror.unit!r} (one of {', '.join(UNITS)})")
        if mirror.source not in SOURCES:
            raise ConfigError(f"{where}: unknown source {mirror.source!r} (one of {', '.join(SOURCES)})")
        if mirror.key not in SOURCES[mirror.source].columns:
            raise ConfigError(f"{where}: key {mirror.key!r} is not a {mirror.source} column {SOURCES[mirror.source].columns}")
        if mirror.source == "runs" and mirror.executor not in EXECUTORS:
            raise ConfigError(f"{where}: `runs` needs `executor`, one of {', '.join(EXECUTORS)}")
        if mirror.source != "runs" and mirror.executor:
            raise ConfigError(f"{where}: `executor` only applies to the `runs` source")
        if (mirror.sheet, mirror.tab) in seen:
            raise ConfigError(f"{where}: another mirror already writes sheet {mirror.sheet} tab {mirror.tab!r}")
        seen.add((mirror.sheet, mirror.tab))
        mirrors.append(mirror)
    gcp = None
    if doc.get("gcp") is not None:
        g = _keys(doc["gcp"], GCP_KEYS, "gcp")
        gcp = Gcp(
            project=_str(g, "project", "gcp"),
            service_account=_str(g, "service_account", "gcp"),
            job=_str(g, "job", "gcp"),
            region=_str(g, "region", "gcp", required=False) or "us-central1",
            image=_str(g, "image", "gcp", required=False),
            trigger=_str(g, "trigger", "gcp", required=False),
        )
    return MirrorConfig(
        site=site,
        token_secret=_str(doc, "token_secret", "sheet-mirror.yml"),
        schedule=_str(doc, "schedule", "sheet-mirror.yml"),
        mirrors=tuple(mirrors),
        subdir=_str(doc, "subdir", "sheet-mirror.yml", required=False),
        gcp=gcp,
    )


def read_config(path: str) -> MirrorConfig:
    import sys  # noqa: PLC0415

    return load_config(sys.stdin.read() if path == "-" else Path(path).read_text())


def _assign(pairs: Iterable[tuple[str, str]]) -> str:
    return " ".join(f"{k}={shlex.quote(v)}" for k, v in pairs)


def plan_lines(cfg: MirrorConfig) -> list[str]:
    """One line per mirror of shell-quoted assignments, for ``sync.sh`` to
    ``eval`` in a loop (every value passes through ``shlex.quote``)."""
    return [
        _assign([
            ("source", m.source),
            ("site", cfg.site),
            ("subdir", cfg.subdir),
            ("sheet", m.sheet),
            ("tab", m.tab),
            ("key", m.key),
            ("footer", m.footer),
            ("executor", m.executor),
            ("unit", m.unit),
        ])
        for m in cfg.mirrors
    ]


def env_lines(cfg: MirrorConfig) -> list[str]:
    """The deploy-side variables (``KEY=<quoted>``, one per line) that
    ``build.sh`` / ``deploy.sh`` ``eval``. Needs the config's ``gcp`` block."""
    if cfg.gcp is None:
        raise ConfigError("sheet-mirror.yml: `gcp` (project, service_account, job[, region, image, trigger]) is required to build/deploy")
    g = cfg.gcp
    return [
        _assign([(k, v)])
        for k, v in (
            ("SITE", cfg.site),
            ("TOKEN_SECRET", cfg.token_secret),
            ("SCHEDULE", cfg.schedule),
            ("PROJECT", g.project),
            ("REGION", g.region),
            ("SA", g.service_account),
            ("JOB", g.job),
            ("TRIGGER", g.trigger_name),
            ("IMAGE", g.image_ref),
        )
    ]
