"""A deployment's scan on the VM pilot's box (specs/vm-pilot.md "A scan on the box"): the deployment's own scan head
(`dt-cloud scan head`, its pinned image, its profile) as one local job (`job_backend.Local`) in the box's
`scan.slice`, every step a process of Nextflow's local executor inside that container, one task at a time.

The head runs as on Batch but for its executor: the deployment's profile (read from the image) is rewritten for the
box (`box_profile`): the local executor, Nextflow's launch and work dir on an existing disk the job mounts (so a
failed scan resumes from the same cache, and the run's reports stay on the box), every step's DuckDB memory and threads
capped at the box's budget, and the data store's bucket mounted where nf-google mounts it on Batch
(`$DT_DATA_MOUNT/<bucket>`: the local backend's gcsfuse volume, as the job's account). Everything else is the
deployment's: its run record and phases on /scans, the interval store and static-names appends, the R2 publish, the
digest, the run thread. The job runs as the deployment's VM account (`-a`), whose tokens the job's metadata server
mints (`job_metadata`).

`report` reads the run back per task: each traced task's wall time and peak RSS (Nextflow's trace) beside the maxima
of the job's cgroup samples while it ran (`job_usage`), with the head container's `memory.peak` and OOM kills.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from click import ClickException, IntRange, argument, group, option

from .job_backend import TOKEN_ENV, Local, existing_disks, sa_project

#: The head's Nextflow launch + work dir in its container: an existing disk (`NF_DEVICE`) the job mounts there.
BOX_WORK = "/mnt/disks/nf"
NF_DEVICE = "nf"
#: Where the data store's bucket is mounted in the head's container (`$DT_DATA_MOUNT/<bucket>`), as nf-google mounts the
#: work dir's bucket in a google-batch task.
DATA_MOUNT = "/mnt/disks"
#: The data bucket's gcsfuse options: nf-google's (`google.batch.gcsfuseOptions`), named for the box.
GCSFUSE_OPTIONS = ("-o rw", "-implicit-dirs", "--app-name disky-vm-pilot")
#: The budget's share kept for what isn't a step: the head's Nextflow (`NXF_OPTS`: 768 MiB of heap) and Python, and the
#: data bucket's gcsfuse (in the supervisor's unit, in the slice).
HEAD_RESERVE = 3 << 29  # 1.5 GiB
#: A step's DuckDB memory: this share of the rest (as `scan_steps.MEM_SHARE`).
MEM_SHARE = 0.6
NXF_OPTS = "-Xms128m -Xmx768m"
#: The head's secrets when it records its run: the D1 token (the run record) and the Slack bot (alerts, the run thread).
HEAD_SECRETS = ("CLOUDFLARE_API_TOKEN", "SLACK_BOT_TOKEN")
#: Options that make a step submit Batch jobs, which the box's job accounts can't (no Batch).
BATCH_OPTIONS = {"static-names": ("profile", "compact_cmd"), "interval-store": ("profile",), "vm-pack": ("batch",)}
BATCH_STEPS = ("access-jobs",)
#: The head's time limit (as Batch's head: `job/<deployment>-scan`'s `maxRunDuration`).
MAX_RUN_S = 21600
#: The head's line naming its scan (`scan_head.head`).
SCAN_LINE = re.compile(r"scan head: .*? scan (?P<scan>\S+) gen (?P<gen>\S+) session ")


def size_bytes(text: str) -> int:
    from .static_names import mem_bytes

    return mem_bytes(text)


def cap_size(value: str, cap: str) -> str:
    """`value` (a DuckDB size) when it is at most `cap`, else `cap`; a value that isn't a size (a `{mem}` template, which
    the step fills from `$DT_MEM`) as it is."""
    try:
        return value if size_bytes(value) <= size_bytes(cap) else cap
    except ValueError:
        return value


def budgets(budget: int) -> tuple[str, str]:
    """The task memory (`$DT_MEM`) and the DuckDB cap a `budget`-byte slice leaves the steps: the budget less
    `HEAD_RESERVE`, and `MEM_SHARE` of that (whole GB, at least 1)."""
    task = budget - HEAD_RESERVE
    if task < 1 << 30:
        raise ValueError(f"a {budget / (1 << 30):.1f} GiB budget leaves the steps under 1 GiB beside the head ({HEAD_RESERVE / (1 << 30):.1f} GiB)")
    return f"{task >> 20}MiB", f"{max(1, int(task * MEM_SHARE / (1 << 30)))}GB"


def box_profile(raw: dict, *, task_mem: str, mem: str, nproc: int, queue_size: int = 1, drop_secrets: tuple[str, ...] = (),
                skip: tuple[str, ...] = ()) -> dict:
    """The deployment's profile (`raw`, as its YAML loads) for the box:

    - `executor: {kind: local}`, `work` = `BOX_WORK`; `queue_size` tasks at a time (1: one task's memory at once, and
      each task's samples its own).
    - `machines`: only each label's `time` (a machine type, disk, CPU or memory request means nothing to the local
      executor, which refuses a task asking for more memory than the host has); sources keep their lister settings
      (`procs`, `threads` at most `nproc`) and lose their machines.
    - Every `options.<step>.mem` and `$DUCKDB_MEM` at most `mem`, every `threads` and `$DUCKDB_THREADS` at most `nproc`;
      `$DT_MEM` = `task_mem` (what a step sizes `{mem}` from); `$DT_DATA_MOUNT` = `DATA_MOUNT` (the data bucket's mount);
      `$GCP_PROJECT` the executor's project (the steps' `secrets exec` project); `$DT_LIVENESS_PID` empty, so each step
      logs its own liveness, as in a Batch task.
    - `drop_secrets`: these env names' bindings removed everywhere (the profile's, each step's, each source's): a
      secret the box's job account can't read would fail its step's `secrets exec`.
    - `skip`: these steps off (an extra step out of `steps`, a core one into `skip`).

    Refused (`ValueError`): a data store that isn't `gs://` (the box mounts a bucket), and a step left on whose options
    submit Batch jobs (`BATCH_OPTIONS`, `BATCH_STEPS`)."""
    p = copy.deepcopy(raw)
    ex = p.get("executor") or {}
    project = ex.get("project")
    if urlparse(str(p.get("data") or "")).scheme != "gs":
        raise ValueError(f"data {p.get('data')!r}: the box mounts a gs:// data store (its gcsfuse volume)")
    p["executor"] = {"kind": "local"}
    p["work"] = BOX_WORK
    p["queue_size"] = queue_size
    p["machines"] = {label: {"time": m["time"]} for label, m in (p.get("machines") or {}).items()
                     if isinstance(m, dict) and m.get("time")}
    for s in p.get("sources") or []:
        if not isinstance(s, dict):
            continue
        for k in ("machine", "disk", "fallback", "queue_timeout"):
            s.pop(k, None)
        for k in ("procs", "threads"):
            if isinstance(s.get(k), int):
                s[k] = min(s[k], nproc)
    env = dict(p.get("env") or {})
    if "DUCKDB_MEM" in env:
        env["DUCKDB_MEM"] = cap_size(str(env["DUCKDB_MEM"]), mem)
    if str(env.get("DUCKDB_THREADS", "")).isdigit():
        env["DUCKDB_THREADS"] = str(min(int(env["DUCKDB_THREADS"]), nproc))
    env.update(DT_DATA_MOUNT=DATA_MOUNT, DT_MEM=task_mem, DT_LIVENESS_PID="")
    if project:
        env.setdefault("GCP_PROJECT", project)
    p["env"] = env
    for o in (p.get("options") or {}).values():
        if not isinstance(o, dict):
            continue
        if isinstance(o.get("mem"), str):
            o["mem"] = cap_size(o["mem"], mem)
        if isinstance(o.get("threads"), int):
            o["threads"] = min(o["threads"], nproc)
    drop = set(drop_secrets)
    if drop:
        p["secrets"] = {k: v for k, v in (p.get("secrets") or {}).items() if k not in drop}
        for holder in [*(p.get("options") or {}).values(), *(s for s in p.get("sources") or [] if isinstance(s, dict))]:
            if isinstance(holder, dict) and isinstance(holder.get("secrets"), dict):
                holder["secrets"] = {k: v for k, v in holder["secrets"].items() if k not in drop}
                if not holder["secrets"]:
                    del holder["secrets"]
    steps, skipped = list(p.get("steps") or []), list(p.get("skip") or [])
    for s in skip:
        if s in steps:
            steps.remove(s)
        elif s not in skipped:
            skipped.append(s)
    for k, v in (("steps", steps), ("skip", skipped)):
        if v or k in p:
            p[k] = v
    from .scan_profile import parse

    on = set(parse(p, where="box profile").enabled())
    batch = [f"options.{n}.{k}" for n, ks in BATCH_OPTIONS.items() if n in on for k in ks if k in (p.get("options") or {}).get(n, {})]
    batch += [f"steps: {n}" for n in BATCH_STEPS if n in on]
    if batch:
        raise ValueError(f"the box runs no Batch jobs (its job accounts have no Batch): {', '.join(batch)} submit"
                         " them; skip the step (-S) or run this scan on Batch")
    return p


def head_spec(profile: dict, *, image: str, account: str, project: str, job_id: str, profile_path: str, disk: str,
              env: dict[str, str]) -> dict:
    """The head's local job: one container of `image` running `dt-cloud scan head <profile_path>`, as `account`, with
    the data bucket as a gcsfuse volume at `$DT_DATA_MOUNT/<bucket>` and the existing disk `disk` at `BOX_WORK`; the
    head's secrets (`HEAD_SECRETS`, from `project`) when its profile records the run; `env` over the head's own."""
    tenant = (profile.get("labels") or {}).get("deployment") or profile["deployment"]
    labels = {"app": "disky", "deployment": tenant, "component": "scan"}
    bucket = urlparse(profile["data"]).netloc
    at = f"{DATA_MOUNT}/{bucket}"
    records = "SCAN_RUNS_PROFILE" in (profile.get("env") or {})
    names = profile.get("secrets") or {}
    secrets = {k: f"projects/{project}/secrets/{names[k]}/versions/latest" for k in HEAD_SECRETS if records and k in names}
    variables = {
        "DISKY_LABELS": ",".join(f"{k}={v}" for k, v in labels.items()),
        "DT_SCAN_IMAGE": image,
        "DT_RUN_ID": job_id,
        "DT_NF_DIR": "/app/job/nf",
        "NXF_OPTS": NXF_OPTS,
        **env,
    }
    return {
        "labels": labels,
        "allocationPolicy": {
            "labels": labels,
            "serviceAccount": {"email": account},
            "instances": [{"policy": {"disks": [{"deviceName": NF_DEVICE, "existingDisk": disk}]}}],
        },
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {
                "imageUri": image,
                "entrypoint": "dt-cloud",
                "commands": ["scan", "head", profile_path],
                "volumes": [f"{at}:{at}:rw", f"{BOX_WORK}:{BOX_WORK}:rw"],
            }}],
            "volumes": [
                {"gcs": {"remotePath": bucket}, "mountPath": at, "mountOptions": list(GCSFUSE_OPTIONS)},
                {"deviceName": NF_DEVICE, "mountPath": BOX_WORK},
            ],
            "environment": {"variables": variables, **({"secretVariables": secrets} if secrets else {})},
            "maxRunDuration": f"{MAX_RUN_S}s",
        }}],
    }


def slice_budget(slice_: str, root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """The slice's `memory.max` in bytes; None when it has none (`max`) or can't be read."""
    try:
        v = (root / slice_ / "memory.max").read_text().strip()
    except OSError:
        return None
    return int(v) if v.isdigit() else None


def image_file(jobs: Local, image: str, path: str, account: str) -> str:
    """`path`'s text in `image`, pulled as `account` (the engine's registry credential helper reads the minted token)
    and read by a container with no network."""
    token = jobs.identity.access_token(account, 900)
    return jobs.host.capture([jobs.engine, "run", "--rm", "--network", "none", "--entrypoint", "cat", image, path],
                             env={TOKEN_ENV: token})


def job_id_for(tenant: str, now: datetime) -> str:
    return f"{tenant}-box-{now:%Y%m%d-%H%M%S}"


def submit(jobs: Local, image: str, profile_in_image: str, *, account: str, budget: int, nproc: int, queue_size: int = 1,
           mem: str | None = None, drop_secrets: tuple[str, ...] = (), skip: tuple[str, ...] = (),
           env: dict[str, str] | None = None, dry_run: bool = False, now: datetime | None = None) -> dict:
    """Read the deployment's profile from `image`, rewrite it for the box (`box_profile`), write it (and `box.json`, the
    run's budgets) to the head's disk, and submit the head (`head_spec`) as a local job. Returns what it did."""
    import yaml

    from .scan_dag import build
    from .scan_profile import parse

    if not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError(f"image {image!r}: pin it by digest (the deployment's scan image, as its scheduler runs it)")
    project = sa_project(account)
    if not project:
        raise ValueError(f"account {account!r}: a service account (<name>@<project>.iam.gserviceaccount.com)")
    if mem:
        size_bytes(mem)  # a DuckDB size, or ValueError
    raw = yaml.safe_load(image_file(jobs, image, profile_in_image, account))
    task_mem, cap = budgets(budget)
    profile = box_profile(raw, task_mem=task_mem, mem=mem or cap, nproc=nproc, queue_size=queue_size,
                          drop_secrets=drop_secrets, skip=skip)
    tenant = (profile.get("labels") or {}).get("deployment") or profile["deployment"]
    job_id = job_id_for(tenant, now or datetime.now(timezone.utc))
    disk = f"{tenant}-{NF_DEVICE}"
    in_container = f"{BOX_WORK}/profiles/{job_id}.yml"
    spec = head_spec(profile, image=image, account=account, project=(raw.get("executor") or {}).get("project") or project,
                     job_id=job_id, profile_path=in_container, disk=disk, env=env or {})
    dag = [n.name for n in build(parse(profile, where="box profile")).nodes]
    host_dir = jobs.kept_root / disk
    doc = {"job": job_id, "image": image, "account": account, "profile": str(host_dir / "profiles" / f"{job_id}.yml"),
           "work": str(host_dir), "budget": budget, "task_mem": task_mem, "mem": mem or cap, "queue_size": queue_size,
           "dag": dag}
    if dry_run:
        return {**doc, "box_profile": profile, "spec": spec}
    (host_dir / "profiles").mkdir(parents=True, exist_ok=True)
    (host_dir / "profiles" / f"{job_id}.yml").write_text(yaml.safe_dump(profile, sort_keys=False))
    (host_dir / "profiles" / f"{job_id}.box.json").write_text(json.dumps(doc, indent=1) + "\n")
    jobs.submit(spec, job_id)
    return doc


def report(jobs: Local, job_id: str) -> dict:
    """The run's per-task usage (`job_usage.per_task`) from the head's trace (`<work>/runs/<scan>/trace.tsv`, the head
    publishes it at its end) and the job's samples, with each container run's summary; written beside the trace as
    `usage.json` and `usage.tsv`."""
    import csv

    from .job_usage import per_task, read_runs, read_timeline, table

    d = jobs.dir(job_id)
    job = jobs.job(job_id)
    disk = existing_disks(json.loads((d / "spec.json").read_text())).get(NF_DEVICE)
    work = next((Path(k) for k in job.get("keep", []) if Path(k).name == disk), None)
    if work is None:
        raise ValueError(f"local job {job_id}: no {NF_DEVICE} disk: not a box scan's head")
    log = (d / "log").read_text(errors="replace") if (d / "log").exists() else ""
    m = SCAN_LINE.search(log)
    if not m:
        raise ValueError(f"local job {job_id}: its log names no scan (the head hasn't started)")
    scan = m["scan"]
    trace = next((t for t in (work / "runs" / scan / "trace.tsv", work / "trace.tsv") if t.exists()), None)
    rows = []
    if trace:
        with open(trace, newline="") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
    timeline = read_timeline(d / "usage.tsv") if (d / "usage.tsv").exists() else []
    runs = read_runs(d / "usage.jsonl")
    box = work / "profiles" / f"{job_id}.box.json"
    budget = json.loads(box.read_text()).get("budget") if box.exists() else None
    tasks = per_task(rows, timeline)
    out = work / "runs" / scan
    out.mkdir(parents=True, exist_ok=True)
    doc = {"job": job_id, "scan": scan, "gen": m["gen"], "budget": budget, "trace": str(trace) if trace else None,
           "runs": runs, "tasks": tasks}
    (out / "usage.json").write_text(json.dumps(doc, indent=1) + "\n")
    (out / "usage.tsv").write_text(table(tasks, runs, budget))
    return doc


# ── the CLI: `dt-cloud scan box …` ─────────────────────────────────────────


@group("box")
def cli() -> None:
    """A deployment's scan on this host, the VM pilot's box (specs/vm-pilot.md "A scan on the box")."""


@cli.command("submit")
@option("-a", "--account", required=True, help="The job's service account: the deployment's VM account (the box may mint only its tenants')")
@option("-b", "--trigger-by", help="Who started the run (`$DT_TRIGGER_BY`, recorded on /scans; the run is `manual`)")
@option("-B", "--budget", help="The scan budget the steps size to (a size, e.g. 8GiB); default the slice's `memory.max`")
@option("-g", "--gen", help="Resume this generation of scan `-s` (both, or neither: a new scan stamped at start)")
@option("-i", "--image", required=True, help="The deployment's scan image, pinned by digest (what its scheduler runs)")
@option("-l", "--machine", default="vm-pilot", show_default=True, help="The run's machine fact on /scans (`$DT_RUN_MACHINE`; not a priced type: the box's cost is fixed)")
@option("-m", "--mem", help="Every step's DuckDB memory cap (default: 60% of the budget less the head's 1.5 GiB)")
@option("-n", "--dry-run", is_flag=True, help="Print the box profile, the head's spec and the DAG; write and submit nothing (still pulls the image)")
@option("-q", "--queue-size", type=IntRange(1), default=1, show_default=True, help="Tasks at a time (1: each task's memory its own)")
@option("-r", "--region", help="The run's region fact on /scans (`$DT_RUN_REGION`)")
@option("-s", "--scan", help="Resume this scan (with -g): its Nextflow cache on the head's disk, each step skipping what it finished")
@option("-S", "--skip", "skip", multiple=True, help="A step to leave out of this run (repeatable), e.g. one needing what the job account lacks")
@option("-x", "--drop-secret", "drop_secrets", multiple=True, help="An env name whose secret bindings to drop (repeatable): one the job account can't read")
@argument("profile")
def submit_cmd(account: str, trigger_by: str | None, budget: str | None, gen: str | None, image: str, machine: str,
               mem: str | None, dry_run: bool, queue_size: int, region: str | None, scan: str | None, skip: tuple[str, ...],
               drop_secrets: tuple[str, ...], profile: str) -> None:
    """Run the scan PROFILE (its path in the image, e.g. /app/job/profiles/r2.yml) describes on this host: its head as
    a local job in `$DISKY_LOCAL_SLICE`, every step in that container, one at a time. Prints the job id, the profile
    written, the head's disk and the DAG; `dt-cloud local-job status|logs|cancel JOB` follow it, `dt-cloud scan box
    report JOB` reads its per-task usage once it has ended."""
    from .scan_runs import TRIGGER_BY_RE

    if bool(scan) != bool(gen):
        raise ClickException("-s/--scan and -g/--gen go together (a resume names both)")
    if trigger_by and not TRIGGER_BY_RE.fullmatch(trigger_by):
        raise ClickException("-b/--trigger-by: an address or handle (`scan_runs.TRIGGER_BY_RE`)")
    jobs = Local.from_env()
    size = size_bytes(budget) if budget else slice_budget(jobs.slice)
    if not size:
        raise ClickException(f"no budget: {jobs.slice} has no memory.max here; pass -B")
    env = {"DT_TRIGGER": "manual", "DT_RUN_MACHINE": machine,
           **({"DT_TRIGGER_BY": trigger_by} if trigger_by else {}), **({"DT_RUN_REGION": region} if region else {}),
           **({"SNAP_ID": scan, "GEN": gen} if scan and gen else {})}
    try:
        doc = submit(jobs, image, profile, account=account, budget=size, nproc=os.cpu_count() or 1, queue_size=queue_size,
                     mem=mem, drop_secrets=drop_secrets, skip=skip, env=env, dry_run=dry_run)
    except ValueError as e:
        raise ClickException(str(e)) from None
    print(json.dumps(doc, indent=1))
    if not dry_run:
        print(f"follow: dt-cloud local-job status {doc['job']} · dt-cloud local-job logs {doc['job']} · "
              f"after: dt-cloud scan box report {doc['job']}", file=sys.stderr)


@cli.command("report")
@argument("job_id")
def report_cmd(job_id: str) -> None:
    """JOB_ID's per-task wall time and memory (the head's trace, the cgroup's samples while each task ran), and its
    container run's `memory.peak` and OOM kills; also written beside the run's trace (`usage.json`, `usage.tsv`)."""
    from .job_usage import table

    try:
        doc = report(Local.from_env(), job_id)
    except ValueError as e:
        raise ClickException(str(e)) from None
    sys.stdout.write(table(doc["tasks"], doc["runs"], doc["budget"]))
