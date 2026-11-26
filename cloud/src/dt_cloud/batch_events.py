"""`dt-cloud batch-events serve`: a scan head's Batch state changes → its run record and its Slack thread, as they happen.

A scan updates its run record and its scan thread (`nominal`) itself, at its start and its end. A head that never
reaches its end (cancelled from /scans or the console, a lost VM, a delete) can't: its record stays `running` and its
reply ⏳ until something else looks. This is that something, driven by Batch itself: the head's job spec names a
Pub/Sub topic in `notifications` (`JOB_STATE_CHANGED`), a push subscription delivers each change to a Cloud Run
service running `serve` (infra `BatchEvents`), and on a terminal state it

1. reconciles the profile store's `running` rows against Batch (`scan_runs.reconcile`, no quiet period: the event says
   the job is over), which records a head that ended without recording its end as failed (`cancelled` when cancelled),
   and on CANCELLED marks the job's run `cancelled` when its head's exit trap already recorded the cancel's SIGTERM
   as a failure (`scan_runs.batch_cancelled`); a run one of whose steps had failed on its own before the cancel stays
   `failed` either way (`scan_runs.failed_before`),
2. converges the scan thread (`nominal.run`): the reply, the thread head, its pin and the canvas.

Both are idempotent and the thread's writers serialize on its lease, so the delivery after a head that did finish
(the job already wrote both) changes nothing, and a redelivery is harmless.

A static-names compaction's jobs do too (the same `events_topic`, every job `alert`): a failure or cancel alerts.
An interval-store compaction's jobs publish to the same topic when the profile names it (`events_topic`,
`interval_append.with_events`), labelled `events` (`append_runner.EVENTS_LABEL`); their ends go to `compaction`
instead: the job that publishes the new generation's pointer (`catch-up`), on success, submits the catch-up append as
its own Batch job (`catch_up_spec`: the compaction's image, account, machine and env, running `interval-store append
-c -l`; its id the compaction's + `-catch-up`, so a redelivery meets it, 409, and starts no second); any compaction job
failing or cancelled posts an alert (`disk-tree notify`'s channels). No clock: the scans deferred meanwhile go in when
the compaction is done, not at the next scan. The service runs as the scan job's own
account (D1, the site token, the thread's state bucket, Batch reads), with the job's env (`SCAN_RUNS_PROFILE`,
`D1_DB_ID`, the Cloudflare and Slack tokens).

`serve` answers 204 when the change needs nothing (not terminal) or both steps ran, 500 when one raised (Pub/Sub
redelivers, backing off), 400 for a body that isn't a push envelope."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from click import group, option

if TYPE_CHECKING:
    from .scan_runs import Profile, Sink

err = partial(print, file=sys.stderr, flush=True)

#: The states after which a job runs nothing more.
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

#: What a terminal change does: given the message's attributes (`JobUID`, `JobName`, `NewJobState`, …).
Act = Callable[[Mapping[str, str]], None]


def envelope(body: bytes) -> tuple[dict[str, str], bytes, str | None]:
    """A push delivery's message attributes, data and id. Raises `ValueError` on anything else."""
    try:
        msg = json.loads(body)["message"]
        data = base64.b64decode(msg.get("data") or "", validate=True)
    except (KeyError, TypeError, binascii.Error) as e:
        raise ValueError(f"not a Pub/Sub push envelope: {e}") from e
    attrs = msg.get("attributes") or {}
    if not isinstance(attrs, dict):
        raise ValueError("attributes isn't an object")
    return {str(k): str(v) for k, v in attrs.items()}, data, msg.get("messageId") or msg.get("message_id")


def handle(body: bytes, act: Act) -> int:
    """One push delivery → an HTTP status (see the module doc)."""
    try:
        attrs, data, mid = envelope(body)
    except ValueError as e:
        err(f"batch-events: {e}")
        return 400
    state = attrs.get("NewJobState")
    what = f"message {mid}: {attrs.get('JobName') or attrs.get('JobUID') or '?'} {attrs.get('Type') or '?'} → {state or '?'}"
    if attrs.get("Type") != "JOB_STATE_CHANGED" or state not in TERMINAL:
        err(f"batch-events: {what}: nothing to do")
        return 204
    try:
        act(attrs)
    except Exception as e:  # noqa: BLE001 — logged and redelivered
        err(f"batch-events: {what}: {type(e).__name__}: {e}")
        return 500
    err(f"batch-events: {what}: reconciled, thread converged")
    return 204


#: A notification's `JobName`: `projects/<project>/locations/<region>/jobs/<id>`.
JOB_NAME_RE = re.compile(r"projects/(?P<project>[^/]+)/locations/(?P<region>[^/]+)/jobs/(?P<id>[^/]+)")
#: The catch-up's command (`interval_append.append_cmd`): every published scan the store doesn't hold yet, in order.
CATCH_UP_CMD = "dt-cloud interval-store append -c -l"
CATCH_UP_SUFFIX = "-catch-up"


def job_ref(attrs: Mapping[str, str]) -> tuple[str, str, str] | None:
    """A notification's job as (project, region, id), from its `JobName`; None when it doesn't name one."""
    m = JOB_NAME_RE.fullmatch(attrs.get("JobName") or "")
    return (m["project"], m["region"], m["id"]) if m else None


def catch_up_id(job_id: str) -> str:
    """The catch-up's Batch id for compaction job `job_id`: one per compaction, at most 63 characters."""
    return job_id[:63 - len(CATCH_UP_SUFFIX)].rstrip("-") + CATCH_UP_SUFFIX


def catch_up_spec(job: Mapping) -> dict | None:
    """The catch-up append's Batch job, from the compaction job `job` (Batch's own record of it): its image (also the
    stages', `$INTERVAL_STORE_IMAGE`), account, machine, mounts and env (secrets included; its own run facts, `DT_JOB_*`,
    dropped), one task, no retry, running `CATCH_UP_CMD`; labelled as it was (cost labels included) but `stage`
    `catch-up` and no `events` (its end asks nothing), and no `notifications`. None when the job names no
    `$INTERVAL_STORE_PROFILE` to append with."""
    from .append_runner import EVENTS_LABEL
    from .interval_append import ENV, PROFILE_ENV
    from .maint_runs import FACT_ENV

    ts = deepcopy(job["taskGroups"][0]["taskSpec"])
    c = ts["runnables"][0]["container"]
    env = ts.get("environment") or {}
    variables = {k: v for k, v in (env.get("variables") or {}).items() if k not in FACT_ENV.values()}
    if not variables.get(PROFILE_ENV):
        return None
    variables[ENV["image"]] = c["imageUri"]
    ts["runnables"] = [{"container": {"imageUri": c["imageUri"], "entrypoint": "bash", "commands": ["-c", CATCH_UP_CMD],
                                      "volumes": list(c.get("volumes") or [])}}]
    ts["environment"] = {**env, "variables": variables}
    ts["maxRetryCount"] = 0
    labels = {k: v for k, v in (job.get("labels") or {}).items() if k != EVENTS_LABEL} | {"stage": "catch-up"}
    return {"taskGroups": [{"taskCount": 1, "parallelism": 1, "taskSpec": ts}], "allocationPolicy": deepcopy(job["allocationPolicy"]),
            "labels": labels, "logsPolicy": deepcopy(job.get("logsPolicy") or {"destination": "CLOUD_LOGGING"})}


#: A static-names compaction job's `component` label (`static_runner.StaticRunner.job`)
STATIC_COMPONENT = "static-names"


def compaction(attrs: Mapping[str, str], job: Mapping | None, submit: Callable[[dict, str, str, str], str],
               alert: Callable[[str, str], None]) -> str | None:
    """A terminal change of an interval-store or static-names compaction job (labels `component` `interval-store` /
    `static-names`, `events` `catch-up` / `alert`): what it did (see the module doc), or None for any other job (a scan
    head's: `converge`). `submit(spec, id, region, project)` creates a Batch job (a 409 counts as created);
    `alert(level, message)` posts."""
    from .append_runner import ALERT, CATCH_UP, EVENTS_LABEL
    from .interval_append import COMPONENT

    labels = (job or {}).get("labels") or {}
    event = labels.get(EVENTS_LABEL)
    ref = job_ref(attrs)
    component = labels.get("component")
    if job is None or ref is None or component not in (COMPONENT, STATIC_COMPONENT) or event not in (CATCH_UP, ALERT):
        return None
    project, region, jid = ref
    state = attrs.get("NewJobState")
    gen = labels.get("gen") or "?"
    if state != "SUCCEEDED":
        if component == STATIC_COMPONENT:
            # cw's static-names compactions ran out of memory twice a day for days (2026-11-22..), unseen: their runs piled up
            alert("fail", f"static-names compaction job {jid} (generation {gen}) ended {state}: the generation isn't compacted. "
                          "Its runs keep piling up (each scan's anchors and drill read them all); while the compaction is due, "
                          "the next scan's append submits it again.")
        else:
            alert("fail", f"interval-store compaction job {jid} (generation {gen}) ended {state}: the generation isn't compacted. "
                          "Appends wait on the compaction lease until it lapses; while the compaction is due, the next append submits it again.")
        return f"compaction {jid} {state}: alerted"
    if component == STATIC_COMPONENT:
        return f"compaction {jid} SUCCEEDED: nothing to do"
    if event == ALERT:
        return f"compaction {jid} SUCCEEDED: a step before the pointer's, nothing to do"
    spec = catch_up_spec(job)
    if spec is None:
        alert("warn", f"interval-store compaction {jid} (generation {gen}) published, but its job names no $INTERVAL_STORE_PROFILE: "
                      "no catch-up submitted; the scans it deferred go in with the next scan's append")
        return f"compaction {jid} SUCCEEDED: no profile, alerted"
    name = submit(spec, catch_up_id(jid), region, project)
    return f"compaction {jid} SUCCEEDED: catch-up {name} submitted"


def post(env: Mapping[str, str], level: str, message: str) -> None:
    """`message` (a compaction's, naming its store) through `disk-tree notify`'s channels in `env` (`fail`: high
    priority), or stderr without one."""
    from disk_tree.notify import push

    title = f"{'❌' if level == 'fail' else '⚠️'} compaction"
    chans = push.configured(env)
    if not chans:
        err(f"batch-events: alert (no channel configured): {title}: {message}")
        return
    for c, e in push.send(push.Notice(title, message, priority=1 if level == "fail" else 0), chans, env=env, post=push.http_post):
        err(f"batch-events: alert: {c}: {e or 'sent'}")


def submit(spec: dict, job_id: str, region: str, project: str) -> str:
    """`batch.submit_job`, a 409 counting as submitted (a redelivery meets the job it already created)."""
    from .batch import submit_job

    return submit_job(spec, job_id, region=region, project=project, exists_ok=True)


def scan_ended(attrs: Mapping[str, str], sink: Sink, profile: Profile, lookup: Callable[[str, str], Mapping | None], now: int) -> None:
    """A terminal change of any job but a compaction's (a scan head's): the profile store's `running` rows reconciled
    against Batch (`lookup(job, region)`; no quiet period, the event says the job is over), then, on CANCELLED, the job's
    run its head's exit trap recorded failed by the cancel's SIGTERM marked `cancelled` (`scan_runs.batch_cancelled`).
    Either leaves `failed` a run one of whose steps had already failed on its own (`scan_runs.failed_before`)."""
    from .scan_runs import batch_cancelled, reconcile

    for r in reconcile(sink, profile, now, lookup, 0):
        err(f"batch-events: {attrs.get('JobUID')}: recorded {r.run_id} ({r.scan}) {r.status}: {r.error}")
    if attrs.get("NewJobState") == "CANCELLED" and (ref := job_ref(attrs)) is not None:
        batch_cancelled(sink, profile, ref[2], ref[1], now)


def converge(env: Mapping[str, str]) -> Act:
    """The terminal-state action: an interval-store compaction's (`compaction`), else a scan head's (`scan_ended`),
    then converge its scan thread (if it has one)."""

    def act(attrs: Mapping[str, str]) -> None:
        from .nominal import run
        from .scan_runs import batch_lookup, load_profile, sink_for

        if (ref := job_ref(attrs)) is not None:
            did = compaction(attrs, batch_lookup(ref[0], ref[2], ref[1]), submit, partial(post, env))
            if did is not None:
                err(f"batch-events: {did}")
                return
        profile = load_profile(None)
        if not profile.project:
            raise ValueError("the profile names no project")
        sink = sink_for(False, None, None)
        scan_ended(attrs, sink, profile, partial(batch_lookup, profile.project), int(time.time()))
        if profile.nominal:
            err(run(profile.nominal, env, profile.project, execute=True, sink=sink))  # and each reply's link (`thread_url`)

    return act


def server(port: int, act: Act, host: str = "") -> ThreadingHTTPServer:
    """An HTTP server answering each POST with `handle` (any path: the push endpoint is the service's root)."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(handle(body, act))
            self.end_headers()

        def log_message(self, fmt: str, *args) -> None:
            err(f"batch-events: {self.address_string()} {fmt % args}")

    return ThreadingHTTPServer((host, port), Handler)


@group("batch-events")
def cli() -> None:
    """A scan head's Batch state changes (a Pub/Sub push) → its run record and its scan thread."""


@cli.command("serve")
@option("-p", "--port", type=int, default=None, help="Port to listen on (default `$PORT`, as Cloud Run sets it, else 8080)")
def serve_cmd(port: int | None) -> None:
    """Answer a Pub/Sub push subscription's deliveries of Batch `JOB_STATE_CHANGED` notifications. The caller is
    authenticated by Cloud Run (the push's OIDC token, `run.invoker`), not here."""
    port = port or int(os.environ.get("PORT") or 8080)
    err(f"batch-events: listening on :{port}")
    server(port, converge(os.environ)).serve_forever()
