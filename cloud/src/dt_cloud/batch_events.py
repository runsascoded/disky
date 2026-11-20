"""`dt-cloud batch-events serve`: a scan head's Batch state changes → its run record and its Slack thread, as they happen.

A scan updates its run record and its scan thread (`nominal`) itself, at its start and its end. A head that never
reaches its end (cancelled from /scans or the console, a lost VM, a delete) can't: its record stays `running` and its
reply ⏳ until something else looks. This is that something, driven by Batch itself: the head's job spec names a
Pub/Sub topic in `notifications` (`JOB_STATE_CHANGED`), a push subscription delivers each change to a Cloud Run
service running `serve` (infra `BatchEvents`), and on a terminal state it

1. reconciles the profile store's `running` rows against Batch (`scan_runs.reconcile`, no quiet period: the event says
   the job is over), which records a head that ended without recording its end as failed (`cancelled` when cancelled),
2. converges the scan thread (`nominal.run`): the reply, the thread head, its pin and the canvas.

Both are idempotent and the thread's writers serialize on its lease, so the delivery after a head that did finish
(the job already wrote both) changes nothing, and a redelivery is harmless. The service runs as the scan job's own
account (D1, the site token, the thread's state bucket, Batch reads), with the job's env (`SCAN_RUNS_PROFILE`,
`D1_DB_ID`, the Cloudflare and Slack tokens).

`serve` answers 204 when the change needs nothing (not terminal) or both steps ran, 500 when one raised (Pub/Sub
redelivers, backing off), 400 for a body that isn't a push envelope."""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from click import group, option

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


def converge(env: Mapping[str, str]) -> Act:
    """The terminal-state action: reconcile the profile store's runs, then converge its scan thread (if it has one)."""

    def act(attrs: Mapping[str, str]) -> None:
        from .nominal import run
        from .scan_runs import batch_lookup, load_profile, reconcile, sink_for

        profile = load_profile(None)
        if not profile.project:
            raise ValueError("the profile names no project")
        sink = sink_for(False, None, None)
        lost = reconcile(sink, profile, int(time.time()), partial(batch_lookup, profile.project), 0)
        for r in lost:
            err(f"batch-events: {attrs.get('JobUID')}: recorded {r.run_id} ({r.scan}) failed: {r.error}")
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
