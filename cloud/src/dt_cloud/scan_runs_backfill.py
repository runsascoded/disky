"""`dt-cloud scan-run backfill`: reconstruct past scan runs (specs/scan-runs-ui.md §Backfill) from what outlives a run.

- The Batch API: every job the profile's kinds match — its uid (the run id), state, start/end, machine, SPOT, tasks.
- Cloud Logging (`log_days`, 30 by default — the retention): the task log's phase markers (each phase's end is its
  log line's timestamp), the done / NOP markers, the ERR trap's failure line.
- The data bucket: the outputs the profile names, per scan; an index generation goes to the run whose window holds
  its stamp, every other output to the scan's latest succeeded run of the kinds that write it.

Pure over its inputs (`reconstruct`), so the tests feed fakes; `fetch_jobs` / `fetch_logs` are the read-only API
calls. Writing is idempotent: the same upserts the job's own `record` makes, `source = 'backfill'` unless the job
wrote the run first.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace

from .scan_id import is_scan_id
from .scan_runs import ALL, JobKind, ObjectStore, Output, Phase, Profile, Record, Run, _epoch, job_fields, measure_after

GEN_STAMP = re.compile(r"(\d{8}T\d{6}Z)")
BATCH_STATES = {"SUCCEEDED": "succeeded", "FAILED": "failed", "CANCELLED": "failed", "RUNNING": "running",
                "SCHEDULED": "running", "QUEUED": "running"}


def _commands(job: Mapping) -> tuple[str, str, str, Mapping[str, str]]:
    ts = (job.get("taskGroups") or [{}])[0].get("taskSpec", {})
    c = (ts.get("runnables") or [{}])[0].get("container", {})
    env = ts.get("environment", {}).get("variables", {}) or {}
    return c.get("imageUri", ""), c.get("entrypoint", ""), " ".join(c.get("commands", ())), env


def matches(kind: JobKind, job: Mapping) -> bool:
    image, entrypoint, commands, env = _commands(job)
    name = job["name"].rsplit("/", 1)[-1]
    fields_ = {"name": name, "image": image, "entrypoint": entrypoint, "commands": commands}
    for k, v in kind.where.items():
        if k in fields_:
            if not re.search(str(v), fields_[k]):
                return False
        elif k == "env":
            if not all(e in env for e in v):
                return False
        elif k == "no_env":
            if any(e in env for e in v):
                return False
        else:
            raise ValueError(f"scan-run profile: job kind {kind.kind}: unknown `where` key {k!r}")
    return True


def classify(profile: Profile, job: Mapping) -> JobKind | None:
    """The first job kind matching `job` (None: not a scan run)."""
    return next((k for k in profile.jobs if matches(k, job)), None)


def task_lines(entries: Sequence[Mapping]) -> dict[str, list[tuple[int, str]]]:
    """Log entries grouped by job uid, in time order, as `(epoch s, text)`."""
    out: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for e in entries:
        uid = (e.get("labels") or {}).get("job_uid")
        text = e.get("textPayload")
        if uid and text:
            out[uid].append((e["timestamp"], _epoch(e["timestamp"]), text.rstrip("\n")))
    return {u: [(t, x) for _, t, x in sorted(v)] for u, v in out.items()}


def scan_of(kind: JobKind, job: Mapping, lines: Sequence[tuple[int, str]], started: int | None) -> str | None:
    """The run's scan id from the kind's sources, in order (a source yielding a non-id is skipped)."""
    _, _, commands, env = _commands(job)
    for src in kind.scan:
        v = None
        if "env" in src:
            v = env.get(src["env"])
        elif "commands" in src:
            m = re.search(src["commands"], commands)
            v = m.group(1) if m else None
        elif "log" in src:
            v = next((m.group(1) for _, x in lines if (m := re.search(src["log"], x))), None)
        elif "started" in src and started:
            v = dt.datetime.fromtimestamp(started, dt.timezone.utc).strftime(src["started"])
        elif "created" in src:
            v = dt.datetime.fromtimestamp(_epoch(job["createTime"]), dt.timezone.utc).strftime(src["created"])
        if v and is_scan_id(v):
            return v
    return None


def phases_of(profile: Profile, lines: Sequence[tuple[int, str]], started: int | None) -> list[Phase]:
    """The phase markers, in order: each ends at its line's time and starts where the previous ended (an overlapped
    phase at the run's start; a concurrent one where the last serial phase ended). A repeated name keeps its last
    marker."""
    rx = re.compile(profile.phase_marker)
    out: dict[str, Phase] = {}
    prev = serial = started
    for t, x in lines:
        m = rx.search(x)
        if not m or m.group("name") in profile.phase_ignore:
            continue
        name = m.group("name")
        note = m.groupdict().get("note")
        begin = started if name in profile.overlapped else serial if name in profile.concurrent else prev
        seq = out[name].seq if name in out else len(out)
        out[name] = Phase(name, seq, begin, t, "done", note)
        if name not in profile.overlapped:
            prev = t
            if name not in profile.concurrent:
                serial = t
    return sorted(out.values(), key=lambda p: p.seq)


def failure_of(profile: Profile, job: Mapping, lines: Sequence[tuple[int, str]]) -> str | None:
    if profile.error_marker:
        rx = re.compile(profile.error_marker)
        for _, x in lines:
            if m := rx.search(x):
                g = m.groupdict()
                return f"exit {g.get('rc')}: {g.get('cmd', '').strip()}" + (f" (line {g['line']})" if g.get("line") else "")
    for ev in reversed(job.get("status", {}).get("statusEvents", ())):
        d = ev.get("description", "")
        if " to CANCELLED" in d:
            return "Batch: cancelled"
        if " to FAILED" in d:
            code = m.group(1) if (m := re.search(r"exit code (\d+)", d)) else None
            if "maximum runtime" in d:
                return f"Batch: over the maximum runtime (exit {code})"
            return f"Batch: task failed (exit {code})" if code else "Batch: failed"
    return None


def run_of(profile: Profile, kind: JobKind, job: Mapping, lines: Sequence[tuple[int, str]], now: int) -> Record | None:
    jf = job_fields(profile, job, now)
    scan = scan_of(kind, job, lines, jf["started_ts"])
    state = job.get("status", {}).get("state", "")
    status = BATCH_STATES.get(state)
    if not scan or not status:
        return None
    if status == "succeeded" and profile.nop_marker and any(re.search(profile.nop_marker, x) for _, x in lines):
        status = "nop"
    run = Run(run_id=job["uid"], scan=scan, kind=kind.kind, status=status, source="backfill", store=profile.store, **jf)
    if status == "failed":
        run = replace(run, error=failure_of(profile, job, lines))
    return Record(run, tuple(phases_of(profile, lines, jf["started_ts"])))


def gens_under(profile: Profile, scan: str, store: ObjectStore) -> list[str]:
    """The index generations the scan has: the `{gen}` segments under the first gen-templated output's parent."""
    for spec in profile.outputs:
        if "{gen}" in spec.uri:
            parent = spec.uri.split("{gen}", 1)[0].replace("{scan}", scan)
            gens = {u[len(parent):].split("/", 1)[0] for u, _ in store.list(parent) if "/" in u[len(parent):]}
            return sorted(gens)
    return []


def owner_of_gen(gen: str, runs: Sequence[Run]) -> Run | None:
    """The run whose window holds the generation's stamp (a job stamps its gen at start: allow 10 min before the
    task's start); else None."""
    m = GEN_STAMP.search(gen)
    if not m:
        return None
    t = int(dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).timestamp())
    for r in runs:
        if r.started_ts and r.started_ts - 600 <= t <= (r.finished_ts or 2**62):
            return r
    return None


def reconstruct(
    profile: Profile,
    jobs: Sequence[Mapping],
    log_entries: Sequence[Mapping],
    store: ObjectStore | None,
    now: int,
    since: str | None = None,
    workers: int = 8,
) -> list[Record]:
    """Every scan run the jobs + logs + store can rebuild, oldest first (`since`: scans ≥ that id only)."""
    lines = task_lines(log_entries)
    recs: list[Record] = []
    for job in jobs:
        kind = classify(profile, job)
        if not kind:
            continue
        rec = run_of(profile, kind, job, lines.get(job.get("uid", ""), []), now)
        if rec and (since is None or rec.run.scan >= since):
            recs.append(rec)
    by_scan: dict[str, list[Record]] = defaultdict(list)
    for r in recs:
        by_scan[r.run.scan].append(r)
    def one(scan: str) -> list[Record]:
        group = sorted(by_scan[scan], key=lambda r: (r.run.started_ts or 0, r.run.run_id))
        heads = [r.run for r in group if not _downstream(profile, r.run.kind)]
        group = [_link(profile, r, heads) for r in group]
        return _attach_outputs(profile, scan, group, store) if store is not None else group

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max(1, workers)) as ex:  # the store reads are I/O bound
        out = [r for g in ex.map(one, sorted(by_scan)) for r in g]
    return sorted(out, key=lambda r: (r.run.started_ts or 0, r.run.run_id))


def _downstream(profile: Profile, kind: str) -> bool:
    k = profile.kind(kind)
    return bool(k and k.downstream)


def _link(profile: Profile, rec: Record, heads: Sequence[Run]) -> Record:
    """A downstream run's parent: the orchestrating run of its scan whose window holds its start (else the latest)."""
    if not _downstream(profile, rec.run.kind) or not heads:
        return rec
    t = rec.run.started_ts or 0
    inside = [h for h in heads if h.started_ts and h.started_ts - 120 <= t <= (h.finished_ts or 2**62)]
    parent = (inside or [h for h in heads if (h.started_ts or 0) <= t] or list(heads))[-1]
    return replace(rec, run=replace(rec.run, parent=parent.run_id))


def _attach_outputs(profile: Profile, scan: str, group: list[Record], store: ObjectStore) -> list[Record]:
    runs = [r.run for r in group]
    gens = gens_under(profile, scan, store)
    heads = [r for r in runs if r.status in ("succeeded", "running") and not _downstream(profile, r.kind)]
    owners = {g: owner_of_gen(g, heads) for g in gens}
    outs: dict[str, list[Output]] = defaultdict(list)
    gen_of: dict[str, str] = {}
    for g, r in owners.items():
        if r is not None:
            gen_of[r.run_id] = g  # a run writes one generation; the latest stamp wins
    for spec in profile.outputs:
        if "{gen}" in spec.uri:
            for run_id, g in gen_of.items():
                kind = next(r.kind for r in runs if r.run_id == run_id)
                if spec.kinds and kind not in spec.kinds:
                    continue
                outs[run_id] += measure_after(replace(profile, outputs=(spec,)), ALL, kind, scan, g, store)
            continue
        writers = [r for r in runs if r.status == "succeeded" and not _downstream(profile, r.kind)
                   and (not spec.kinds or r.kind in spec.kinds)]
        if writers:
            w = max(writers, key=lambda r: (r.finished_ts or 0, r.run_id))
            outs[w.run_id] += measure_after(replace(profile, outputs=(spec,)), ALL, w.kind, scan, None, store)
    return [replace(r, run=replace(r.run, gen=gen_of.get(r.run.run_id, r.run.gen)), outputs=tuple(outs.get(r.run.run_id, ())))
            for r in group]


# ── The read-only API calls ─────────────────────────────────────────────────


def fetch_jobs(project: str, regions: Sequence[str]) -> list[dict]:
    """Every Batch job in the regions (paginated), newest first."""
    from .gcp import _get

    jobs: list[dict] = []
    for region in regions:
        tok = None
        while True:
            params = {"pageSize": 500}
            if tok:
                params["pageToken"] = tok
            d = _get(f"https://batch.googleapis.com/v1/projects/{project}/locations/{region}/jobs", **params)
            jobs += d.get("jobs", [])
            tok = d.get("nextPageToken")
            if not tok:
                break
    return sorted(jobs, key=lambda j: j.get("createTime", ""), reverse=True)


def log_filter(profile: Profile, since: dt.datetime) -> str:
    """The task-log lines the backfill reads: a server-side prefilter on each marker's literal head (the phase, NOP
    and error markers and the kinds' `log` scan sources); the markers' full regexes then run here."""
    rxs = [profile.phase_marker] + [r for r in (profile.nop_marker, profile.error_marker) if r]
    rxs += [s["log"] for k in profile.jobs for s in k.scan if "log" in s]
    heads = list(dict.fromkeys(_head(r) for r in rxs))
    alt = " OR ".join(f'textPayload=~"{h}"' for h in heads)
    return f'log_id("batch_task_logs") AND timestamp >= "{since.strftime("%Y-%m-%dT%H:%M:%SZ")}" AND ({alt})'


def _head(rx: str) -> str:
    """A regex's literal head as an RE2 filter literal: `^PHASE (?P<name>…` → `^PHASE `, `^\\+ fail_alert (…` →
    `^\\\\+ fail_alert ` (RE2-escaped, then escaped for the filter's string literal)."""
    anchored = rx.startswith("^")
    i, lit = (1 if anchored else 0), []
    while i < len(rx):
        c = rx[i]
        if c == "\\" and i + 1 < len(rx) and not rx[i + 1].isalnum():
            lit.append(rx[i + 1])
            i += 2
            continue
        if c in "()[]{}.*+?|$\\":
            break
        lit.append(c)
        i += 1
    if lit and rx[i:i + 1] in ("?", "*", "{"):  # the last char was optional: not part of the head
        lit.pop()
    body = "".join("\\" + ch if ch in "()[]{}.*+?|$^\\" else ch for ch in lit)
    return (("^" if anchored else "") + body).replace("\\", "\\\\").replace('"', '\\"')


def fetch_logs(profile: Profile, project: str, now: int, limit: int = 200_000) -> list[dict]:
    from .gcp import log_entries

    since = dt.datetime.fromtimestamp(now, dt.timezone.utc) - dt.timedelta(days=profile.log_days)
    return log_entries(log_filter(profile, since), project, limit=limit, asc=True)
