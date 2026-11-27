"""A digest thread's reply style (its variant) per month: which one is live, which one an admin wants, and the switch
between them (specs/digest-thread-variant.md).

A month's thread keeps one converge state per variant (`digest.thread_states`). The live one has an OP and no
``retired_for`` (`digest.live_of`). The variant a run converges is, first match wins: the site's `digest_threads` row
(an admin's switch, `/admin/slack`), the live state's, the deployment default (`digest_settings.variant`), the config
file's (`desired_variant`). When the wanted variant isn't the live one, the converge switches (`converge_thread`): post
the new variant's replies into the same thread, mark the old state retiring, delete its replies (only those the
admin's reviewed dry run listed), mark it retired. Every step is resumable, and a failure leaves duplicates, never a
loss. A switch's dry run writes its plan (`switch_plan`), which the site stores with the row: its ``delete`` ts are
the only replies a switch may delete."""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import digest as dg

#: The site's thread rows (`GET /api/digest-threads`; `site/functions/api/digest-threads.ts`).
THREADS_PATH = "/api/digest-threads"
#: The switch plan's format (`switch_plan`; the site reads it back: `digest-threads.ts` `parseSwitchPlan`).
PLAN_VERSION = 1
#: A state's own bookkeeping, never carried to the variant a thread switches to.
SWITCH_KEYS = ("variant", "retired_for", "retired_at", "switching_from")
#: A state's replies: deleted by a switch (the OP, its split reply and the quota alert carry over).
REPLY_KEYS = ("posted", "extra", "stale", "provisional")


class SwitchRefused(RuntimeError):
    """A switch (or a retiring state's deletes) held back: a reply to delete isn't in the reviewed dry run's list, or
    the target variant's old state is still retiring. The run has converged the live variant; the caller exits 1."""


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def thread_row(cfg: dg.DigestConfig, channel: str, month: dt.date) -> dict | None:
    """The site's `digest_threads` row for (``channel``, ``month``): ``{variant, deletes: [ts…], plan_job, updated_by,
    updated_at}``, or None: no row, no `site_url` / `$SITE_TOKEN`, or the site unreachable (warned: the thread then keeps
    its live variant)."""
    from .deploy import site_token
    from .site import SiteError, get_json

    token = site_token()
    if not (cfg.site_url and token):
        return None
    try:
        doc = get_json(cfg.site_url, token, THREADS_PATH, {"channel": channel, "month": f"{month:%Y-%m}"})
    except SiteError as e:
        _err(f"digest: no thread row from {cfg.site_url}{THREADS_PATH} ({e}); the thread keeps its live variant")
        return None
    assert isinstance(doc, dict)
    return doc["thread"]


def desired_variant(*, override: str | None, row: dict | None, live: str | None, default: str | None, file: str) -> str:
    """The variant a run converges: ``override`` (`-V`, a candidate), else the admin's ``row``, else the ``live``
    thread's, else the deployment ``default`` (a new month), else the config ``file``'s."""
    return override or (row or {}).get("variant") or live or default or file


def replies(state: dict) -> list[tuple[str, str]]:
    """Every reply a state tracks, as (what, ts): ``posted`` and ``extra`` by key, the provisional ones, and ``stale``
    ones a redo or switch hasn't deleted yet. Not the OP, its split reply or the quota alert: a switch keeps those."""
    out = [(k, dg._ts(e)) for k, e in {**state.get("extra", {}), **state.get("posted", {})}.items()]
    out += [(f"provisional {k}", p["ts"]) for k, p in state.get("provisional", {}).items()]
    out += [("stale", ts) for ts in state.get("stale", [])]
    return out


def _headline(reply: dg.Reply, variant: str) -> str:
    return reply.username if variant == "sender" else reply.body.split("\n", 1)[0]


def switch_plan(tpl: dg.Template, root: str, month: dt.date, channel: str, states: dict[str, tuple[str, dict]], frm: str, to: str,
                now: dt.datetime | None = None) -> dict:
    """The switch of ``month``'s thread from ``frm`` (live) to ``to``, as its dry run reports it and the site stores it:
    the replies to post (``to``'s units a resumed switch hasn't posted yet) and the replies to delete (``frm``'s, by ts)."""
    data = tpl.load(root, month)
    if not data:
        raise SystemExit(f"digest: no scans for {month:%Y-%m}")
    new = states.get(to, ("", {}))[1]
    done = new.get("posted", {}) if new.get("switching_from") == frm else {}
    return {
        "digest_switch": PLAN_VERSION, "month": f"{month:%Y-%m}", "channel": channel, "from": frm, "to": to,
        "post": [{"key": u.key, "scan": u.scan, "headline": _headline(u.reply, to)} for u in tpl.units(data, to) if u.key not in done],
        "delete": [{"key": k, "ts": ts} for k, ts in replies(states[frm][1])],
        "at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"),
    }


def format_switch_plan(plan: dict) -> str:
    lines = [f"digest {plan['month']} in {plan['channel']}: switch {plan['from']} -> {plan['to']}; dry-run, nothing posted or deleted",
             f"  replies to post ({plan['to']}): {len(plan['post'])}"]
    lines += [f"    {p['key']}  {p['scan']}  {p['headline']!r}" for p in plan["post"]]
    lines.append(f"  replies to delete ({plan['from']}): {len(plan['delete'])}")
    lines += [f"    {d['key']}  ts={d['ts']}" for d in plan["delete"]]
    return "\n".join(lines)


def write_switch_plan(path: str, plan: dict) -> None:
    from disk_tree import blobfs

    if not blobfs.is_url(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    blobfs.write_text(path, json.dumps(plan, indent=2) + "\n")


def _retire(path: str, state: dict, reviewed: set[str], client) -> str | None:
    """Delete a retiring state's ``stale`` replies (only reviewed ones), then mark it retired. Returns why it was held
    back, or None."""
    unreviewed = [ts for ts in state.get("stale", []) if ts not in reviewed]
    if unreviewed:
        return f"{len(unreviewed)} of the retiring {state.get('variant')} state's replies aren't in the reviewed dry run ({', '.join(unreviewed[:5])})"
    dg._delete_stale(state, lambda ts: client.delete(ts, orphans_ok=True))
    if not state.get("stale"):
        state.pop("stale", None)
        state["retired_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    dg.save_state(path, state)
    return None if not state.get("stale") else f"{len(state['stale'])} of the retiring {state.get('variant')} state's replies not deleted yet (retried next run)"


def converge_thread(tpl: dg.Template, root: str, month: dt.date, client, channel: str, desired: str, *, row: dict | None,
                    current: bool = False, **kw) -> dict:
    """Converge ``month``'s thread to ``desired`` (`desired_variant`): today's converge when it's live (or there's no
    thread yet), else the switch. ``row`` is the admin's `digest_threads` row: its ``deletes`` are the only replies a
    switch, or a resumed retirement, deletes. Raises `SwitchRefused` after converging the live variant when it can't
    switch; ``kw`` passes to `digest.converge_slack`."""
    cfg = tpl.cfg
    states = dg.thread_states(cfg, root, month, channel, tpl.variants)
    reviewed = set((row or {}).get("deletes") or ())
    held = [why for v, (path, s) in states.items() if s.get("retired_for") and not s.get("retired_at")
            if (why := _retire(path, s, reviewed, client))]
    live = dg.live_of(states)
    if live is None or live == desired:
        state = dg.converge_slack(tpl, root, month, client, channel, desired, current=current, **kw)
        if held:
            raise SwitchRefused(f"digest: {month:%Y-%m}: " + "; ".join(held))
        return state
    old_path, old = states[live]
    target = states.get(desired, (None, {}))[1]
    if target.get("retired_for") and not target.get("retired_at"):
        held.append(f"the {desired} state is still retiring; it can't take the thread back until its deletes finish")
    if unreviewed := [ts for _, ts in replies(old) if ts not in reviewed]:
        held.append(f"switch to {desired} pending: {len(unreviewed)} of {live}'s replies aren't in the reviewed dry run "
                    f"({', '.join(unreviewed[:5])}); re-run it")
    if held:
        dg.converge_slack(tpl, root, month, client, channel, live, current=current, **kw)
        raise SwitchRefused(f"digest: {month:%Y-%m}: " + "; ".join(held))

    new_path = dg.state_path(root, month, cfg.state.format(channel=channel, variant=desired))
    if target.get("switching_from") != live:
        seed = {k: v for k, v in old.items() if k not in (*REPLY_KEYS, *SWITCH_KEYS)}
        dg.save_state(new_path, {**seed, "switching_from": live})
    _err(f"digest: switching {month:%Y-%m} from {live} to {desired}")
    new = dg.converge_slack(tpl, root, month, client, channel, desired, current=current, **kw)
    if not new:
        return new
    stale = [ts for _, ts in replies(old)]
    for k in REPLY_KEYS:
        old.pop(k, None)
    old.update(retired_for=desired, stale=stale)
    dg.save_state(old_path, old)
    new.pop("switching_from", None)
    dg.save_state(new_path, new)
    if why := _retire(old_path, old, reviewed, client):
        raise SwitchRefused(f"digest: {month:%Y-%m}: switched to {desired}, but {why}")
    _err(f"digest: switched {month:%Y-%m} from {live} to {desired} ({len(stale)} replies deleted)")
    return new


def lease_url(cfg: dg.DigestConfig, root: str, month: dt.date, channel: str) -> str:
    """The month's thread lease, beside its per-variant states: the state dir without its variant, `<YYYY-MM>.lease.json`."""
    state_dir = "/".join(p for p in cfg.state.format(channel=channel, variant="").split("/") if p)
    return dg.state_path(root, month, state_dir).removesuffix(".json") + ".lease.json"


@contextmanager
def thread_lease(cfg: dg.DigestConfig, root: str, month: dt.date, channel: str) -> Iterator[None]:
    """Hold ``month``'s thread lease (`append_runner.coordinator_lease` on its store's `LeaseFile`): a converge, a switch
    and a redo never run at once on one thread. Raises `append_runner.Busy` while another holds it (a holder that's an
    ended Batch job is taken over at once)."""
    from .append_runner import coordinator_lease, job_ended, lease_file

    with coordinator_lease(lease_file(lease_url(cfg, root, month, channel)), ended=job_ended()):
        yield

