"""`dt-cloud cost`: what a deployment's GCP storage and Batch compute cost now, and projected over horizons.

Read-only, from measurements:
1. Storage: Cloud Monitoring's `storage.googleapis.com/storage/v2/total_bytes` per bucket, daily means over the
   window, split by its `type` (live vs soft-deleted objects) and `storage_class` labels. Live bytes grow at the
   median of their daily deltas (step-robust: a one-off cleanup or fill doesn't set the rate), except in a bucket a lifecycle `Delete` rule bounds (`age: N` over the whole bucket: its
   steady state is ~N days of writes), where they are held at their current level. A prefix- or otherwise-scoped
   `Delete` rule can't be separated from the bucket-level metric: the bucket is marked partially TTL'd and keeps its
   slope. Soft-deleted bytes track churn, not growth, so they are held at their current level (or at zero for a
   bucket whose soft-delete retention is off: they drain). A bucket with fewer than `MIN_POINTS` daily points is a
   short window, and the summary calls the projection provisional.
2. Compute: the Batch jobs (every location, or `-L`) created in the window whose labels match a selector. A job's
   VM-hours are its `runDuration` × min(`taskCount`, `parallelism`) (its first task group), priced by its machine
   type and provisioning model (SPOT × `spot_factor`). The window's $ / its days is the rate, projected flat.

Prices are approximate US list prices (`PRICES`), overridable by a YAML file (`load_prices`): estimates, not
invoices; no discounts, disks, network or operations. A machine type or storage class with no price is reported
as unpriced, never dropped.

The fetchers (`fetch_series`, `fetch_jobs`, `select_buckets`, `fetch_buckets`) are the only network calls; the
rest takes the APIs' JSON as given, so tests inject fake series and jobs.

`-o ROOT` (repeatable) writes the day's snapshot instead, `<ROOT>/<date>.json` (`snapshot`, the shape
`SNAPSHOT_VERSION` names): the job's `gcp-cost` step (`scan_steps`) writes one a day under `snapshots/cost/gcp/`,
and the site's `/admin/cost/gcp` reads them (`site/functions/api/cost/[cloud].ts`).
"""
from __future__ import annotations

import json
import re
import sys
from statistics import median
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from click import UsageError, command, option

from .scan_cost import labels_of, timestamp

METRIC = "storage.googleapis.com/storage/v2/total_bytes"
LIVE = "live-object"
DAY = 86400
GB = 1e9
GIB = 2**30
YEAR_DAYS = 365.25
MONTH_DAYS = YEAR_DAYS / 12
#: Fewer daily points than this: a short window, its projection provisional.
MIN_POINTS = 14

#: $/vCPU-hour and $/GB-hour (on-demand, US regions) per machine family; a predefined type's price is their sum
#: over its vCPUs and memory, which is how GCP prices them.
FAMILY_RATES = {
    "e2": (0.021811, 0.002923),
    "n2": (0.031611, 0.004237),
    "n2d": (0.027502, 0.003686),
}
#: GB of memory per vCPU by shape.
SHAPES = {"standard": 4, "highmem": 8}
SIZES = {"e2": (2, 4, 8, 16, 32), "n2": (2, 4, 8, 16, 32, 48, 64, 80, 96), "n2d": (2, 4, 8, 16, 32, 48, 64, 80, 96)}


def _machine_prices() -> dict[str, float]:
    out = {}
    for family, (cpu, mem) in FAMILY_RATES.items():
        for shape, gb_per_cpu in SHAPES.items():
            for n in SIZES[family]:
                out[f"{family}-{shape}-{n}"] = round(n * cpu + n * gb_per_cpu * mem, 6)
    # Shared-core E2s are priced per type, not per resource.
    out |= {"e2-micro": 0.008376, "e2-small": 0.016751, "e2-medium": 0.033503}
    return out


@dataclass(frozen=True)
class Prices:
    """$/GiB-month per Monitoring `storage_class` label value (a regional bucket's Standard storage is `REGIONAL`,
    a multi-region's `MULTI_REGIONAL`), on-demand $/hour per machine type, and the SPOT multiplier."""

    storage: Mapping[str, float]
    machines: Mapping[str, float]
    spot_factor: float


#: Approximate US list prices (USD). Regional buckets in other continents and Spot's per-region discounts differ:
#: override with `-P` (`load_prices`).
PRICES = Prices(
    storage={
        "REGIONAL": 0.020,
        "STANDARD": 0.020,
        "DURABLE_REDUCED_AVAILABILITY": 0.020,
        "MULTI_REGIONAL": 0.026,
        "NEARLINE": 0.010,
        "COLDLINE": 0.004,
        "ARCHIVE": 0.0012,
    },
    machines=_machine_prices(),
    spot_factor=0.3,
)


def load_prices(path: Path | None, base: Prices = PRICES) -> Prices:
    """`base`, with `path`'s (YAML) `storage` / `machines` entries added or replacing, and its `spot_factor`."""
    if path is None:
        return base
    doc = yaml.safe_load(path.read_text()) or {}
    unknown = set(doc) - {"storage", "machines", "spot_factor"}
    if unknown:
        raise UsageError(f"{path}: unknown keys {sorted(unknown)} (expected storage, machines, spot_factor)")
    return Prices(
        storage={**base.storage, **{k: float(v) for k, v in (doc.get("storage") or {}).items()}},
        machines={**base.machines, **{k: float(v) for k, v in (doc.get("machines") or {}).items()}},
        spot_factor=float(doc.get("spot_factor", base.spot_factor)),
    )


# --- parsing ------------------------------------------------------------------------------------------------

def parse_selector(text: str | None) -> dict[str, str]:
    """`"k=v,k2=v2"` → `{"k": "v", "k2": "v2"}`; None / empty → `{}`. A value may list alternatives, `v1|v2`
    (`matches`): e.g. a deployment renamed within the window."""
    out: dict[str, str] = {}
    for item in (text or "").split(","):
        if not item.strip():
            continue
        k, sep, v = item.strip().partition("=")
        if not sep or not k:
            raise UsageError(f"label selector items are KEY=VALUE: {item!r}")
        out[k] = v
    return out


def matches(labels: Mapping[str, str], selector: Mapping[str, str]) -> bool:
    return all(labels.get(k) in v.split("|") for k, v in selector.items())


SPAN = re.compile(r"(\d+(?:\.\d+)?)([dwmy])")
SPAN_DAYS = {"d": 1.0, "w": 7.0, "m": MONTH_DAYS, "y": YEAR_DAYS}


def parse_span(text: str) -> float:
    """`30d`, `2w`, `6m`, `1y` → days."""
    m = SPAN.fullmatch(text.strip())
    if not m:
        raise UsageError(f"spans are a number and d|w|m|y: {text!r}")
    return float(m[1]) * SPAN_DAYS[m[2]]


@dataclass(frozen=True)
class Series:
    """One bucket's bytes of one `type` and `storage_class`: (epoch seconds, bytes), oldest first."""

    bucket: str
    type: str
    storage_class: str
    points: tuple[tuple[float, float], ...]


def parse_series(pages: Iterable[Mapping]) -> list[Series]:
    """Monitoring `timeSeries.list` pages → `Series`, sorted by (bucket, type, class)."""
    out = []
    for page in pages:
        for ts in page.get("timeSeries", []):
            pts = sorted(
                (timestamp(p["interval"]["endTime"]), float(p["value"].get("doubleValue", p["value"].get("int64Value", 0))))
                for p in ts.get("points", [])
            )
            m = ts["metric"]["labels"]
            out.append(Series(ts["resource"]["labels"]["bucket_name"], m["type"], m["storage_class"], tuple(pts)))
    return sorted(out, key=lambda s: (s.bucket, s.type, s.storage_class))


def slope_per_day(points: Sequence[tuple[float, float]]) -> float:
    """Bytes/day: the median of the consecutive points' deltas (each per day of its gap), so a one-off step (a
    cleanup, an initial fill) doesn't set the rate; 0 with fewer than two points."""
    if len(points) < 2:
        return 0.0
    return median((v1 - v0) / ((t1 - t0) / DAY) for (t0, v0), (t1, v1) in zip(points, points[1:]))


#: Lifecycle conditions that confine a rule to noncurrent versions: such a rule never bounds live bytes.
NONCURRENT = {"numNewerVersions", "daysSinceNoncurrentTime", "noncurrentTimeBefore"}


@dataclass(frozen=True)
class BucketPolicy:
    """What bounds a bucket's bytes: soft delete off (soft-deleted bytes drain), a whole-bucket lifecycle `Delete`
    at `ttl_days` (live bytes plateau), or only scoped `Delete` rules (`partial_ttl`: not separable)."""

    soft_delete_off: bool = False
    ttl_days: float | None = None
    partial_ttl: bool = False


def bucket_policy(meta: Mapping) -> BucketPolicy:
    """A Storage JSON API bucket resource (`softDeletePolicy`, `lifecycle`) → `BucketPolicy`."""
    whole: list[float] = []
    partial = False
    for rule in meta.get("lifecycle", {}).get("rule", []):
        cond = rule.get("condition", {})
        if rule.get("action", {}).get("type") != "Delete" or "age" not in cond:
            continue
        if cond.get("isLive") is False or NONCURRENT & set(cond):
            continue
        if set(cond) - {"age", "isLive"}:
            partial = True
        else:
            whole.append(float(cond["age"]))
    retention = int(meta.get("softDeletePolicy", {}).get("retentionDurationSeconds", 0))
    return BucketPolicy(
        soft_delete_off=retention == 0,
        ttl_days=min(whole) if whole else None,
        partial_ttl=partial and not whole,
    )


@dataclass(frozen=True)
class JobUsage:
    name: str
    location: str
    component: str
    machine: str | None
    spot: bool
    vm_hours: float


def machine_key(u: JobUsage) -> str:
    return f"{u.machine or '(unknown)'}{' spot' if u.spot else ''}"


def machine_of(job: Mapping) -> tuple[str | None, bool]:
    """The job's machine type and whether it is SPOT: `allocationPolicy.instances[0].policy`, else (a template-based
    job) the instances its status reports; no `provisioningModel` = STANDARD."""
    ins = job.get("allocationPolicy", {}).get("instances", [])
    policy = ins[0].get("policy", {}) if ins else {}
    machine = policy.get("machineType")
    model = policy.get("provisioningModel")
    if machine is None:
        for group in job.get("status", {}).get("taskGroups", {}).values():
            for i in group.get("instances", []):
                machine, model = i.get("machineType"), model or i.get("provisioningModel")
                break
    return machine, model == "SPOT"


def job_usage(job: Mapping) -> JobUsage:
    group = job.get("taskGroups", [{}])[0]
    tasks = int(group.get("taskCount", 1))
    vms = min(tasks, int(group.get("parallelism", tasks)))
    seconds = float(job.get("status", {}).get("runDuration", "0s").rstrip("s"))
    machine, spot = machine_of(job)
    return JobUsage(
        name=job["name"].rsplit("/", 1)[-1],
        location=job["name"].split("/")[3],
        component=labels_of(job).get("component", "(none)"),
        machine=machine,
        spot=spot,
        vm_hours=seconds * vms / 3600,
    )


# --- the report ---------------------------------------------------------------------------------------------

def projected(live: bool, current: float, slope: float, pol: BucketPolicy, days: float) -> float:
    """One series' bytes `days` out: live bytes extrapolated at `slope` (floored at 0), or held in a TTL'd bucket;
    soft-deleted held, or 0 where soft delete is off (they drain)."""
    if live:
        return current if pol.ttl_days is not None else max(0.0, current + slope * days)
    return 0.0 if pol.soft_delete_off else current


def storage_report(
    series: Sequence[Series],
    prices: Prices,
    horizons: Mapping[str, float],
    policies: Mapping[str, BucketPolicy] | None = None,
) -> dict:
    """Per (bucket, type, class): current bytes (the newest daily mean), live bytes' slope, $/mo; per bucket its
    daily points and policy; totals now and at each horizon (live bytes extrapolated, floored at 0, or held in a
    TTL'd bucket; soft-deleted held, or 0 where soft delete is off)."""
    policies = policies or {}
    none = BucketPolicy()
    rows: list[dict[str, Any]] = []
    buckets: dict[str, dict[str, Any]] = {}
    unpriced: dict[str, float] = defaultdict(float)
    at: dict[str, dict[str, float]] = {h: {"bytes": 0.0, "usd_per_month": 0.0} for h in horizons}
    for s in series:
        if not s.points:
            continue
        current = s.points[-1][1]
        live = s.type == LIVE
        pol = policies.get(s.bucket, none)
        b_ = buckets.setdefault(s.bucket, {"points": 0, "soft_delete_off": pol.soft_delete_off,
                                           "ttl_days": pol.ttl_days, "partial_ttl": pol.partial_ttl})
        b_["points"] = max(b_["points"], len(s.points))
        b_["short"] = b_["points"] < MIN_POINTS
        slope = slope_per_day(s.points) if live else 0.0
        price = prices.storage.get(s.storage_class)
        if price is None:
            unpriced[s.storage_class] += current
        rows.append({
            "bucket": s.bucket,
            "type": s.type,
            "storage_class": s.storage_class,
            "days": len(s.points),
            "bytes": current,
            "slope_bytes_per_day": slope,
            "usd_per_month": None if price is None else current / GIB * price,
        })
        for h, days in horizons.items():
            b = projected(live, current, slope, pol, days)
            at[h]["bytes"] += b
            if price is not None:
                at[h]["usd_per_month"] += b / GIB * price
    by_type = {t: sum(r["bytes"] for r in rows if r["type"] == t) for t in sorted({r["type"] for r in rows})}
    return {
        "rows": rows,
        "bytes": sum(r["bytes"] for r in rows),
        "bytes_by_type": by_type,
        "live_slope_bytes_per_day": sum(r["slope_bytes_per_day"] for r in rows),
        "usd_per_month": sum(r["usd_per_month"] or 0.0 for r in rows),
        "buckets": dict(sorted(buckets.items())),
        "unpriced_bytes": dict(sorted(unpriced.items())),
        "horizons": at,
    }


def job_usd(u: JobUsage, prices: Prices) -> float | None:
    """`u`'s VM-hours at its machine's price (× `spot_factor` on SPOT); None when the machine has no price."""
    rate = prices.machines.get(u.machine) if u.machine else None
    return None if rate is None else u.vm_hours * rate * (prices.spot_factor if u.spot else 1.0)


def _tally(rows: Iterable[tuple[str, JobUsage, float | None]], window_days: float) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"jobs": 0, "vm_hours": 0.0, "usd": 0.0})
    for k, u, usd in rows:
        out[k]["jobs"] += 1
        out[k]["vm_hours"] += u.vm_hours
        out[k]["usd"] += usd or 0.0
    return {k: {**v, "usd_per_month": v["usd"] / window_days * MONTH_DAYS} for k, v in sorted(out.items())}


def compute_report(
    jobs: Sequence[Mapping],
    selector: Mapping[str, str],
    prices: Prices,
    window_days: float,
) -> dict:
    """The selector's jobs' VM-hours and $ per component and machine, as $/day and $/mo over the window; unpriced
    machines' VM-hours; and the window's jobs that carry none of the selector's keys, by name prefix (work the
    selector can't see, which may or may not be the deployment's)."""
    priced = [(u, job_usd(u, prices)) for u in (job_usage(j) for j in jobs if matches(labels_of(j), selector))]
    unpriced: dict[str, float] = defaultdict(float)
    for u, usd in priced:
        if usd is None:
            unpriced[machine_key(u)] += u.vm_hours
    unlabeled = [job_usage(j) for j in jobs if selector and not set(selector) & set(labels_of(j))]
    usd = sum(x or 0.0 for _, x in priced)
    per_day = usd / window_days
    return {
        "selector": dict(selector),
        "jobs": len(priced),
        "vm_hours": sum(u.vm_hours for u, _ in priced),
        "usd": usd,
        "usd_per_day": per_day,
        "usd_per_month": per_day * MONTH_DAYS,
        "components": _tally(((u.component, u, x) for u, x in priced), window_days),
        "machines": _tally(((machine_key(u), u, x) for u, x in priced), window_days),
        "unpriced_vm_hours": dict(sorted(unpriced.items())),
        "unlabeled": _tally(((u.name.split("-")[0], u, job_usd(u, prices)) for u in unlabeled), window_days),
    }


def report(
    *,
    name: str,
    project: str,
    now: float,
    window_days: float,
    horizons: Mapping[str, float],
    prices: Prices,
    series: Sequence[Series] | None,
    policies: Mapping[str, BucketPolicy] | None = None,
    jobs: Sequence[Mapping] | None = None,
    selector: Mapping[str, str] | None = None,
) -> dict:
    doc: dict = {
        "name": name,
        "project": project,
        "as_of": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days,
        "horizons": dict(horizons),
        "basis": "approximate US list prices; estimates, not billed cost",
        "storage": None if series is None else storage_report(series, prices, horizons, policies),
        "compute": None if jobs is None else compute_report(jobs, selector or {}, prices, window_days),
    }
    doc["summary"] = summary(doc)
    return doc


def _gb(b: float) -> str:
    return f"{b / GB:,.1f}"


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def summary(doc: Mapping) -> str:
    """"We can leave NAME running for 1y/2y for X→Y GB of storage ($A→$B/mo) and $C→$D/mo on compute.\""""
    hs = list(doc["horizons"])
    parts = []
    provisional = ""
    if (s := doc["storage"]) is not None:
        if short := [k for k, b in s["buckets"].items() if b["short"]]:
            provisional = f"; provisional: short window (< {MIN_POINTS} daily points) for {', '.join(short)}"
        ys = "/".join(_gb(s["horizons"][h]["bytes"]) for h in hs)
        bs = "/".join(_usd(s["horizons"][h]["usd_per_month"]) for h in hs)
        parts.append(f"{_gb(s['bytes'])}→{ys} GB of storage ({_usd(s['usd_per_month'])}→{bs}/mo)")
    if (c := doc["compute"]) is not None:
        parts.append(f"{_usd(c['usd_per_month'])}→{_usd(c['usd_per_month'])}/mo on compute")
    return f"We can leave {doc['name']} running for {'/'.join(hs)} for {' and '.join(parts)} (list-price estimates{provisional})."


def render(doc: Mapping) -> str:
    hs = list(doc["horizons"])
    lines = [
        f"{doc['name']} · {doc['project']} · {doc['window_days']:g}-day window to {doc['as_of']} · {doc['basis']}",
    ]
    if (s := doc["storage"]) is not None:
        lines += ["", "Storage (Cloud Monitoring `storage/v2/total_bytes`, daily means):",
                  f"  {'bucket':<32} {'type':<20} {'class':<15} {'pts':>4} {'GB':>12} {'GB/day':>9} {'$/mo':>10}"]
        for r in s["rows"]:
            usd = "unpriced" if r["usd_per_month"] is None else _usd(r["usd_per_month"])
            slope = f"{r['slope_bytes_per_day'] / GB:+,.2f}" if r["type"] == LIVE else "-"
            lines.append(f"  {r['bucket']:<32} {r['type']:<20} {r['storage_class']:<15} {r['days']:>4} {_gb(r['bytes']):>12} {slope:>9} {usd:>10}")
        by_type = ", ".join(f"{t} {_gb(b)} GB" for t, b in s["bytes_by_type"].items())
        lines.append(f"  now: {_gb(s['bytes'])} GB ({by_type}), {_usd(s['usd_per_month'])}/mo; live {s['live_slope_bytes_per_day'] / GB:+,.2f} GB/day")
        for h in hs:
            at = s["horizons"][h]
            lines.append(f"  {h}: {_gb(at['bytes'])} GB, {_usd(at['usd_per_month'])}/mo")
        for name, b in s["buckets"].items():
            notes = [f"short window ({b['points']} pts)" if b["short"] else f"{b['points']} daily points"]
            if b["ttl_days"] is not None:
                notes.append(f"lifecycle Delete at {b['ttl_days']:g}d: live bytes held flat")
            elif b["partial_ttl"]:
                notes.append("partially TTL'd (scoped Delete rules): live slope kept")
            notes.append("soft delete off: soft-deleted bytes drain" if b["soft_delete_off"] else "soft-deleted bytes held")
            lines.append(f"  {name}: {'; '.join(notes)}")
        for cls, b in s["unpriced_bytes"].items():
            lines.append(f"  UNPRICED: {_gb(b)} GB of storage class {cls}")
    if (c := doc["compute"]) is not None:
        sel = ",".join(f"{k}={v}" for k, v in c["selector"].items()) or "(any)"
        lines += ["", f"Compute (Batch jobs labeled {sel}, created in the window; VM-h = runDuration × min(taskCount, parallelism)):",
                  f"  {'component':<24} {'jobs':>5} {'VM-h':>9} {'$/mo':>10}"]
        for k, v in c["components"].items():
            lines.append(f"  {k:<24} {v['jobs']:>5} {v['vm_hours']:>9,.1f} {_usd(v['usd_per_month']):>10}")
        lines.append(f"  {'machine':<24} {'jobs':>5} {'VM-h':>9} {'$/mo':>10}")
        for k, v in c["machines"].items():
            lines.append(f"  {k:<24} {v['jobs']:>5} {v['vm_hours']:>9,.1f} {_usd(v['usd_per_month']):>10}")
        lines.append(f"  total: {c['jobs']} jobs, {c['vm_hours']:,.1f} VM-h, {_usd(c['usd'])} = {_usd(c['usd_per_day'])}/day = {_usd(c['usd_per_month'])}/mo (flat)")
        for k, h in c["unpriced_vm_hours"].items():
            lines.append(f"  UNPRICED: {h:,.1f} VM-h on {k}")
        if un := c["unlabeled"]:
            n = sum(v["jobs"] for v in un.values())
            h = sum(v["vm_hours"] for v in un.values())
            m = sum(v["usd_per_month"] for v in un.values())
            lines.append(f"  NOT COUNTED: {n} jobs in the window ({h:,.1f} VM-h, {_usd(m)}/mo) carry none of the selector's keys; by name prefix:")
            for k, v in sorted(un.items(), key=lambda kv: (-kv[1]["jobs"], kv[0])):
                lines.append(f"    {k:<22} {v['jobs']:>5} {v['vm_hours']:>9,.1f} {_usd(v['usd_per_month']):>10}")
    lines += ["", doc["summary"]]
    return "\n".join(lines) + "\n"


# --- daily snapshots ------------------------------------------------------------------------------------------

#: `snapshot`'s shape; a reader (`site/functions/api/cost/[cloud].ts`) refuses another.
SNAPSHOT_VERSION = 1
#: The snapshot's `cloud`, its key space beside other clouds' cost pages: `snapshots/cost/<cloud>/<date>.json`.
CLOUD = "gcp"


def _num(x: Any) -> Any:
    """`x` with stable JSON numbers (an integral value as an int, else rounded to 6 places), containers recursively."""
    if isinstance(x, bool) or not isinstance(x, (int, float, Mapping, list, tuple)):
        return x
    if isinstance(x, (int, float)):
        return int(x) if float(x).is_integer() else round(x, 6)
    if isinstance(x, Mapping):
        return {k: _num(v) for k, v in x.items()}
    return [_num(v) for v in x]


def bucket_rows(storage: Mapping, prices: Prices, horizons: Mapping[str, float]) -> list[dict]:
    """Per bucket (name order): its bytes in all, by `type` (live / soft-deleted) and by `storage_class`, its live
    slope, $/mo now (priced classes) and unpriced bytes, its bytes and $/mo at each horizon (`projected`, per series,
    as the totals are), and its policy and window (`storage_report`'s `buckets` entry)."""
    out: dict[str, dict[str, Any]] = {}
    for r in storage["rows"]:
        meta = storage["buckets"][r["bucket"]]
        pol = BucketPolicy(meta["soft_delete_off"], meta["ttl_days"], meta["partial_ttl"])
        x = out.setdefault(r["bucket"], {
            "bucket": r["bucket"], "bytes": 0.0, "bytes_by_type": {}, "bytes_by_class": {},
            "live_slope_bytes_per_day": 0.0, "usd_per_month": 0.0, "unpriced_bytes": 0.0,
            "horizons": {h: {"bytes": 0.0, "usd_per_month": 0.0} for h in horizons}, **meta,
        })
        x["bytes"] += r["bytes"]
        x["bytes_by_type"][r["type"]] = x["bytes_by_type"].get(r["type"], 0.0) + r["bytes"]
        x["bytes_by_class"][r["storage_class"]] = x["bytes_by_class"].get(r["storage_class"], 0.0) + r["bytes"]
        x["live_slope_bytes_per_day"] += r["slope_bytes_per_day"]
        price = prices.storage.get(r["storage_class"])
        if price is None:
            x["unpriced_bytes"] += r["bytes"]
        else:
            x["usd_per_month"] += r["bytes"] / GIB * price
        for h, days in horizons.items():
            b = projected(r["type"] == LIVE, r["bytes"], r["slope_bytes_per_day"], pol, days)
            x["horizons"][h]["bytes"] += b
            if price is not None:
                x["horizons"][h]["usd_per_month"] += b / GIB * price
    return list(out.values())


def snapshot(doc: Mapping, prices: Prices) -> dict:
    """One day's cost document, `snapshots/cost/gcp/<date>.json` (`date`: the report's `as_of`, UTC):

    - `version`, `cloud`, `date`, and the report's `name`, `project`, `as_of`, `window_days`, `horizons` (days per
      horizon), `basis`, `summary`;
    - `usd_per_month`: `storage`, `compute` and their `total`, now (list-price estimates);
    - `storage` (None: not measured): the report's totals (`bytes`, `bytes_by_type`, `live_slope_bytes_per_day`,
      `usd_per_month`, `unpriced_bytes`, `horizons`) and `rows` (bucket × type × class), plus `buckets`: a list of
      `bucket_rows`, each with its own projection;
    - `compute` (None: not measured): `compute_report` as is (per `component` and machine, `unlabeled`).

    Numbers are `_num`'d, so a rerun over the same measurements writes the same bytes."""
    s, c = doc["storage"], doc["compute"]
    storage_usd = s["usd_per_month"] if s is not None else 0.0
    compute_usd = c["usd_per_month"] if c is not None else 0.0
    return _num({
        "version": SNAPSHOT_VERSION,
        "cloud": CLOUD,
        "date": doc["as_of"][:10],
        **{k: doc[k] for k in ("name", "project", "as_of", "window_days", "horizons", "basis", "summary")},
        "usd_per_month": {"storage": storage_usd, "compute": compute_usd, "total": storage_usd + compute_usd},
        "storage": None if s is None else {
            **{k: s[k] for k in ("bytes", "bytes_by_type", "live_slope_bytes_per_day", "usd_per_month", "unpriced_bytes", "horizons", "rows")},
            "buckets": bucket_rows(s, prices, doc["horizons"]),
        },
        "compute": c,
    })


def write_snapshot(snap: Mapping, roots: Sequence[str]) -> list[str]:
    """`snap` as `<root>/<date>.json` under each root (a dir or URL), replacing that day's earlier one (a later run's
    measurement of the same day wins); their locations."""
    from .blobstore import Store

    written = []
    for root in roots:
        store = Store(root)
        key = f"{snap['date']}.json"
        store.write_text(key, json.dumps(snap, indent=1) + "\n")
        written.append(store.uri(key))
    return written


# --- fetching (read-only) -----------------------------------------------------------------------------------

def clock() -> float:
    return datetime.now(timezone.utc).timestamp()


def _rfc3339(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_buckets(project: str, selector: Mapping[str, str]) -> list[str]:
    """The project's buckets whose labels match `selector`."""
    from google.cloud.storage import Client

    return sorted(b.name for b in Client(project=project).list_buckets() if matches(b.labels or {}, selector))


def fetch_buckets(buckets: Sequence[str]) -> dict[str, dict]:
    """Each bucket's JSON API resource (its `softDeletePolicy` and `lifecycle`)."""
    from .gcp import _get

    return {name: _get(f"https://storage.googleapis.com/storage/v1/b/{name}", fields="softDeletePolicy,lifecycle")
            for name in buckets}


def fetch_series(project: str, buckets: Sequence[str], start: float, end: float) -> list[dict]:
    """`METRIC` for `buckets`, ALIGN_MEAN per day over [start, end]: the raw `timeSeries.list` pages."""
    from .gcp import pages

    names = ", ".join(json.dumps(b) for b in buckets)
    return pages(
        f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
        filter=f'metric.type="{METRIC}" resource.labels.bucket_name=one_of({names})',
        **{
            "interval.startTime": _rfc3339(start),
            "interval.endTime": _rfc3339(end),
            "aggregation.alignmentPeriod": f"{DAY}s",
            "aggregation.perSeriesAligner": "ALIGN_MEAN",
        },
    )


def fetch_jobs(project: str, locations: Sequence[str], since: float) -> list[dict]:
    """The Batch jobs created since `since`, in `locations` (empty: every location, `-`)."""
    from .gcp import pages

    out = []
    for loc in locations or ["-"]:
        for page in pages(
            f"https://batch.googleapis.com/v1/projects/{project}/locations/{loc}/jobs",
            filter=f'create_time>"{_rfc3339(since)}"',
            pageSize="1000",
        ):
            out.extend(page.get("jobs", []))
    return sorted(out, key=lambda j: j["name"])


@command("cost")
@option("-b", "--bucket", "buckets", multiple=True, help="A bucket to measure (repeatable)")
@option("-H", "--horizons", default="1y,2y", help="Projection horizons, comma-separated (`6m`, `1y`, `90d`, …)")
@option("-j", "--json", "as_json", is_flag=True, help="The report as JSON")
@option("-J", "--job-labels", default=None, help="Batch job label selector, `k=v,k=v`, a value may be `v1|v2` (e.g. `app=disky,deployment=NAME`); unset: no compute section")
@option("-l", "--bucket-labels", default=None, help="Bucket label selector, `k=v,k=v` (a value may be `v1|v2`): the project's buckets whose labels match (added to `-b`)")
@option("-L", "--location", "locations", multiple=True, help="Batch location (repeatable; default every location)")
@option("-n", "--name", default=None, help="What the summary calls the deployment (default: the selectors)")
@option("-o", "--out", "outs", multiple=True, help="Write the day's snapshot (`snapshot`), `<OUT>/<date>.json` (a dir or URL; repeatable), and print its locations instead of the report")
@option("-p", "--project", default=None, help="GCP project (default `$GCP_PROJECT`, else ADC's)")
@option("-P", "--prices", "prices_path", type=Path, default=None, help="YAML overriding the built-in list prices: `storage` ($/GiB-month by class), `machines` ($/h by type), `spot_factor`")
@option("-w", "--window", default="30d", help="Measurement window (`30d`, `2w`, …)")
def cost_cmd(
    buckets: tuple[str, ...],
    horizons: str,
    as_json: bool,
    job_labels: str | None,
    bucket_labels: str | None,
    locations: tuple[str, ...],
    name: str | None,
    outs: tuple[str, ...],
    project: str | None,
    prices_path: Path | None,
    window: str,
) -> None:
    """A deployment's storage and compute cost now and projected over `-H` horizons, from the window's GCS
    Monitoring bytes and Batch jobs: list-price estimates. Read-only, except `-o`'s daily snapshot (the job's
    `gcp-cost` step; the site's `/admin/cost/gcp`)."""
    from .gcp import gcp_project

    if not (buckets or bucket_labels or job_labels):
        raise UsageError("nothing to measure: pass -b / -l (storage) and/or -J (compute)")
    project = project or gcp_project()
    prices = load_prices(prices_path)
    window_days = parse_span(window)
    spans = {h.strip(): parse_span(h) for h in horizons.split(",") if h.strip()}
    now = clock()
    start = now - window_days * DAY
    series = None
    policies: dict[str, BucketPolicy] = {}
    if buckets or bucket_labels:
        names = sorted({*buckets, *(select_buckets(project, parse_selector(bucket_labels)) if bucket_labels else [])})
        if not names:
            raise UsageError(f"no bucket in {project} matches {bucket_labels}")
        series = parse_series(fetch_series(project, names, start, now))
        policies = {k: bucket_policy(v) for k, v in fetch_buckets(names).items()}
    selector = parse_selector(job_labels)
    jobs = fetch_jobs(project, locations, start) if job_labels else None
    label = name or " / ".join(x for x in (job_labels, bucket_labels) if x) or ", ".join(buckets)
    doc = report(
        name=label, project=project, now=now, window_days=window_days, horizons=spans, prices=prices,
        series=series, policies=policies, jobs=jobs, selector=selector,
    )
    if outs:
        for w in write_snapshot(snapshot(doc, prices), outs):
            print(w)
        print(doc["summary"], file=sys.stderr)
        return
    print(json.dumps(doc, indent=2) if as_json else render(doc), end="\n" if as_json else "")

