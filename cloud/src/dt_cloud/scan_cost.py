"""Read-only per-scan compute estimates; never invoice amounts.

Root Batch identity plus propagated scan_run labels links downstream work,
including work which outlives the root. Legacy inference is explicitly opt-in.
Task status events identify VM assignments: union their intervals per VM so
packed tasks do not double-count a machine, and retain failed/retried attempts.
"""
from __future__ import annotations

import json
import re
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
    for row in rows:
        if row["estimated_compute_usd"] is not None:
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
        "billing_filter": {"root_batch_job_uid": root["uid"], "child_scan_run": labels_of(root).get("scan_run") or root["uid"]},
        "jobs": rows,
    }


def render_report(doc: Mapping) -> str:
    lines = [f"{doc['scan']} · {doc['scan_run']}", "Observed compute estimate (not billed cost):"]
    for row in doc["jobs"]:
        amount = "unknown" if row["estimated_compute_usd"] is None else f"${row['estimated_compute_usd']:.4f}"
        lines.append(f"  {row['component']}/{row['stage'] or '-'}  {row['state']}  {row['machine']}  {row['observed_vm_seconds'] / 3600:.3f} VM-h  {amount}  {row['job']}")
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


@command("cost")
@option("-c", "--profile", "profile_path", default=None, help=f"Scan-run profile (default ${PROFILE_ENV})")
@option("-j", "--json", "as_json", is_flag=True, help="Structured resource/cost report")
@option("-l", "--legacy", is_flag=True, help="Infer pre-label children by exact scan ID and dispatch window")
@option("-o", "--out", type=Path, default=None, help="Save the report instead of printing it")
@option("-s", "--scan", required=True, help="Exact scan ID, not a day prefix")
@argument("run_id")
def cost_cmd(
    profile_path: str | None,
    as_json: bool,
    legacy: bool,
    out: Path | None,
    scan: str,
    run_id: str,
) -> None:
    """Estimate RUN_ID's parent and child compute (Batch job name or UID); read-only.

    Includes detached jobs carrying the root label. Rates are deployment config,
    not an invoice. Save JSON to retain the evidence before Batch expires jobs.
    """
    from .gcp import batch_jobs, batch_tasks

    profile = load_profile(profile_path)
    check_scan_id(scan)
    jobs = [j for region in profile.regions for j in batch_jobs(profile.project, region)]
    roots = [j for j in jobs if run_id in (j["uid"], j["name"], j["name"].rsplit("/", 1)[-1])]
    if len(roots) != 1:
        raise ValueError(f"expected one root job for {run_id!r}; found {len(roots)}")
    root = roots[0]
    selected = select_jobs(jobs, root, scan, legacy=legacy)
    tasks = {j["uid"]: batch_tasks(j) for j, _ in selected}
    doc = report(profile, root, scan, selected, tasks, datetime.now(timezone.utc).timestamp())
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n" if as_json else render_report(doc)
    if out is None:
        print(text, end="")
    else:
        out.write_text(text)
