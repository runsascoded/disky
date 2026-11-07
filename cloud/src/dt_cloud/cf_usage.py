"""`dt-cloud cf-usage`: what a deployment's Cloudflare load costs, and who drives it (specs/usage-metrics.md phase 2).

Two sources, per day:
1. Cloudflare's billed totals, through the GraphQL Analytics API (`/client/v4/graphql`), one query per dataset,
   filtered to the deployment's own resources as its wrangler config names them: Pages Functions requests (and an
   estimate of their CPU time) of the project's two scripts (`CF_PAGES_SCRIPT`: production + preview), R2 Class A /
   B operations and storage per bucket, D1 rows read / written per database, KV operations and storage per
   namespace. Each becomes list-price dollars through `PRICES`.
2. Our attribution: the site's usage points (`site/functions/_lib/usage.ts`) in its Analytics Engine dataset,
   through the Analytics Engine SQL API, summed by caller and by route (`SUM(_sample_interval * …)`: points are
   sampled at volume).

Both need an API token with **Account Analytics: Read** on the account (`$CF_ANALYTICS_TOKEN`). The parsers take
the APIs' JSON as given (`cloud/tests/fixtures/cf_usage/` are recorded in their documented shapes), so the math is
tested without one.

`-o ROOT` (repeatable) writes one snapshot per day, `<ROOT>/<date>.json` (`snapshot`, the shape `SNAPSHOT_VERSION`
names), for the window's days missing from any root: the daily job's `cf-usage` step (`scan_steps`) backfills that way,
and the site's `/admin/usage` reads them (`site/functions/api/admin/usage.ts`).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tomllib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from click import Choice, Path as ClickPath, UsageError, command, option

from .secrets import env_secret


def err(*a: object) -> None:
    print(*a, file=sys.stderr)

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
TOKEN_ENV = "CF_ANALYTICS_TOKEN"
TIMEOUT = 60


@dataclass(frozen=True)
class Price:
    """One billed unit: list price per `per` units, and the monthly amount the Workers Paid plan includes."""

    usd: float
    per: float
    included: float
    label: str


#: List prices (Workers Paid; R2 Standard storage), USD. Storage units are GB-months: a day's GB stored costs
#: `gb × usd / 30`. The included amounts are per month; a window is compared with them pro rata (`overage`).
PRICES: dict[str, Price] = {
    "functions_requests": Price(0.30, 1e6, 10e6, "Functions requests"),
    "functions_cpu_ms": Price(0.02, 1e6, 30e6, "Functions CPU ms (est.)"),
    "r2_class_a": Price(4.50, 1e6, 1e6, "R2 Class A ops"),
    "r2_class_b": Price(0.36, 1e6, 10e6, "R2 Class B ops"),
    "r2_storage_gb": Price(0.015, 1, 10, "R2 storage GB-mo"),
    "d1_rows_read": Price(0.001, 1e6, 25e9, "D1 rows read"),
    "d1_rows_written": Price(1.00, 1e6, 50e6, "D1 rows written"),
    "kv_reads": Price(0.50, 1e6, 10e6, "KV reads"),
    "kv_writes": Price(5.00, 1e6, 1e6, "KV writes/deletes/lists"),
    "kv_storage_gb": Price(0.50, 1, 1, "KV storage GB-mo"),
}
#: The units billed as GB-months (one day's value is a level, not a count).
STORAGE_UNITS = {"r2_storage_gb", "kv_storage_gb"}

#: R2's operation classes by `actionType` (anything else, e.g. DeleteObject, is free).
R2_CLASS_A = {
    "ListBuckets", "PutBucket", "ListObjects", "PutObject", "CopyObject", "CompleteMultipartUpload",
    "CreateMultipartUpload", "LifecycleStorageTierTransition", "ListMultipartUploads", "UploadPart", "UploadPartCopy",
    "ListParts", "PutBucketEncryption", "PutBucketCors", "PutBucketLifecycleConfiguration",
}
R2_CLASS_B = {
    "HeadBucket", "HeadObject", "GetObject", "UsageSummary", "GetBucketEncryption", "GetBucketLocation",
    "GetBucketCors", "GetBucketLifecycleConfiguration",
}


@dataclass(frozen=True)
class Deployment:
    """The Cloudflare resources a deployment's wrangler config names."""

    account: str
    project: str
    buckets: tuple[str, ...]
    databases: tuple[str, ...]
    namespaces: tuple[str, ...]
    dataset: str | None
    #: The Pages project's Functions script stem, `pages-worker--<n>`: the Functions dataset names a project's scripts
    #: `<stem>-production` / `<stem>-preview`, never by project name, and only the Pages API (which an analytics token
    #: can't read) maps one to the other (`production_script_name`). None: the account's total, with a note.
    script: str | None = None


def load_deployment(path: Path, account: str | None = None) -> Deployment:
    """`path`'s (a Pages `wrangler.toml`) project, R2 buckets, D1 databases, KV namespaces and usage dataset, the
    top level's and `[env.preview]`'s together; the account from `account`, else `$CLOUDFLARE_ACCOUNT_ID`, else
    its `[vars]` `CF_ACCOUNT_ID`; the Functions script stem from its `[vars]` `CF_PAGES_SCRIPT`."""
    cfg = tomllib.loads(path.read_text())
    envs = [cfg, *cfg.get("env", {}).values()]
    def uniq(key: str, field_: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(b[field_] for e in envs for b in e.get(key, []) if b.get(field_)))
    acct = account or env_secret("CLOUDFLARE_ACCOUNT_ID") or cfg.get("vars", {}).get("CF_ACCOUNT_ID")
    if not acct:
        raise UsageError(f"no account: pass -a, set $CLOUDFLARE_ACCOUNT_ID, or set CF_ACCOUNT_ID in {path}'s [vars]")
    datasets = uniq("analytics_engine_datasets", "dataset")
    return Deployment(
        account=acct,
        project=cfg["name"],
        buckets=uniq("r2_buckets", "bucket_name"),
        databases=uniq("d1_databases", "database_id"),
        namespaces=uniq("kv_namespaces", "id"),
        dataset=datasets[0] if datasets else None,
        script=cfg.get("vars", {}).get("CF_PAGES_SCRIPT") or None,
    )


# --- GraphQL ------------------------------------------------------------------------------------------------

def _q(dataset: str, filt: str, fields: str, extra_vars: str = "") -> str:
    return (
        f"query($accountTag: string!, $since: Time!, $until: Time!, $sinceDate: Date!, $untilDate: Date!{extra_vars}) {{\n"
        f"  viewer {{ accounts(filter: {{accountTag: $accountTag}}) {{\n"
        f"    {dataset}(limit: 10000, filter: {{{filt}}}) {{ {fields} }}\n"
        f"  }} }}\n}}"
    )


_TIME = "datetime_geq: $since, datetime_lt: $until"
_DATE = "date_geq: $sinceDate, date_lt: $untilDate"

#: The CPU-time quantiles the Functions dataset has (µs), at their probabilities: it has no CPU sum (nor does any
#: other dataset for a Pages script: `workersInvocationsAdaptive`'s `cpuTimeUs` covers Workers only).
CPU_QUANTILES = (("cpuTimeP25", 0.25), ("cpuTimeP50", 0.5), ("cpuTimeP75", 0.75), ("cpuTimeP90", 0.9),
                 ("cpuTimeP95", 0.95), ("cpuTimeP99", 0.99), ("cpuTimeP999", 0.999))
_FN_FIELDS = f"sum {{ requests wallTime }} quantiles {{ {' '.join(q for q, _ in CPU_QUANTILES)} }}"

#: The deployment's two scripts, hour by hour (the finer the group, the closer its quantiles' mean to its CPU sum).
FUNCTIONS_SCOPED = _q(
    "pagesFunctionsInvocationsAdaptiveGroups", f"{_TIME}, scriptName_in: $scripts",
    f"{_FN_FIELDS} dimensions {{ date datetimeHour scriptName }}", ", $scripts: [string!]",
)
#: Every script's day by day (no stem: the account's total; hourly groups of every script would pass the limit).
FUNCTIONS_ACCOUNT = _q("pagesFunctionsInvocationsAdaptiveGroups", _TIME, f"{_FN_FIELDS} dimensions {{ date scriptName }}")

#: One query per dataset (a dataset the token or plan can't read fails alone); `functions` is `FUNCTIONS_SCOPED` or
#: `FUNCTIONS_ACCOUNT`, by whether the deployment names its script.
QUERIES: dict[str, str] = {
    "functions": FUNCTIONS_SCOPED,
    "r2_ops": _q(
        "r2OperationsAdaptiveGroups", f"{_TIME}, bucketName_in: $buckets",
        "sum { requests } dimensions { date bucketName actionType }", ", $buckets: [string!]",
    ),
    "r2_storage": _q(
        "r2StorageAdaptiveGroups", f"{_TIME}, bucketName_in: $buckets",
        "max { payloadSize metadataSize objectCount } dimensions { date bucketName }", ", $buckets: [string!]",
    ),
    "d1": _q(
        "d1AnalyticsAdaptiveGroups", f"{_DATE}, databaseId_in: $databases",
        "sum { readQueries writeQueries rowsRead rowsWritten } dimensions { date databaseId }", ", $databases: [string!]",
    ),
    "kv_ops": _q(
        "kvOperationsAdaptiveGroups", f"{_DATE}, namespaceId_in: $namespaces",
        "sum { requests } dimensions { date namespaceId actionType }", ", $namespaces: [string!]",
    ),
    "kv_storage": _q(
        "kvStorageAdaptiveGroups", f"{_DATE}, namespaceId_in: $namespaces",
        "max { byteCount keyCount } dimensions { date namespaceId }", ", $namespaces: [string!]",
    ),
}
#: Each query's resources: skipped when the deployment has none.
NEEDS = {"r2_ops": "buckets", "r2_storage": "buckets", "d1": "databases", "kv_ops": "namespaces", "kv_storage": "namespaces"}


def window(days: int, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """The `days` whole UTC days before `today` (default: today's UTC date): `[since, until)`."""
    until = today or dt.datetime.now(dt.UTC).date()
    return until - dt.timedelta(days=days), until


def gql_variables(dep: Deployment, since: dt.date, until: dt.date) -> dict[str, Any]:
    return {
        "accountTag": dep.account,
        "since": f"{since.isoformat()}T00:00:00Z",
        "until": f"{until.isoformat()}T00:00:00Z",
        "sinceDate": since.isoformat(),
        "untilDate": until.isoformat(),
        "buckets": list(dep.buckets),
        "databases": list(dep.databases),
        "namespaces": list(dep.namespaces),
        **({"scripts": [f"{dep.script}-production", f"{dep.script}-preview"]} if dep.script else {}),
    }


Fetch = Callable[[str, str, bytes, str], Any]
"""`(url, token, body, content_type) → parsed JSON`; tests pass a fixture reader."""


def http_fetch(url: str, token: str, body: bytes, content_type: str) -> Any:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": content_type})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url}: HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e


def _groups(resp: dict, dataset: str) -> list[dict]:
    """A GraphQL response's groups for `dataset` (one account); its `errors` raised."""
    if resp.get("errors"):
        raise RuntimeError("; ".join(e.get("message", str(e)) for e in resp["errors"]))
    accounts = (resp.get("data") or {}).get("viewer", {}).get("accounts") or []
    return [g for a in accounts for g in a.get(dataset) or []]


Units = dict[str, dict[str, float]]
"""date → unit → amount."""


def mean_cpu_us(quantiles: dict[str, float]) -> float:
    """A group's mean CPU µs from its quantiles: the quantile function integrated by trapezoids, from 0 at p=0 to
    p99.9 held to p=1. The tail carries the mean (gcs, 2026-10-14: p50 33 ms, p90 3.2 s, mean ≈ 1 s), so a median
    under-counts it ~30×; the open ends bias this low, and the caller caps it at the group's wall time."""
    pts = [(0.0, 0.0), *((p, float(quantiles.get(q) or 0)) for q, p in CPU_QUANTILES)]
    pts.append((1.0, pts[-1][1]))
    return sum((p1 - p0) * (v0 + v1) / 2 for (p0, v0), (p1, v1) in zip(pts, pts[1:]))


FunctionsSplit = dict[str, dict[str, dict[str, float]]]
"""date → script env (`production` / `preview`; `account` unscoped) → `{requests, cpu_ms}`."""


def parse_functions(resp: dict, script: str | None) -> tuple[Units, FunctionsSplit]:
    """Pages Functions requests and an estimate of their CPU ms (requests × `mean_cpu_us`, at most the group's wall
    time; the dataset has no CPU sum) per day, in total and per script env: `script`'s two (`<script>-<env>`), or,
    with no `script`, every script's as `account`."""
    out: Units = defaultdict(lambda: defaultdict(float))
    split: FunctionsSplit = defaultdict(lambda: defaultdict(lambda: {"requests": 0.0, "cpu_ms": 0.0}))
    for g in _groups(resp, "pagesFunctionsInvocationsAdaptiveGroups"):
        name = g["dimensions"].get("scriptName") or ""
        if script and not name.startswith(f"{script}-"):
            continue
        env = name[len(script) + 1:] if script else "account"
        d, s = g["dimensions"]["date"], g["sum"]
        cpu_ms = s["requests"] * mean_cpu_us(g.get("quantiles") or {}) / 1000
        if s.get("wallTime") is not None:
            cpu_ms = min(cpu_ms, s["wallTime"] / 1000)  # wall time is µs
        out[d]["functions_requests"] += s["requests"]
        out[d]["functions_cpu_ms"] += cpu_ms
        split[d][env]["requests"] += s["requests"]
        split[d][env]["cpu_ms"] += cpu_ms
    return out, {d: {e: dict(v) for e, v in sorted(es.items())} for d, es in sorted(split.items())}


def parse_r2_ops(resp: dict) -> Units:
    out: Units = defaultdict(lambda: defaultdict(float))
    for g in _groups(resp, "r2OperationsAdaptiveGroups"):
        d, action, n = g["dimensions"]["date"], g["dimensions"]["actionType"], g["sum"]["requests"]
        if action in R2_CLASS_A:
            out[d]["r2_class_a"] += n
        elif action in R2_CLASS_B:
            out[d]["r2_class_b"] += n
    return out


def parse_r2_storage(resp: dict) -> Units:
    """Each day's stored GB (payload + metadata, the day's max per bucket, summed over buckets)."""
    out: Units = defaultdict(lambda: defaultdict(float))
    for g in _groups(resp, "r2StorageAdaptiveGroups"):
        m = g["max"]
        out[g["dimensions"]["date"]]["r2_storage_gb"] += (m["payloadSize"] + m.get("metadataSize", 0)) / 1e9
    return out


def parse_d1(resp: dict) -> Units:
    out: Units = defaultdict(lambda: defaultdict(float))
    for g in _groups(resp, "d1AnalyticsAdaptiveGroups"):
        d, s = g["dimensions"]["date"], g["sum"]
        out[d]["d1_rows_read"] += s["rowsRead"]
        out[d]["d1_rows_written"] += s["rowsWritten"]
    return out


def parse_kv_ops(resp: dict) -> Units:
    out: Units = defaultdict(lambda: defaultdict(float))
    for g in _groups(resp, "kvOperationsAdaptiveGroups"):
        d, action, n = g["dimensions"]["date"], g["dimensions"]["actionType"], g["sum"]["requests"]
        out[d]["kv_reads" if action == "read" else "kv_writes"] += n
    return out


def parse_kv_storage(resp: dict) -> Units:
    out: Units = defaultdict(lambda: defaultdict(float))
    for g in _groups(resp, "kvStorageAdaptiveGroups"):
        out[g["dimensions"]["date"]]["kv_storage_gb"] += g["max"]["byteCount"] / 1e9
    return out


def merge(*parts: Units) -> dict[str, dict[str, float]]:
    """The parts' units per day, days sorted, units in `PRICES` order (absent units 0)."""
    days = sorted({d for p in parts for d in p})
    return {d: {u: sum(p.get(d, {}).get(u, 0.0) for p in parts) for u in PRICES} for d in days}


def cost(unit: str, amount: float) -> float:
    """List-price USD of `amount` of `unit` (one day's level, for a storage unit)."""
    p = PRICES[unit]
    usd = amount / p.per * p.usd
    return usd / 30 if unit in STORAGE_UNITS else usd


def day_costs(units: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Each day's list-price $ per unit, before the plan's included amounts."""
    return {d: {u: cost(u, a) for u, a in us.items()} for d, us in units.items()}


def overage(units: dict[str, dict[str, float]], days: int) -> dict[str, dict[str, float]]:
    """The window's total per unit (storage: the mean level) against the plan's included amount pro rata
    (`included × days / 30`), and the $ beyond it."""
    out = {}
    for u, p in PRICES.items():
        vals = [us[u] for us in units.values()]
        total = (sum(vals) / len(vals) if vals else 0.0) if u in STORAGE_UNITS else sum(vals)
        included = p.included * days / 30
        over = max(0.0, total - included)
        usd = over / p.per * p.usd * (days / 30 if u in STORAGE_UNITS else 1)
        out[u] = {"total": total, "included": included, "over": over, "usd": usd}
    return out


# --- Analytics Engine ---------------------------------------------------------------------------------------

#: The usage point's fields (`site/functions/_lib/usage.ts` `usagePoint`).
AE_DIMS = {"caller": "blob4", "via": "blob3", "route": "blob1", "status": "blob5", "cache": "blob6", "store": "blob7"}
AE_SUMS = {"requests": "1", "wall_ms": "double2", "r2_reads": "double3", "r2_bytes": "double4", "d1_queries": "double5",
           "d1_rows_read": "double6", "kv_reads": "double7", "d1_rows_written": "double8"}


def ae_sql(dataset: str, since: dt.date, until: dt.date, by: tuple[str, ...], host: str | None = "prod") -> str:
    """The SQL summing the usage points per day and `by` dimensions, sample-weighted; `host` (`prod` / `dev`)
    keeps one host's points, `None` both."""
    dims = ", ".join(f"{AE_DIMS[b]} AS {b}" for b in by)
    sums = ", ".join(f"SUM(_sample_interval * {c}) AS {k}" if c != "1" else f"SUM(_sample_interval) AS {k}" for k, c in AE_SUMS.items())
    where = f"timestamp >= toDateTime('{since.isoformat()} 00:00:00') AND timestamp < toDateTime('{until.isoformat()} 00:00:00')"
    if host:
        where += f" AND blob8 = '{host}'"
    group = ", ".join(["day", *by])
    return f"SELECT toDate(timestamp) AS day, {dims}, {sums} FROM {dataset} WHERE {where} GROUP BY {group} ORDER BY {group} FORMAT JSON"


def parse_ae(resp: dict, by: tuple[str, ...]) -> list[dict[str, Any]]:
    """The SQL API's `FORMAT JSON` rows: `day` and the `by` dimensions as strings, the sums as floats (the API may
    return a 64-bit sum as a string; a point written before a field existed reads 0)."""
    out = []
    for r in resp.get("data") or []:
        row: dict[str, Any] = {"day": str(r["day"])[:10]}
        for b in by:
            row[b] = str(r[b])
        for k in AE_SUMS:
            row[k] = float(r.get(k) or 0)
        out.append(row)
    return out


def top_n(rows: list[dict[str, Any]], key: str, n: int) -> list[dict[str, Any]]:
    """`rows` summed over days per `key`, the `n` with most requests first, the rest folded into `(other)`."""
    acc: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(AE_SUMS, 0.0))
    for r in rows:
        for k in AE_SUMS:
            acc[r[key]][k] += r[k]
    ranked = sorted(acc.items(), key=lambda kv: (-kv[1]["requests"], kv[0]))
    head, tail = ranked[:n], ranked[n:]
    out = [{key: name, **sums} for name, sums in head]
    if tail:
        out.append({key: "(other)", **{k: sum(s[k] for _, s in tail) for k in AE_SUMS}})
    return out


# --- the report ---------------------------------------------------------------------------------------------

@dataclass
class Report:
    since: str
    until: str
    project: str
    units: dict[str, dict[str, float]]
    costs: dict[str, dict[str, float]]
    overage: dict[str, dict[str, float]]
    functions: FunctionsSplit = field(default_factory=dict)
    by_caller: list[dict[str, Any]] = field(default_factory=list)
    by_route: list[dict[str, Any]] = field(default_factory=list)
    #: The attribution rows per day (`parse_ae`'s), which `snapshot` cuts by day.
    daily_caller: list[dict[str, Any]] = field(default_factory=list)
    daily_route: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


PARSERS: dict[str, Callable[[dict], Units]] = {
    "r2_ops": parse_r2_ops,
    "r2_storage": parse_r2_storage,
    "d1": parse_d1,
    "kv_ops": parse_kv_ops,
    "kv_storage": parse_kv_storage,
}


def build_report(dep: Deployment, token: str, days: int, fetch: Fetch | None = None, today: dt.date | None = None, top: int = 10, host: str | None = "prod") -> Report:
    """Both sources over the `days` before `today`: a failed dataset is a note, not a failure. `fetch`: `http_fetch`."""
    fetch = fetch or http_fetch
    since, until = window(days, today)
    variables = gql_variables(dep, since, until)
    parts: list[Units] = []
    notes: list[str] = []
    split: FunctionsSplit = {}
    if not dep.script:
        notes.append(f"functions: no CF_PAGES_SCRIPT for {dep.project!r} (the Pages API's `production_script_name` "
                     "without `-production`); the account's total")
    for name, query in QUERIES.items():
        need = NEEDS.get(name)
        if need and not getattr(dep, need):
            continue
        if name == "functions" and not dep.script:
            query = FUNCTIONS_ACCOUNT
        try:
            resp = fetch(GRAPHQL_URL, token, json.dumps({"query": query, "variables": variables}).encode(), "application/json")
            if name == "functions":
                units, split = parse_functions(resp, dep.script)
                parts.append(units)
            else:
                parts.append(PARSERS[name](resp))
        except Exception as e:  # one dataset's failure (permissions, plan, schema) leaves the others
            notes.append(f"{name}: {e}")
    units = merge(*parts)
    report = Report(since.isoformat(), until.isoformat(), dep.project, units, day_costs(units), overage(units, days),
                    functions=split, notes=notes)
    if not dep.dataset:
        report.notes.append("attribution: no analytics_engine_datasets in the wrangler config")
        return report
    sql_url = f"https://api.cloudflare.com/client/v4/accounts/{dep.account}/analytics_engine/sql"
    for by, attr, key in ((("caller", "via"), "caller", "caller"), (("route",), "route", "route")):
        try:
            resp = fetch(sql_url, token, ae_sql(dep.dataset, since, until, by, host).encode(), "text/plain")
            rows = parse_ae(resp, by)
            setattr(report, f"daily_{attr}", rows)
            setattr(report, f"by_{attr}", top_n(rows, key, top))
        except Exception as e:
            report.notes.append(f"attribution by {key}: {e}")
    return report


def _n(x: float) -> str:
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{x / div:.3g}{suf}"
    return f"{x:.3g}"


def render(r: Report) -> str:
    """The text report: per-day billed units and $, the window against the plan, then attribution."""
    used = [u for u in PRICES if any(us[u] for us in r.units.values())]
    lines = [f"{r.project}: {r.since} … {r.until} (UTC, until exclusive)", ""]
    if used:
        lines.append("\t".join(["day", *(PRICES[u].label for u in used), "$ list"]))
        for d, us in r.units.items():
            lines.append("\t".join([d, *(_n(us[u]) for u in used), f"{sum(r.costs[d].values()):.2f}"]))
        lines += ["", "window vs plan (pro rata):"]
        for u in used:
            o = r.overage[u]
            lines.append(f"  {PRICES[u].label}: {_n(o['total'])} of {_n(o['included'])} included → ${o['usd']:.2f} over")
    else:
        lines.append("no billed usage")
    for title, rows, key in (("by caller", r.by_caller, "caller"), ("by route", r.by_route, "route")):
        if rows:
            lines += ["", f"{title}:", "\t".join([key, "requests", "R2 reads", "R2 bytes", "D1 rows read", "D1 rows written", "KV reads", "ms/req"])]
            for x in rows:
                ms = x["wall_ms"] / x["requests"] if x["requests"] else 0
                lines.append("\t".join([x[key], _n(x["requests"]), _n(x["r2_reads"]), _n(x["r2_bytes"]), _n(x["d1_rows_read"]), _n(x["d1_rows_written"]), _n(x["kv_reads"]), f"{ms:.0f}"]))
    if r.notes:
        lines += ["", "notes:", *(f"  {n}" for n in r.notes)]
    return "\n".join(lines)


# --- per-day snapshots ---------------------------------------------------------------------------------------

#: `snapshot`'s shape; a reader refuses another.
SNAPSHOT_VERSION = 1
#: Attribution rows per table in a snapshot (the rest folded into `(other)`).
SNAPSHOT_TOP = 100


def _num(x: float) -> float | int:
    """A JSON number: an integral value as an int, else rounded to 6 places (stable bytes across reruns)."""
    return int(x) if float(x).is_integer() else round(x, 6)


def day_rows(rows: list[dict[str, Any]], day: str, keys: tuple[str, ...], top: int) -> list[dict[str, Any]]:
    """`day`'s attribution rows, most requests first (then by `keys`), the first `top` as given, the rest summed into
    one row whose first key is `(other)` (and other keys empty)."""
    mine = sorted((r for r in rows if r["day"] == day), key=lambda r: (-r["requests"], *(r[k] for k in keys)))
    head, tail = mine[:top], mine[top:]
    out = [{**{k: r[k] for k in keys}, **{s: _num(r[s]) for s in AE_SUMS}} for r in head]
    if tail:
        out.append({**{k: ("(other)" if i == 0 else "") for i, k in enumerate(keys)},
                    **{s: _num(sum(r[s] for r in tail)) for s in AE_SUMS}})
    return out


def snapshot(r: Report, day: str, top: int = SNAPSHOT_TOP) -> dict[str, Any]:
    """One day's usage document, `snapshots/usage/<day>.json`:

    - `version`, `date`, `project`;
    - `units`: the billed units (`PRICES`' keys, all present; storage units are the day's GB level), `usd`: each
      one's list price before the plan's included amounts (a storage unit's day: a 30th of its month);
    - `plan`: per unit its `label`, list `usd` per `per` units, the plan's monthly `included` amount, and `storage`
      (a GB level billed per month, not a count);
    - `functions`: requests and estimated CPU ms per script env (`production` / `preview`, or `account`);
    - `by_caller` (`caller`, `via`) and `by_route` (`route`): the usage points' sums (`AE_SUMS`), sample-weighted,
      both hosts, top `top` by requests + `(other)`;
    - `notes`: what the report couldn't read (a dataset's failure, an unscoped total)."""
    zero = dict.fromkeys(PRICES, 0.0)
    return {
        "version": SNAPSHOT_VERSION,
        "date": day,
        "project": r.project,
        "units": {u: _num(v) for u, v in r.units.get(day, zero).items()},
        "usd": {u: _num(v) for u, v in r.costs.get(day, zero).items()},
        "plan": {u: {"label": p.label, "usd": p.usd, "per": _num(p.per), "included": _num(p.included), "storage": u in STORAGE_UNITS}
                 for u, p in PRICES.items()},
        "functions": {e: {k: _num(v) for k, v in x.items()} for e, x in r.functions.get(day, {}).items()},
        "by_caller": day_rows(r.daily_caller, day, ("caller", "via"), top),
        "by_route": day_rows(r.daily_route, day, ("route",), top),
        "notes": list(r.notes),
    }


def days_between(since: dt.date, until: dt.date) -> list[str]:
    return [(since + dt.timedelta(days=i)).isoformat() for i in range((until - since).days)]


def missing(roots: list[str], days: list[str]) -> dict[str, list[str]]:
    """Per root (a dir or a URL), the `days` with no `<day>.json` under it."""
    from .blobstore import Store

    out = {}
    for root in roots:
        have = {k.rsplit("/", 1)[-1] for k in Store(root).listing("")}
        out[root] = [d for d in days if f"{d}.json" not in have]
    return out


def write_snapshots(r: Report, todo: dict[str, list[str]], top: int = SNAPSHOT_TOP) -> list[str]:
    """Each root's `todo` days' snapshots, written; their locations."""
    from .blobstore import Store

    written = []
    for root, days in todo.items():
        store = Store(root)
        for d in days:
            store.write_text(f"{d}.json", json.dumps(snapshot(r, d, top), indent=1) + "\n")
            written.append(store.uri(f"{d}.json"))
    return written


@command("cf-usage")
@option("-a", "--account", default=None, help="Cloudflare account id (default `$CLOUDFLARE_ACCOUNT_ID`, else the config's `CF_ACCOUNT_ID` var)")
@option("-c", "--config", "config", type=ClickPath(dir_okay=False, exists=True, path_type=Path), default=Path("site/wrangler.toml"), help="The deployment's Pages wrangler config (its project, buckets, D1s, KV namespaces, usage dataset, `CF_PAGES_SCRIPT`)")
@option("-d", "--days", type=int, default=7, help="Whole UTC days before today (GraphQL datasets cap a query at 31)")
@option("-f", "--force", is_flag=True, help="With -o: rewrite every day of the window, not only the missing ones")
@option("-H", "--host", type=Choice(["prod", "dev", "all"]), default="prod", help="Attribution: the prod host's points, the dev stack's, or both (snapshots: both)")
@option("-j", "--json", "as_json", is_flag=True, help="The report as JSON")
@option("-n", "--top", type=int, default=10, help="Attribution rows per table (the rest folded into `(other)`; snapshots keep 100)")
@option("-o", "--out", "outs", multiple=True, help="Write a snapshot per day, `<OUT>/<date>.json` (a dir or URL; repeatable), for the window's days any OUT lacks")
@option("-s", "--script", default=None, help="The Pages Functions script stem, `pages-worker--<n>` (default: the config's `CF_PAGES_SCRIPT` var)")
def cli(account: str | None, config: Path, days: int, force: bool, host: str, as_json: bool, top: int, outs: tuple[str, ...], script: str | None) -> None:
    """Billed Cloudflare usage (GraphQL Analytics) and our per-caller / per-route attribution (Analytics Engine)
    for the deployment CONFIG names, per day, with list-price $. Token: `$CF_ANALYTICS_TOKEN` (Account Analytics: Read).
    With `-o`: write the per-day snapshots (`snapshot`) the window's missing days need, and print their locations."""
    if not 1 <= days <= 31:
        raise UsageError("-d must be 1..31 (a GraphQL Analytics query spans at most 31 days)")
    token = env_secret(TOKEN_ENV)
    if not token:
        raise UsageError(f"set ${TOKEN_ENV}: a Cloudflare API token with Account Analytics: Read on the account")
    dep = load_deployment(config, account)
    if script:
        dep = replace(dep, script=script)
    if not outs:
        r = build_report(dep, token, days, top=top, host=None if host == "all" else host)
        print(json.dumps(r.__dict__, indent=2) if as_json else render(r))
        return
    since, until = window(days)
    todo = {root: ds for root, ds in (missing(list(outs), days_between(since, until)).items() if not force
                                       else ((o, days_between(since, until)) for o in outs)) if ds}
    if not todo:
        err(f"cf-usage: every day {since} … {until} (exclusive) is in {', '.join(outs)}")
        return
    first = min(d for ds in todo.values() for d in ds)
    r = build_report(dep, token, (until - dt.date.fromisoformat(first)).days, host=None)
    for n in r.notes:
        err(f"cf-usage: {n}")
    for w in write_snapshots(r, todo):
        print(w)
