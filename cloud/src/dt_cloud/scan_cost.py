"""Read-only per-scan compute estimates; never invoice amounts.

Root Batch identity plus propagated scan_run labels links downstream work,
including work which outlives the root. Legacy inference is explicitly opt-in.
Task status events identify VM assignments: union their intervals per VM so
packed tasks do not double-count a machine, and retain failed/retried attempts.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from click import argument, command, option

from .scan_id import check_scan_id
from .scan_runs import PROFILE_ENV, Profile, load_profile

INSTANCE = re.compile(r"zones/[^/\s]+/instances/(\d+)")
TERMINAL = {"SUCCEEDED", "FAILED", "DELETION_IN_PROGRESS"}
EXCLUSIONS = [
    "VM time before task assignment and after task completion",
    "boot disks and local SSDs",
    "network transfers, storage operations, and persistent index storage",
    "billing credits, negotiated discounts, and actual Spot prices",
]


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def labels_of(job: Mapping) -> dict:
    return {**job.get("labels", {}), **job.get("allocationPolicy", {}).get("labels", {})}


def commands_of(job: Mapping) -> str:
    return " ".join(
        c
        for g in job.get("taskGroups", [])
        for r in g.get("taskSpec", {}).get("runnables", [])
        for c in r.get("container", {}).get("commands", [])
    )


def select_jobs(
    jobs: Sequence[Mapping],
    root: Mapping,
    scan: str,
    *,
    legacy: bool = False,
) -> list[tuple[Mapping, str]]:
    check_scan_id(scan)
    root_labels = labels_of(root)
    if root_labels.get("scan_id") not in (None, scan.lower()):
        raise ValueError(f"root job is labeled for {root_labels['scan_id']}, not {scan}")
    root_id = root_labels.get("scan_run") or root["uid"]
    out = []
    start = timestamp(root["createTime"])
    end = timestamp(root["updateTime"]) if root["status"]["state"] in TERMINAL else float("inf")
    stamp = scan.lower().replace("t", "-")
    name_pattern = re.compile(rf"^(?:sn|iv)-[a-z0-9-]+-{re.escape(stamp)}-\d{{6}}$")
    for job in jobs:
        labels = labels_of(job)
        if job["uid"] == root["uid"]:
            out.append((job, "root"))
        elif labels.get("scan_run") == root_id:
            out.append((job, "scan_run_label"))
        elif legacy and not labels.get("scan_run"):
            # No broad time-window attribution: exact scan identity AND the
            # root's dispatch window AND matching deployment are required.
            scope = {k: root_labels[k] for k in ("app", "deployment") if k in root_labels}
            same_deploy = bool(scope) and all(labels.get(k) == v for k, v in scope.items())
            in_window = start <= timestamp(job["createTime"]) <= end
            name = job["name"].rsplit("/", 1)[-1]
            exact_scan = bool(name_pattern.fullmatch(name)) or (
                labels.get("component") == "listing" and f"/listing/{scan}/" in commands_of(job)
            )
            if same_deploy and in_window and exact_scan:
                out.append((job, "legacy_scan_and_window"))
    return sorted(out, key=lambda pair: pair[0]["name"])


def union_seconds(intervals: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    end = float("-inf")
    for a, b in sorted(intervals):
        if b < a:
            raise ValueError("task assignment ends before it starts")
        total += max(0.0, b - max(a, end))
        end = max(end, b)
    return total


def task_usage(tasks: Sequence[Mapping], now: float) -> dict:
    intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
    task_seconds = 0.0
    attempts = 0
    warnings = []
    for task in tasks:
        assigned = running = None
        instance = None
        seen_run = False
        events = sorted(task.get("status", {}).get("statusEvents", []), key=lambda e: e["eventTime"])
        for event in events:
            state = event.get("taskState")
            if state is None:
                continue
            t = timestamp(event["eventTime"])
            if state in ("ASSIGNED", "RUNNING"):
                match = INSTANCE.search(event.get("description", ""))
                if match:
                    if instance and instance != match[1]:
                        raise ValueError(f"{task['name']}: instance changed without closing its assignment")
                    instance = match[1]
                if assigned is None:
                    assigned = t
                if state == "RUNNING" and running is None:
                    running = t
                    attempts += 1
                    seen_run = True
            elif assigned is not None:
                if running is not None:
                    task_seconds += t - running
                if instance:
                    intervals[instance].append((assigned, t))
                else:
                    warnings.append(f"{task['name']}: assignment has no VM identity")
                assigned = running = instance = None
        if assigned is not None:
            if task["status"]["state"] in ("SUCCEEDED", "FAILED", "UNEXECUTED"):
                warnings.append(f"{task['name']}: terminal task has an unclosed assignment")
            else:
                if running is not None:
                    task_seconds += max(0.0, now - running)
                if instance:
                    intervals[instance].append((assigned, max(assigned, now)))
                else:
                    warnings.append(f"{task['name']}: live assignment has no VM identity")
        if not seen_run and task.get("status", {}).get("state") in ("SUCCEEDED", "FAILED"):
            warnings.append(f"{task['name']}: no execution events")
    return {
        "attempts": attempts,
        "task_seconds": round(task_seconds, 3),
        "observed_vm_seconds": round(sum(union_seconds(v) for v in intervals.values()), 3),
        "observed_vms": len(intervals),
        "warnings": warnings,
    }


def job_estimate(
    profile: Profile,
    job: Mapping,
    tasks: Sequence[Mapping],
    now: float,
    association: str,
) -> dict:
    labels = labels_of(job)
    policies = [x.get("policy", {}) for x in job.get("allocationPolicy", {}).get("instances", [])]
    shapes = {(p.get("machineType"), p.get("provisioningModel", "STANDARD")) for p in policies}
    warnings = []
    machine = model = rate = None
    if len(shapes) != 1:
        warnings.append("unknown or mixed VM shapes: no price estimate")
    else:
        machine, model = next(iter(shapes))
        rate = profile.prices.get(machine)
        if rate is None:
            warnings.append(f"no configured price for {machine}")
        elif model == "SPOT":
            if profile.spot_factor is None:
                warnings.append("no configured Spot multiplier")
                rate = None
            else:
                rate *= profile.spot_factor
    expected = sum(int(g.get("taskCount", 1)) for g in job.get("taskGroups", []))
    if len(tasks) != expected:
        warnings.append(f"task coverage incomplete: {len(tasks)}/{expected}")
    usage = task_usage(tasks, now)
    warnings += usage.pop("warnings")
    return {
        "job": job["name"].rsplit("/", 1)[-1],
        "uid": job["uid"],
        "region": job["name"].split("/")[3],
        "association": association,
        "component": labels.get("component", "unknown"),
        "stage": labels.get("stage"),
        # a Nextflow task's process (`base.config` `resourceLabels`): which pipeline step the task ran
        "step": labels.get("step"),
        # a carry or fold (`append_runner.UPKEEP_LABEL`): the generation's upkeep, serving every scan it merges
        "upkeep": labels.get("upkeep"),
        "scan_id": labels.get("scan_id"),
        "state": job["status"]["state"],
        "machine": machine,
        "provisioning_model": model,
        "tasks": expected,
        **usage,
        "configured_hourly_usd": rate,
        "estimated_compute_usd": round(rate * usage["observed_vm_seconds"] / 3600, 4) if rate is not None and not warnings else None,
        "disks_per_vm": [{"type": d.get("type"), "size_gb": int(d.get("sizeGb", 0))}
                         for p in policies for d in [p.get("bootDisk", {}), *(v.get("newDisk", {}) for v in p.get("disks", []))] if d],
        "warnings": warnings,
    }


def report(
    profile: Profile,
    root: Mapping,
    scan: str,
    jobs: Sequence[tuple[Mapping, str]],
    tasks: Mapping[str, Sequence[Mapping]],
    now: float,
) -> dict:
    rows = [job_estimate(profile, job, tasks[job["uid"]], now, association) for job, association in jobs]
    components: dict[str, float] = defaultdict(float)
    upkeep: dict[str, float] = defaultdict(float)
    for row in rows:
        if row["estimated_compute_usd"] is not None:
            if row["upkeep"]:
                upkeep[row["upkeep"]] += row["estimated_compute_usd"]
            else:
                components[row["component"]] += row["estimated_compute_usd"]
    return {
        "scan": scan,
        "scan_run": labels_of(root).get("scan_run") or root["uid"],
        "root_uid": root["uid"],
        "as_of": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "estimate_basis": "union of observed task assignment intervals per VM, at configured profile rates",
        "exclusions": EXCLUSIONS,
        "legacy_inference": any(a == "legacy_scan_and_window" for _, a in jobs),
        "ongoing_jobs": [r["job"] for r in rows if r["state"] not in TERMINAL],
        "unpriced_jobs": [r["job"] for r in rows if r["estimated_compute_usd"] is None],
        "estimated_compute_usd": round(sum(components.values()), 4),
        "components": {k: round(v, 4) for k, v in sorted(components.items())},
        # carries and folds the run submitted: the generation's, not in the run's total (specs/scan-cost-by-stage.md)
        "upkeep_usd": {k: round(v, 4) for k, v in sorted(upkeep.items())},
        "billing_filter": {"root_batch_job_uid": root["uid"], "child_scan_run": labels_of(root).get("scan_run") or root["uid"]},
        "jobs": rows,
    }


def render_report(doc: Mapping) -> str:
    lines = [f"{doc['scan']} · {doc['scan_run']}", "Observed compute estimate (not billed cost):"]
    for row in doc["jobs"]:
        amount = "unknown" if row["estimated_compute_usd"] is None else f"${row['estimated_compute_usd']:.4f}"
        lines.append(f"  {row['component']}/{row['stage'] or '-'}{f' ({row["upkeep"]}, not in the total)' if row['upkeep'] else ''}  {row['state']}  {row['machine']}  {row['observed_vm_seconds'] / 3600:.3f} VM-h  {amount}  {row['job']}")
        lines.extend(f"    WARNING: {w}" for w in row["warnings"])
    lines.append(f"Total priced observed compute: ${doc['estimated_compute_usd']:.4f}")
    if doc["legacy_inference"]:
        lines.append("WARNING: legacy children inferred by exact scan ID + dispatch window; overlapping retries cannot be attributed reliably.")
    if doc["ongoing_jobs"]:
        lines.append("ONGOING: " + ", ".join(doc["ongoing_jobs"]))
    if doc["unpriced_jobs"]:
        lines.append("PARTIAL: " + ", ".join(doc["unpriced_jobs"]))
    lines.append("Rates: configured on-demand prices and Spot multiplier; not live regional billing rates.")
    lines.extend(f"Excluded: {x}." for x in doc["exclusions"])
    return "\n".join(lines) + "\n"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def build_report(profile: Profile, jobs: Sequence[Mapping], run_id: str, scan: str, *, legacy: bool = False,
                 now: float | None = None) -> dict:
    """RUN_ID's (a root job's uid, name or short name, among `jobs`) report (`report`), its tasks fetched."""
    from .gcp import batch_tasks

    roots = [j for j in jobs if run_id in (j["uid"], j["name"], j["name"].rsplit("/", 1)[-1])]
    if len(roots) != 1:
        raise ValueError(f"expected one root job for {run_id!r}; found {len(roots)}")
    root = roots[0]
    selected = select_jobs(jobs, root, scan, legacy=legacy)
    tasks = {j["uid"]: batch_tasks(j) for j, _ in selected}
    return report(profile, root, scan, selected, tasks, _now() if now is None else now)


def billed_sql(table: str, days: int, deployment: str) -> str:
    """The billing export's (`table`) net cost (cost + credits) per Batch job of `deployment` over the last `days` days,
    of the jobs a scan run labelled (`scan_run`): `job_uid`, `net`, `last_day` (its latest usage day). The export is
    ingestion-time partitioned (`_PARTITIONTIME` prunes it) and lags about a day."""
    from .cost_labels import TABLE_RE, check_label

    if not TABLE_RE.fullmatch(table):
        raise ValueError(f"billing table {table!r}: expected project.dataset.table")
    if days < 1:
        raise ValueError(f"days: {days} < 1")
    check_label("deployment", deployment)
    since = f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)"
    return f"""SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'batch-job-id') AS job_uid,
  SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)) AS net,
  MAX(DATE(usage_start_time)) AS last_day
FROM `{table.replace(":", ".")}`
WHERE _PARTITIONTIME >= TIMESTAMP_TRUNC({since}, DAY)
  AND usage_start_time >= {since}
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'batch-job-id')
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'scan_run')
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'deployment' AND value = '{deployment}')
GROUP BY job_uid"""


def billed_rows(page: dict) -> list[tuple[str, float, str]]:
    """A BigQuery page's rows (`billed_sql`) as `(job_uid, net, last_day)`."""
    return [(r["f"][0]["v"], float(r["f"][1]["v"]), r["f"][2]["v"]) for r in page.get("rows", [])]


def with_billed(doc: dict, billed: Mapping[str, float]) -> dict:
    """`doc` (a run's report) with what the billing export says each job cost (`billed_usd`, None: no line yet: the
    export lags a day), and the run's billed totals beside the estimate: `billed_usd` (its own jobs), `billed_upkeep_usd`
    (its carries and folds), `billed_jobs` (how many of its jobs have a line)."""
    jobs = [{**j, "billed_usd": round(billed[j["uid"]], 4) if j["uid"] in billed else None} for j in doc["jobs"]]
    own = [j["billed_usd"] for j in jobs if j["billed_usd"] is not None and not j.get("upkeep")]
    upkeep = [j["billed_usd"] for j in jobs if j["billed_usd"] is not None and j.get("upkeep")]
    return {**doc, "jobs": jobs, "billed_usd": round(sum(own), 4) if own else None,
            "billed_upkeep_usd": round(sum(upkeep), 4) if upkeep else None,
            "billed_jobs": sum(j["billed_usd"] is not None for j in jobs)}


def report_key(scan: str, root_uid: str) -> str:
    """Where a run's saved report lives under a reports root: `<scan>/<root uid>.json`."""
    return f"{check_scan_id(scan)}/{root_uid}.json"


def saved(store, days: int, now: float) -> list[tuple[str, str]]:
    """Every saved report (`(scan, root uid)`) of the scans within `days` of `now`."""
    from .scan_id import scan_time

    out = []
    for key in store.keys(""):
        scan, _, name = key.partition("/")
        if not name.endswith(".json") or "/" in name:
            continue
        try:
            when = scan_time(scan).timestamp()
        except ValueError:
            continue
        if now - when <= days * 86400:
            out.append((scan, name.removesuffix(".json")))
    return sorted(out)


def unsettled(store, days: int, now: float) -> list[tuple[str, str]]:
    """The saved reports (`saved`) that still listed a job running (a run's head, a detached carry or fold): reread once
    those end, so the saved one is the whole run's."""
    return [k for k in saved(store, days, now) if store.read_json(report_key(*k)).get("ongoing_jobs")]


@command("cost")
@option("-c", "--profile", "profile_path", default=None, help=f"Scan-run profile (default ${PROFILE_ENV})")
@option("-j", "--json", "as_json", is_flag=True, help="Structured resource/cost report")
@option("-l", "--legacy", is_flag=True, help="Infer pre-label children by exact scan ID and dispatch window")
@option("-o", "--out", type=Path, default=None, help="Save the report instead of printing it")
@option("-O", "--save", "roots", multiple=True, help="Save the JSON report as <ROOT>/<scan>/<root uid>.json (a dir or URL; repeatable), and reread the saved reports of the last `-r` days' scans that still listed a job running")
@option("-B", "--billing-table", help="With -O: the billing export (`project.dataset.table`): each saved report of the last `-r` days gets what each of its jobs billed (the export lags about a day)")
@option("-d", "--deployment", help="With -B: the jobs' `deployment` label (default: $DISKY_LABELS')")
@option("-r", "--refresh-days", type=int, default=7, show_default=True, help="With -O: how far back unsettled saved reports are reread")
@option("-s", "--scan", required=True, help="Exact scan ID, not a day prefix")
@argument("run_id")
def cost_cmd(
    profile_path: str | None,
    as_json: bool,
    legacy: bool,
    out: Path | None,
    roots: tuple[str, ...],
    billing_table: str | None,
    deployment: str | None,
    refresh_days: int,
    scan: str,
    run_id: str,
) -> None:
    """Estimate RUN_ID's parent and child compute (Batch job name or UID); read-only.

    Includes detached jobs carrying the root label. Rates are deployment config,
    not an invoice. Save JSON to retain the evidence before Batch expires jobs.
    """
    from .blobstore import Store
    from .gcp import batch_jobs

    profile = load_profile(profile_path)
    check_scan_id(scan)
    jobs = [j for region in profile.regions for j in batch_jobs(profile.project, region)]
    now = _now()
    doc = build_report(profile, jobs, run_id, scan, legacy=legacy, now=now)
    if roots:
        stores = [Store(r) for r in roots]
        docs = {(scan, doc["root_uid"]): doc}
        for s_, uid in unsettled(stores[0], refresh_days, now):
            if (s_, uid) not in docs and any(j["uid"] == uid for j in jobs):
                docs[(s_, uid)] = build_report(profile, jobs, uid, s_, legacy=legacy, now=now)
        if billing_table:
            from .cost_labels import deployment_labels, query_rows

            dep = deployment or deployment_labels(dict(os.environ))["deployment"]
            billed = {uid: net for uid, net, _ in query_rows(billed_sql(billing_table, refresh_days + 2, dep), profile.project,
                                                              parse=billed_rows)}
            # every saved report of the window, settled or not: the export's lines for a job arrive a day after it ran
            for s_, uid in saved(stores[0], refresh_days, now):
                docs.setdefault((s_, uid), stores[0].read_json(report_key(s_, uid)))
            docs = {k: with_billed(d, billed) for k, d in docs.items()}
        for (s_, uid), d in sorted(docs.items()):
            data = (json.dumps(d, indent=2, sort_keys=True) + "\n").encode()
            for st in stores:
                st.write_bytes(report_key(s_, uid), data)
            billed = f", billed ${d['billed_usd']:.2f}" if d.get("billed_usd") is not None else ""
            print(f"cost: {s_} {uid}: ${d['estimated_compute_usd']:.2f} (+ upkeep ${sum(d.get('upkeep_usd', {}).values()):.2f}){billed}"
                  + (f", still running: {', '.join(d['ongoing_jobs'])}" if d["ongoing_jobs"] else ""), file=sys.stderr)
        return
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n" if as_json else render_report(doc)
    if out is None:
        print(text, end="")
    else:
        out.write_text(text)
