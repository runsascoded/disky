"""Cost-attribution labels for everything a deployment creates on GCP.

One setting, from the env: ``DISKY_LABELS`` (``k=v,k=v``, e.g.
``app=disky,deployment=gcs``). Each job adds its own ``component`` (``scan``,
``listing``, ``static-names``, ``drill``, ``anchors``, ``sweep``, …), so a
billing export groups a shared project's spend by deployment and component
(specs/done/cost-labels.md).

Nothing here names a deployment, and nothing is opt-in (specs/done/cost-labels-required.md): a Batch
job without ``app``, ``deployment`` (from ``DISKY_LABELS``) and its ``component`` is refused
(``MissingCostLabels``), when its spec is labelled and again right before ``batch.submit_job`` posts it
(``check_spec``). ``label_batch_spec`` forwards ``DISKY_LABELS`` into every task's env, so a job that
submits helpers labels them identically.

Batch: ``allocationPolicy.labels`` is what reaches the VMs, disks and GPUs a job
creates (their Compute cost lines in the billing export); the job's own
``labels`` stay on the job. ``label_batch_spec`` sets both.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy

ENV = "DISKY_LABELS"
COMPONENT = "component"
RUN_ENV = "SCAN_RUN_ID"
SCAN_ENV = "SCAN_ID"
#: The labels every deployment's ``$DISKY_LABELS`` carries; with ``component``, the ones every Batch job does.
REQUIRED = ("app", "deployment")

# GCP label rules: ≤64 labels; keys start with a lowercase letter; keys and values are
# ≤63 chars of lowercase letters, digits, `_` and `-` (values may be empty).
KEY_RE = re.compile(r"[a-z][a-z0-9_-]{0,62}")
VALUE_RE = re.compile(r"[a-z0-9_-]{0,63}")
MAX_LABELS = 64


def parse_labels(text: str) -> dict[str, str]:
    """``"k=v,k2=v2"`` → ``{"k": "v", "k2": "v2"}``; raises on anything GCP would refuse."""
    out: dict[str, str] = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"{ENV}: {item!r} is not k=v")
        k, v = (s.strip() for s in item.split("=", 1))
        check_label(k, v)
        if k in out:
            raise ValueError(f"{ENV}: duplicate key {k!r}")
        out[k] = v
    if len(out) > MAX_LABELS:
        raise ValueError(f"{ENV}: {len(out)} labels (GCP allows {MAX_LABELS})")
    return out


def check_label(key: str, value: str) -> None:
    if not KEY_RE.fullmatch(key):
        raise ValueError(f"label key {key!r}: must match {KEY_RE.pattern}")
    if not VALUE_RE.fullmatch(value):
        raise ValueError(f"label {key}={value!r}: value must match {VALUE_RE.pattern}")


def env_labels(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The labels ``$DISKY_LABELS`` sets; ``{}`` when unset or empty (``deployment_labels`` requires them)."""
    return parse_labels((environ if environ is not None else os.environ).get(ENV, ""))


class MissingCostLabels(ValueError):
    """A Batch job (or the env it is labelled from) lacks a required cost label (`REQUIRED`, `COMPONENT`)."""

    def __init__(self, missing: list[str], where: str = "cost labels"):
        self.missing, self.where = missing, where
        super().__init__(f"{where}: missing {', '.join(missing)} "
                         f"(${ENV} carries {','.join(f'{k}=…' for k in REQUIRED)}; each job names its {COMPONENT})")


def deployment_labels(environ: dict[str, str] | None = None) -> dict[str, str]:
    """``$DISKY_LABELS``, refused (``MissingCostLabels``) unless it names ``app`` and ``deployment``."""
    labels = env_labels(environ)
    if missing := [k for k in REQUIRED if not labels.get(k)]:
        raise MissingCostLabels(missing)
    return labels


def cost_labels(component: str | None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """The deployment's labels (``deployment_labels``) plus ``component``, and the scan context (``scan_run`` /
    ``scan_id`` from ``$SCAN_RUN_ID`` / ``$SCAN_ID``) when set. Raises ``MissingCostLabels`` naming what's missing."""
    env = os.environ if environ is None else environ
    labels = env_labels(env)
    if missing := [k for k in REQUIRED if not labels.get(k)] + ([] if component else [COMPONENT]):
        raise MissingCostLabels(missing)
    check_label(COMPONENT, component)
    labels[COMPONENT] = component
    for var, key in ((RUN_ENV, "scan_run"), (SCAN_ENV, "scan_id")):
        if value := env.get(var):
            value = value.lower()
            check_label(key, value)
            labels[key] = value
    return labels


def check_spec(spec: dict, what: str = "Batch job") -> None:
    """Refuse (``MissingCostLabels``) a Batch spec whose job ``labels`` or ``allocationPolicy.labels`` lack a required
    cost label: ``batch.submit_job`` calls it right before the POST, so no job goes out unlabelled."""
    for where, labels in (("labels", spec.get("labels") or {}),
                          ("allocationPolicy.labels", (spec.get("allocationPolicy") or {}).get("labels") or {})):
        if missing := [k for k in (*REQUIRED, COMPONENT) if not labels.get(k)]:
            raise MissingCostLabels(missing, f"{what} {where}")


def label_batch_spec(
    spec: dict,
    component: str,
    environ: dict[str, str] | None = None,
    *,
    scan_id: str | None = None,
) -> dict:
    """A copy of a Batch job spec with the cost labels on the job and its allocation policy, and ``$DISKY_LABELS`` (+
    ``$SCAN_RUN_ID`` / ``$SCAN_ID`` when set) in every task group's env, so a job that submits helpers labels its
    grandchildren identically (their ``component`` their own).

    A spec's own keys (e.g. ``purpose``, ``stage``) are kept; a cost label of the same key overrides them. Under a scan's
    context, the job's ``stage`` reaches the allocation too. Raises ``MissingCostLabels`` (``cost_labels``).
    """
    env = dict(os.environ if environ is None else environ)
    if scan_id is not None:
        from .scan_id import check_scan_id

        env[SCAN_ENV] = check_scan_id(scan_id)
    labels = cost_labels(component, env)
    out = deepcopy(spec)
    out["labels"] = {**out.get("labels", {}), **labels}
    alloc = out.setdefault("allocationPolicy", {})
    alloc["labels"] = {**alloc.get("labels", {}), **labels}
    # Explicit context, not the child VM's own BATCH_JOB_UID: a detached job
    # can submit grandchildren after the root has finished.
    for group in out.get("taskGroups", []):
        variables = group.setdefault("taskSpec", {}).setdefault("environment", {}).setdefault("variables", {})
        variables[ENV] = env[ENV]
        for var in (RUN_ENV, SCAN_ENV):
            if env.get(var):
                variables[var] = env[var]
    if "scan_run" in labels or "scan_id" in labels:
        if stage := out["labels"].get("stage"):
            check_label("stage", stage)
            alloc["labels"]["stage"] = stage
    return out


def gcloud_flag(labels: dict[str, str]) -> str:
    """``k=v,k2=v2``, for ``gcloud … --update-labels`` / ``--labels``."""
    return ",".join(f"{k}={v}" for k, v in labels.items())


def render(component: str | None, fmt: str, environ: dict[str, str] | None = None) -> str:
    labels = cost_labels(component, environ)
    return json.dumps(labels, sort_keys=True) if fmt == "json" else gcloud_flag(labels)


# ── The audit (`dt-cloud cost-labels audit`): Batch VMs the billing export can't attribute ─────────────────────────

#: A billing-export table, `project.dataset.table` (backticked in the SQL; nothing else is interpolated but `days`).
TABLE_RE = re.compile(r"[A-Za-z0-9_-]+(?:[.:][A-Za-z0-9_-]+){1,2}")


def audit_sql(table: str, days: int) -> str:
    """The billing export's (`table`) Compute Engine cost lines of the last `days` days carrying a `batch-job-id` label
    but no `deployment`, per job: `job_id`, `first_day` (its earliest usage day), `net` (cost + credits). The export is
    ingestion-time partitioned, so `_PARTITIONTIME` (≥ a line's usage day) prunes it to the window."""
    if not TABLE_RE.fullmatch(table):
        raise ValueError(f"billing table {table!r}: expected project.dataset.table")
    if days < 1:
        raise ValueError(f"days: {days} < 1")
    since = f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)"
    return f"""SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'batch-job-id') AS job_id,
  MIN(DATE(usage_start_time)) AS first_day,
  SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)) AS net
FROM `{table.replace(":", ".")}`
WHERE _PARTITIONTIME >= TIMESTAMP_TRUNC({since}, DAY)
  AND usage_start_time >= {since}
  AND service.description = 'Compute Engine'
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'batch-job-id')
  AND NOT EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'deployment')
GROUP BY job_id
ORDER BY net DESC, job_id"""


def audit_rows(page: dict) -> list[tuple[str, str, float]]:
    """A BigQuery `jobs.query` / `getQueryResults` page's rows (`{"f": [{"v": …}]}`) as `(job_id, first_day, net)`."""
    return [(r["f"][0]["v"], r["f"][1]["v"], float(r["f"][2]["v"])) for r in page.get("rows", [])]


def audit_lines(rows: list[tuple[str, str, float]]) -> list[str]:
    """One `job_id<TAB>first_day<TAB>$net` line per unlabelled job, then a total when there are any."""
    if not rows:
        return []
    return [f"{job}\t{day}\t${net:,.2f}" for job, day, net in rows] + [f"total\t{len(rows)} jobs\t${sum(n for *_, n in rows):,.2f}"]


def query_rows(sql: str, project: str, timeout_s: int = 300) -> list[tuple[str, str, float]]:
    """Run `sql` (standard SQL) as a BigQuery job billed to `project` over REST (ADC), every page's rows (`audit_rows`)."""
    from time import monotonic

    from .gcp import session

    base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries"
    r = session().post(base, json={"query": sql, "useLegacySql": False, "timeoutMs": 60_000})
    r.raise_for_status()
    page, t0, rows = r.json(), monotonic(), []
    ref = page["jobReference"]
    params = {"location": ref.get("location"), "timeoutMs": 60_000}
    while True:
        if page.get("jobComplete"):
            rows.extend(audit_rows(page))
            if not (token := page.get("pageToken")):
                return rows
            params["pageToken"] = token
        elif monotonic() - t0 > timeout_s:
            raise TimeoutError(f"BigQuery job {ref['jobId']}: not done after {timeout_s}s")
        r = session().get(f"{base}/{ref['jobId']}", params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        page = r.json()
