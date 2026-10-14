#!/usr/bin/env python3
"""m3's scan-run backfill: past capture ingests (AWS Batch) as `dt-cloud scan-run` records (specs/scan-runs-ui.md).

`ingest.sh` records its own run from now on (`SCAN_RUNS_PROFILE`); this rebuilds the ones before that from what
outlives a run, read-only:

- R2 (`disk-tree`): each capture's `_SUCCESS.json` (its `LastModified` ends the capture phase) and `_INGEST.json`
  (the job the trigger submitted), and the sizes of the outputs under `listing/` and `snapshots/`.
- CloudWatch (`/disk-tree-m3-ingest/batch`, 30-day retention): each attempt's task log. Its `[ingest] capture=…`
  line names the capture, scan id, gen and stores; the `fetched`, `wrote …/snap/`, `path-index:`, `uploaded` and
  `footers → D1` lines end the phases (or `PHASE <name>: <s>s` markers, when a run printed them).
- Batch (`disk-tree-m3`, jobs kept ~a week): the job id, name, state, attempt times, failure reason and its job
  definition's size (the machine the profile prices).

A run is one Batch job (its id the run id); a task log whose job Batch no longer lists takes the capture's
`_INGEST.json` job id when that is the capture's only such log, else its ECS task id. Writes go through
`dt_cloud.scan_runs` (`source = 'backfill'`; a run the job recorded itself keeps its own source).

    scan_runs_backfill.py -w tmp/bf/inputs -n > tmp/bf/backfill.sql   # fetch, save the inputs, print the SQL
    scan_runs_backfill.py -i tmp/bf/inputs -n                          # replay saved inputs
    scan_runs_backfill.py -i tmp/bf/inputs                             # write D1 ($D1_DB_ID + Cloudflare creds)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from click import command, option

from dt_cloud.scan_id import is_scan_id
from dt_cloud.scan_runs import Output, Phase, Profile, Record, Run, as_json, billed_secs, cost, load_profile, sink_for, write_all

err = partial(print, file=sys.stderr)

HERE = Path(__file__).resolve().parent
STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})Z$")
CAPTURE_LINE = re.compile(r"^\[ingest\] capture=(?P<key>\S+) date=(?P<scan>\S+) gen=(?P<gen>\S+)(?P<rest>.*)$")
#: The ingest's own lines, in order: each ends its phase (`ingest.sh` before it printed `PHASE` markers).
LINE_PHASES = (
    ("fetch", re.compile(r"^\[ingest\] fetched ")),
    ("path-index", re.compile(r"^wrote \S+/snap/")),
    ("capture-meta", re.compile(r"^\[ingest\] path-index: ")),
    ("upload", re.compile(r"^\[ingest\] uploaded ")),
    ("index-sync", re.compile(r"^\[ingest\] footers → D1")),
)
ORDER = [p for p, _ in LINE_PHASES]
INDEX_SYNC_STARTED = re.compile(r"^index-sync: ")
CAPTURE_META_OUT = re.compile(r"^/\S+/snap/meta\.json$")
BATCH_STATES = {"SUCCEEDED": "succeeded", "FAILED": "failed", "RUNNING": "running", "STARTING": "running",
                "RUNNABLE": "running", "PENDING": "running", "SUBMITTED": "running"}


def epoch(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def stamp_epoch(stamp: str) -> int | None:
    m = STAMP.search(stamp)
    if not m:
        return None
    y, mo, d, h, mi, s = map(int, m.groups())
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone.utc).timestamp())


# ── The inputs ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Inputs:
    """Everything the backfill reads, as saved by `-w` (one JSON file each)."""
    bucket: str
    #: `captures/`, `listing/` and `snapshots/` objects: `{key, size, last_modified}` (ISO, UTC).
    objects: Sequence[Mapping]
    #: Each capture dir's `_INGEST.json`, by the dir's key.
    ingests: Mapping[str, Mapping]
    #: `batch describe-jobs` results.
    jobs: Sequence[Mapping]
    #: `batch describe-job-definitions` results (every revision).
    jobdefs: Sequence[Mapping]
    #: Task-log events by stream name: `{timestamp (ms), message}`.
    logs: Mapping[str, Sequence[Mapping]]

    def save(self, d: Path) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / "r2.json").write_text(json.dumps({"bucket": self.bucket, "objects": list(self.objects), "ingests": dict(self.ingests)}, indent=1) + "\n")
        (d / "jobs.json").write_text(json.dumps(list(self.jobs), indent=1) + "\n")
        (d / "jobdefs.json").write_text(json.dumps(list(self.jobdefs), indent=1) + "\n")
        (d / "logs.json").write_text(json.dumps(dict(self.logs), indent=1) + "\n")

    @classmethod
    def load(cls, d: Path) -> Inputs:
        r2 = json.loads((d / "r2.json").read_text())
        return cls(r2["bucket"], r2["objects"], r2["ingests"], json.loads((d / "jobs.json").read_text()),
                   json.loads((d / "jobdefs.json").read_text()), json.loads((d / "logs.json").read_text()))


def fetch(
    bucket: str,
    endpoint: str,
    r2_profile: str | None,
    aws_profile: str | None,
    region: str,
    queue: str,
    job_def: str,
    log_group: str,
) -> Inputs:
    """The inputs from R2, Batch and CloudWatch (all reads)."""
    import boto3

    r2 = boto3.Session(profile_name=r2_profile).client("s3", endpoint_url=endpoint, region_name="auto")
    objects = []
    for prefix in ("captures/", "listing/", "snapshots/"):
        for page in r2.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            objects += [{"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"].astimezone(dt.timezone.utc).isoformat()}
                        for o in page.get("Contents", ())]
    ingests = {o["key"].rsplit("/", 1)[0]: json.loads(r2.get_object(Bucket=bucket, Key=o["key"])["Body"].read())
               for o in objects if o["key"].startswith("captures/") and o["key"].endswith("/_INGEST.json")}
    aws = boto3.Session(profile_name=aws_profile, region_name=region)
    batch, logs = aws.client("batch"), aws.client("logs")
    ids = []
    for status in ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING", "SUCCEEDED", "FAILED"):
        for page in batch.get_paginator("list_jobs").paginate(jobQueue=queue, jobStatus=status):
            ids += [j["jobId"] for j in page["jobSummaryList"]]
    jobs = [j for i in range(0, len(ids), 100) for j in batch.describe_jobs(jobs=ids[i:i + 100])["jobs"]]
    jobdefs = [d for page in batch.get_paginator("describe_job_definitions").paginate(jobDefinitionName=job_def)
               for d in page["jobDefinitions"]]
    events: dict[str, list[dict]] = {}
    for page in logs.get_paginator("describe_log_streams").paginate(logGroupName=log_group):
        for s in page["logStreams"]:
            out, tok = [], None
            while True:
                kw = {"nextToken": tok} if tok else {}
                r = logs.get_log_events(logGroupName=log_group, logStreamName=s["logStreamName"], startFromHead=True, **kw)
                out += [{"timestamp": e["timestamp"], "message": e["message"]} for e in r["events"]]
                if r["nextForwardToken"] == tok:
                    break
                tok = r["nextForwardToken"]
            events[s["logStreamName"]] = out
    return Inputs(bucket, objects, ingests, _jsonable(jobs), _jsonable(jobdefs), events)


def _jsonable(v):
    return json.loads(json.dumps(v, default=str))


# ── Reconstruction ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Attempt:
    """One task log: what the ingest printed."""
    stream: str
    capture: str  # the capture dir's key
    scan: str
    gen: str
    store: str  # the snapshot dir (`laptop`, `nas`)
    d1_store: str  # `scan_runs.store`
    start: int
    end: int
    phases: tuple[tuple[str, int], ...]  # (phase, end) in order
    sync_started: bool


def attempt_of(stream: str, events: Sequence[Mapping], profile: Profile) -> Attempt | None:
    """The attempt a task log records, or None (no `[ingest] capture=` line: not an ingest's log)."""
    lines = [(e["timestamp"] // 1000, e["message"].rstrip("\n")) for e in sorted(events, key=lambda e: e["timestamp"])]
    head = next(((t, m) for t, x in lines if (m := CAPTURE_LINE.match(x))), None)
    if head is None:
        return None
    _, m = head
    fields_ = dict(kv.split("=", 1) for kv in m.group("rest").split() if "=" in kv)
    marker = re.compile(profile.phase_marker)
    marked = [(mm.group("name"), t) for t, x in lines if (mm := marker.search(x)) and mm.group("name") not in profile.phase_ignore]
    if marked:
        phases = tuple(marked)
    else:
        ends: dict[str, int] = {}
        for t, x in lines:
            for name, rx in LINE_PHASES:
                if rx.search(x):
                    ends[name] = t
        if "capture-meta" in ends and "path-index" not in ends:  # an ingest before `path-index` said what it wrote
            ends["path-index"] = ends.pop("capture-meta")
        elif "capture-meta" in ends and not any(CAPTURE_META_OUT.match(x) for _, x in lines):  # no capture-meta then
            ends.pop("capture-meta")
        phases = tuple((p, ends[p]) for p in ORDER if p in ends)
    store = fields_.get("store", "laptop")
    return Attempt(stream, m.group("key").rstrip("/"), m.group("scan"), m.group("gen"), store, fields_.get("d1", "primary"),
                   lines[0][0], lines[-1][0], phases, any(INDEX_SYNC_STARTED.match(x) for _, x in lines))


def machine_of(jobdef: Mapping | None) -> str | None:
    """`fargate-<arch>-<n>vcpu-<n>gib` from a job definition's size (the profile's `prices` keys)."""
    if not jobdef:
        return None
    c = jobdef["containerProperties"]
    req = {r["type"]: r["value"] for r in c.get("resourceRequirements", ())}
    arch = (c.get("runtimePlatform") or {}).get("cpuArchitecture", "X86_64").lower().replace("_", "-")
    mem = int(req["MEMORY"]) / 1024
    return f"fargate-{arch}-{req['VCPU']}vcpu-{mem:g}gib"


def failure_of(job: Mapping) -> str | None:
    if job.get("status") != "FAILED":
        return None
    att = (job.get("attempts") or [{}])[-1]
    rc = att.get("container", {}).get("exitCode", job.get("container", {}).get("exitCode"))
    reason = att.get("statusReason") or job.get("statusReason") or "failed"
    return f"Batch: {reason}" + (f" (exit {rc})" if rc is not None else "")


def runs(profile: Profile, inp: Inputs) -> list[Record]:
    """Every ingest run the inputs rebuild, oldest first."""
    attempts = {s: a for s, ev in inp.logs.items() if (a := attempt_of(s, ev, profile))}
    markers = {o["key"].rsplit("/", 1)[0]: epoch(o["last_modified"]) for o in inp.objects
               if o["key"].startswith("captures/") and o["key"].endswith("/_SUCCESS.json")}
    jobdefs = {d["jobDefinitionArn"]: d for d in inp.jobdefs}
    groups: list[tuple[Mapping | None, list[Attempt], str]] = []  # (Batch job, its attempts' logs, run id)
    claimed: set[str] = set()
    for job in inp.jobs:
        streams = [a.get("container", {}).get("logStreamName") for a in job.get("attempts", ())] or [job.get("container", {}).get("logStreamName")]
        claimed.update(s for s in streams if s)
        if atts := [attempts[s] for s in streams if s in attempts]:  # a job whose logs are gone names no scan
            groups.append((job, atts, job["jobId"]))
    known = {j["jobId"] for j in inp.jobs}
    by_capture: dict[str, list[Attempt]] = defaultdict(list)
    for s, a in attempts.items():
        if s not in claimed:
            by_capture[a.capture].append(a)
    for cap, atts in by_capture.items():
        job_id = (inp.ingests.get(cap) or {}).get("job_id")
        for a in atts:
            rid = job_id if job_id and len(atts) == 1 and job_id not in known else a.stream.rsplit("/", 1)[-1]
            groups.append((None, [a], rid))
    built = [(r, max(atts, key=lambda a: a.start)) for job, atts, rid in groups
             if (r := record_of(profile, job, atts, rid, markers, jobdefs))]
    built.sort(key=lambda ra: (ra[0].run.started_ts or 0, ra[0].run.run_id))
    return attach_outputs(inp, built)


def record_of(
    profile: Profile,
    job: Mapping | None,
    atts: Sequence[Attempt],
    run_id: str,
    markers: Mapping[str, int],
    jobdefs: Mapping[str, Mapping],
) -> Record | None:
    """One run: its Batch job (None when Batch no longer lists it) and its attempts' logs (phases from the last)."""
    first, last = min(atts, key=lambda a: a.start), max(atts, key=lambda a: a.start)
    if not is_scan_id(last.scan):
        return None
    captured = stamp_epoch(last.capture.rsplit("/", 1)[-1])
    marked = markers.get(last.capture)
    if job:
        jatts = job.get("attempts") or [job]
        job_start = (jatts[0].get("startedAt") or first.start * 1000) // 1000
        att_start = (jatts[-1].get("startedAt") or last.start * 1000) // 1000
        status = BATCH_STATES.get(job.get("status", ""), "running")
        end = (job.get("stoppedAt") or 0) // 1000 or last.end
    else:
        job_start, att_start, end = first.start, last.start, last.end
        final = "index-sync" if last.sync_started else "upload"  # an ingest before index-sync ended at its upload
        status = "succeeded" if last.phases and last.phases[-1][0] == final else "failed"
    phases: list[Phase] = []
    if captured is not None and marked is not None and captured <= marked <= job_start:  # no marker: no split, no phases
        phases.append(Phase("capture", 0, captured, marked))
        phases.append(Phase("queue", 1, marked, job_start))
    prev = att_start
    for name, t in last.phases:
        phases.append(Phase(name, len(phases), prev, t))
        prev = t
    failed_phase = error = None
    if status == "failed":
        done = {p for p, _ in last.phases}
        failed_phase = next((p for p in ORDER if p not in done), None)
        error = failure_of(job) if job else "the task log stops before the ingest finished"
    started = captured if captured is not None else job_start
    finished = None if status == "running" else end
    machine = machine_of(jobdefs.get(job.get("jobDefinition"))) if job else None
    image = job.get("container", {}).get("image") if job else None
    return Record(Run(
        run_id=run_id, scan=last.scan, kind="ingest", status=status, source="backfill", store=last.d1_store,
        failed_phase=failed_phase, error=error, started_ts=started, finished_ts=finished,
        job_name=job.get("jobName") if job else None, region=job["jobArn"].split(":")[3] if job else None,
        machine=machine, spot=1, tasks=1, image=image.rsplit("/", 1)[-1] if image else None,
        cost_usd=cost(profile, machine, True, 1, billed_secs(profile, started, finished, phases)), gen=last.gen,
    ), tuple(phases))


def attach_outputs(inp: Inputs, built: Sequence[tuple[Record, Attempt]]) -> list[Record]:
    """The capture, index and snapshot sizes, on each (store, scan)'s latest succeeded run (a re-ingest rewrites the
    same paths)."""
    latest = {(r.run.store, r.run.scan): r.run.run_id for r, _ in built if r.run.status == "succeeded"}
    objects = [(o["key"], int(o["size"])) for o in inp.objects]
    out = []
    for r, a in built:
        if latest.get((r.run.store, r.run.scan)) != r.run.run_id:
            out.append(r)
            continue
        outs = []
        for key, prefix in (("capture", f"{a.capture}/"), ("index", f"listing/{a.store}/{a.scan}/index/{a.gen}/"),
                            ("snapshot", f"snapshots/{a.store}/{a.scan}/")):
            sizes = [n for k, n in objects if k.startswith(prefix)]
            if sizes:
                outs.append(Output(key, f"r2://{inp.bucket}/{prefix}", bytes=sum(sizes), objects=len(sizes), gen=a.gen if key == "index" else None))
        out.append(replace(r, outputs=tuple(outs)))
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────


@command()
@option("-A", "--aws-profile", default="r", help="AWS profile for Batch + CloudWatch (default r: RAC AWS)")
@option("-b", "--bucket", default="disk-tree", help="The R2 bucket the captures and outputs are in")
@option("-c", "--profile", "profile_path", default=str(HERE / "scan-runs.json"), help="The scan-run profile (default the one beside this script)")
@option("-d", "--d1", default=None, help="D1 database id (default $D1_DB_ID)")
@option("-e", "--endpoint", envvar="DISK_TREE_R2_ENDPOINT_URL", default="https://0dcad5654e9744de6616f74b8df4af63.r2.cloudflarestorage.com", help="R2 endpoint (default $DISK_TREE_R2_ENDPOINT_URL, else m3's account)")
@option("-g", "--log-group", default="/disk-tree-m3-ingest/batch", help="The ingest's CloudWatch log group")
@option("-i", "--inputs", default=None, help="Read the inputs from this dir (as `-w` saved them) instead of fetching")
@option("-j", "--json-out", default=None, help="Also write the records as JSON to this file")
@option("-J", "--job-def", default="disk-tree-m3-ingest", help="The ingest's Batch job definition (its revisions' sizes are the runs' machines)")
@option("-n", "--dry-run", is_flag=True, help="Print the SQL instead of writing it")
@option("-o", "--summary", is_flag=True, help="One line per run on stderr")
@option("-q", "--queue", default="disk-tree-m3", help="The ingest's Batch job queue")
@option("-r", "--region", default="us-east-1", help="The Batch / CloudWatch region")
@option("-R", "--r2-profile", default="m3", help="AWS profile holding the R2 keys (default m3)")
@option("-s", "--sqlite", default=None, help="Write to this local SQLite file (created from the migration) instead of D1")
@option("-w", "--save-inputs", default=None, help="Save the fetched inputs to this dir first (replay with -i)")
def main(aws_profile, bucket, profile_path, d1, endpoint, log_group, inputs, json_out, job_def, dry_run, summary, queue, region, r2_profile,
         sqlite, save_inputs) -> None:
    """Rebuild m3's past ingest runs from R2, CloudWatch and Batch (read-only), and upsert them as scan runs."""
    import time

    profile = load_profile(profile_path)
    inp = Inputs.load(Path(inputs)) if inputs else fetch(bucket, endpoint, r2_profile, aws_profile, region, queue, job_def, log_group)
    if save_inputs:
        inp.save(Path(save_inputs))
    sink = sink_for(dry_run, sqlite, d1)
    recs = runs(profile, inp)
    if json_out:
        Path(json_out).write_text(json.dumps([as_json(r) for r in recs], indent=1) + "\n")
    write_all(sink, recs, int(time.time()))
    if summary:
        for r in recs:
            err(f"{r.run.scan:16} {r.run.store:8} {r.run.status:9} {r.run.run_id:36} phases={len(r.phases)} outputs={len(r.outputs)}"
                + (f" cost=${r.run.cost_usd}" if r.run.cost_usd is not None else ""))
    err(f"scan-run backfill: {len(recs)} runs over {len({(r.run.store, r.run.scan) for r in recs})} scans, "
        f"{sum(len(r.phases) for r in recs)} phases, {sum(len(r.outputs) for r in recs)} outputs "
        f"({len(inp.jobs)} Batch jobs, {len(inp.logs)} task logs, {len(inp.ingests)} _INGEST.json)")


if __name__ == "__main__":
    main()
