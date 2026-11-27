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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _sender(reply: dg.Reply) -> tuple[str | None, str | None, str | None]:
    """A reply's sender as Slack fixes it at post time (`chat.update` can't change it): its name and icon overrides."""
    return reply.username, reply.icon_url, reply.icon_emoji


@dataclass(frozen=True)
class Replies:
    """What a switch does with the thread's replies: ``edit`` (an old reply whose sender matches a new unit's, edited in
    place: its key, its ts, the unit), ``post`` (the units left over) and ``delete`` (the old replies left over: key, ts)."""
    edit: list[tuple[str, str, Any]]
    post: list[Any]
    delete: list[tuple[str, str]]


def plan_replies(tpl: dg.Template, data: Any, old: dict, frm: str, to: str, done: dict) -> Replies:
    """Pair ``to``'s units with ``frm``'s posted replies in thread order where their senders match (edited in place,
    kept where they are), the units after the first that pairs with none posted, the old replies paired with none
    deleted. ``done``: the replies a resumed switch already made (``to``'s state's ``posted``): neither posted again
    nor edited again, and their old ts never deleted."""
    reused = {dg._ts(e) for e in done.values()}
    old_units = {u.key: u for u in tpl.units(data, frm)}
    units = [u for u in tpl.units(data, to) if u.key not in done]
    posted = [(k, dg._ts(e)) for k, e in old.get("posted", {}).items() if dg._ts(e) not in reused]
    edit: list[tuple[str, str, Any]] = []
    i = 0
    for u in units:
        j = next((j for j in range(i, len(posted)) if posted[j][0] in old_units and _sender(old_units[posted[j][0]].reply) == _sender(u.reply)), None)
        if j is None:
            break
        edit.append((*posted[j], u))
        i = j + 1
    kept = {ts for _, ts, _ in edit}
    return Replies(edit, units[len(edit):], [(k, ts) for k, ts in replies(old) if ts not in kept and ts not in reused])


def _human(m: dict) -> bool:
    """A message a person posted (no bot id; a plain message or one also sent to the channel)."""
    return not m.get("bot_id") and m.get("subtype") in (None, "thread_broadcast")


def scan_thread(client, channel: str, op_ts: str, rs: Replies) -> tuple[list[dict], dict[str, dict]]:
    """The thread read once (`conversations.replies`): the people's replies the switch would move above its new ones
    (after the first reply it deletes, when it posts any: Slack only appends), ``{ts, user}`` each; and each reply it
    deletes' reactions, own replies and files, which the delete loses."""
    msgs = client.list_thread_raw(channel, op_ts)
    gone = {ts for _, ts in rs.delete}
    first = next((i for i, m in enumerate(msgs) if m.get("ts") in gone), None)
    moved = [{"ts": m["ts"], "user": m.get("user")} for m in msgs[first + 1:] if _human(m)] if rs.post and first is not None else []
    lost = {m["ts"]: {"reactions": [{"name": r["name"], "count": r.get("count", 1)} for r in m.get("reactions", [])],
                      "replies": m.get("reply_count", 0), "files": len(m.get("files", []))} for m in msgs if m.get("ts") in gone}
    return moved, lost


def link_search(token: str | None):
    """`search.messages` (a user token with `search:read`: a bot token can't search) as ``query -> [match]``, or None
    without a token: then a plan says its links weren't checked."""
    if not token:
        return None
    from thrds.slack import SlackClient

    c = SlackClient(token, "")

    def search(query: str) -> list[dict]:
        return c._request("search.messages", {"query": query, "count": 20}, method="GET").get("messages", {}).get("matches", [])
    return search


def switch_plan(tpl: dg.Template, root: str, month: dt.date, channel: str, states: dict[str, tuple[str, dict]], frm: str, to: str,
                client=None, search=None, now: dt.datetime | None = None) -> dict:
    """The switch of ``month``'s thread from ``frm`` (live) to ``to``, as its dry run reports it and the site stores it:
    the replies edited in place, posted and deleted (`plan_replies`); with ``client``, the people's replies it would move
    (``reorder``) and what each deleted reply loses (reactions, its own replies, files); with ``search``
    (`link_search`), the messages linking to each (``links``; None: not checked)."""
    data = tpl.load(root, month)
    if not data:
        raise SystemExit(f"digest: no scans for {month:%Y-%m}")
    old = states[frm][1]
    new = states.get(to, ("", {}))[1]
    rs = plan_replies(tpl, data, old, frm, to, new.get("posted", {}) if new.get("switching_from") == frm else {})
    old_units = {u.key: u for u in tpl.units(data, frm)}
    moved, lost = scan_thread(client, channel, old["op_ts"], rs) if client is not None and old.get("op_ts") else ([], {})

    def links(ts: str) -> list[dict] | None:
        if search is None:
            return None
        return [{"channel": (m.get("channel") or {}).get("name") or (m.get("channel") or {}).get("id"), "author": m.get("username") or m.get("user"),
                 "permalink": m.get("permalink")} for m in search(f"p{ts.replace('.', '')}")]

    notes = []
    if not rs.edit and rs.post and old.get("posted"):
        notes.append(f"no reply is edited in place: no {to} reply's sender matches a {frm} reply's (Slack can't change a message's sender)")
    return {
        "digest_switch": PLAN_VERSION, "month": f"{month:%Y-%m}", "channel": channel, "from": frm, "to": to,
        "edit": [{"key": k, "ts": ts, "to": u.key, "scan": u.scan, "was": _headline(old_units[k].reply, frm), "headline": _headline(u.reply, to)}
                 for k, ts, u in rs.edit],
        "post": [{"key": u.key, "scan": u.scan, "headline": _headline(u.reply, to)} for u in rs.post],
        "delete": [{"key": k, "ts": ts, **lost.get(ts, {}), "links": links(ts)} for k, ts in rs.delete],
        "thread_read": client is not None,
        "links_checked": search is not None,
        "reorder": moved,
        "notes": notes,
        "at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"),
    }


def _lost(d: dict) -> str:
    bits = [" ".join(f":{r['name']}: {r['count']}" for r in d.get("reactions", []))] if d.get("reactions") else []
    bits += [f"{d['replies']} replies of its own"] if d.get("replies") else []
    bits += [f"{d['files']} file(s)"] if d.get("files") else []
    bits += ["linked from " + ", ".join(f"#{h['channel']} by {h['author']}" for h in d["links"])] if d.get("links") else []
    return f"  ({'; '.join(bits)})" if bits else ""


def format_switch_plan(plan: dict) -> str:
    lines = [f"digest {plan['month']} in {plan['channel']}: switch {plan['from']} -> {plan['to']}; dry-run, nothing posted, edited or deleted"]
    lines += [f"  note: {n}" for n in plan["notes"]]
    lines.append(f"  replies to edit in place: {len(plan['edit'])}")
    lines += [f"    {e['key']}  ts={e['ts']}  {e['was']!r} -> {e['to']} {e['headline']!r}" for e in plan["edit"]]
    lines.append(f"  replies to post ({plan['to']}): {len(plan['post'])}")
    lines += [f"    {p['key']}  {p['scan']}  {p['headline']!r}" for p in plan["post"]]
    lines.append(f"  replies to delete ({plan['from']}): {len(plan['delete'])}")
    lines += [f"    {d['key']}  ts={d['ts']}{_lost(d)}" for d in plan["delete"]]
    if not plan["thread_read"]:
        lines.append("  thread not read (no Slack token): people's replies it would move, and what deleted replies lose, not checked")
    if plan["delete"] and not plan["links_checked"]:
        lines.append("  links not checked (no SLACK_SEARCH_TOKEN): a link elsewhere to a deleted reply would break")
    if plan["reorder"]:
        lines.append(f"  held unless reorder is allowed: {len(plan['reorder'])} people's replies would end up above the new replies")
        lines += [f"    ts={r['ts']} by {r['user']}" for r in plan["reorder"]]
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
    thread yet), else the switch (`plan_replies`): pairs edited in place, the rest posted, then the old replies left
    over deleted. ``row`` is the admin's `digest_threads` row: its ``deletes`` and ``edits`` (the reviewed plan's) are
    the only old replies a switch, or a resumed retirement, touches, and its ``reorder`` the only people's replies it
    may move above its new ones. Raises `SwitchRefused` after converging the live variant when it can't switch; ``kw``
    passes to `digest.converge_slack`."""
    cfg = tpl.cfg
    states = dg.thread_states(cfg, root, month, channel, tpl.variants)
    reviewed = set((row or {}).get("deletes") or ()) | set((row or {}).get("edits") or ())
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
    resumed = target.get("switching_from") == live
    data = tpl.load(root, month)
    rs = plan_replies(tpl, data, old, live, desired, target.get("posted", {}) if resumed else {}) if data else None
    if rs and not held:
        moved, _ = scan_thread(client, channel, old["op_ts"], rs)
        if unmoved := [m for m in moved if m["ts"] not in set((row or {}).get("reorder") or ())]:
            held.append(f"switch to {desired} pending: {len(unmoved)} people's replies would end up above its new replies "
                        "(" + ", ".join(f"{m['ts']} by {m['user']}" for m in unmoved[:5]) + ") and the switch doesn't allow it; preview it again")
    if held:
        dg.converge_slack(tpl, root, month, client, channel, live, current=current, **kw)
        raise SwitchRefused(f"digest: {month:%Y-%m}: " + "; ".join(held))

    new_path = dg.state_path(root, month, cfg.state.format(channel=channel, variant=desired))
    if not resumed:
        target = {**{k: v for k, v in old.items() if k not in (*REPLY_KEYS, *SWITCH_KEYS)}, "switching_from": live}
        dg.save_state(new_path, target)
    _err(f"digest: switching {month:%Y-%m} from {live} to {desired}")
    if rs:
        posted = target.setdefault("posted", {})
        for _, ts, u in rs.edit:
            client.edit(ts, u.reply.body)
            posted[u.key] = dg._record(tpl, ts, u.scan, desired)
            dg.save_state(new_path, target)
            _err(f"digest: edited reply {ts} in place -> {u.key} ({u.scan})")
    new = dg.converge_slack(tpl, root, month, client, channel, desired, current=current, **kw)
    if not new:
        return new
    kept = {dg._ts(e) for e in new.get("posted", {}).values()}
    stale = [ts for _, ts in replies(old) if ts not in kept]
    for k in REPLY_KEYS:
        old.pop(k, None)
    old.update(retired_for=desired, stale=stale)
    dg.save_state(old_path, old)
    new.pop("switching_from", None)
    dg.save_state(new_path, new)
    if why := _retire(old_path, old, reviewed, client):
        raise SwitchRefused(f"digest: {month:%Y-%m}: switched to {desired}, but {why}")
    _err(f"digest: switched {month:%Y-%m} from {live} to {desired} ({len(rs.edit) if rs else 0} edited in place, {len(stale)} deleted)")
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

