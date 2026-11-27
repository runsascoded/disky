"""A digest thread's reply style per month (`digest_switch`, specs/digest-thread-variant.md): which variant is live, which
one a run converges, and the switch between them (post, retiring, reviewed deletes, retired), resumable at each step."""
import json
from datetime import date
from pathlib import Path

import pytest

from thrds.slack import SlackClient

from dt_cloud import digest as E
from dt_cloud import digest_switch as S

from conftest import thrds_fake
from digest_examples import config_file
from test_digest_quota import CFG, LEAD, SEP, SEPT, _FakeSlack, _publish, _tpl

OCT = date(2026, 10, 1)
WEEK = "wk-2026-08-31"
WEEK_HEAD = ":arrow_deg40: *week:* **+10.0 TiB (1.4%)**"


@thrds_fake(SlackClient)
class _ThreadSlack(_FakeSlack):
    """`_FakeSlack` that keeps the thread as Slack would (`conversations.replies`): the OP, then each reply posted
    into it in order, the bot's (``bot_id``) and the people's (`_say`), less those deleted; `_react` adds reactions."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.thread: list[dict] = []

    def post(self, content, thread_id=None, *, username=None, icon_url=None, icon_emoji=None, broadcast=False):
        m = super().post(content, thread_id, username=username, icon_url=icon_url, icon_emoji=icon_emoji, broadcast=broadcast)
        self.thread.append({"ts": m.id, "bot_id": "B1"})
        return m

    def delete(self, message_id, orphans_ok=False):
        super().delete(message_id, orphans_ok=orphans_ok)
        self.thread = [m for m in self.thread if m["ts"] != message_id]

    def list_thread_raw(self, channel, thread_ts):
        return list(self.thread)

    def _say(self, ts: str, user: str) -> None:
        self.thread.append({"ts": ts, "user": user})

    def _react(self, ts: str, name: str, count: int) -> None:
        next(m for m in self.thread if m["ts"] == ts).setdefault("reactions", []).append({"name": name, "count": count, "users": []})


def _sender_thread(tmp_path: Path, **fake_kw):
    """September converged as `sender`: OP m1, replies m2 (9/1) and m3 (9/2)."""
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT)
    fake = _ThreadSlack(**fake_kw)
    E.converge_slack(_tpl(), str(root), SEP, fake, "C1", "sender")
    assert [c[0] for c in fake.calls] == ["post", "post", "post"]
    fake.calls.clear()
    return root, fake


def _state(tmp_path: Path, variant: str) -> dict:
    return json.loads((tmp_path / "digest" / "cw" / "C1" / variant / "2026-09.json").read_text())


def _calls(fake) -> list[tuple]:
    """Each call as (kind, the message it touches: the edited / deleted ts, a post's thread)."""
    return [(c[0], c[2] if c[0] == "post" else c[1]) for c in fake.calls]


def _row(variant: str, *deletes: str, edits: tuple[str, ...] = (), reorder: tuple[str, ...] = ()) -> dict:
    return {"variant": variant, "deletes": list(deletes), "edits": list(edits), "reorder": list(reorder), "plan_job": "cw-digest-20260927-120000z",
            "updated_by": "admin@example.org", "updated_at": 1}


def _at(state: dict) -> dict:
    return {**state, "retired_at": "<t>"} if "retired_at" in state else state


def test_desired_variant_precedence():
    d = S.desired_variant
    assert d(override=None, row=None, live=None, default=None, file="sender") == "sender"
    assert d(override=None, row=None, live=None, default="weekly", file="sender") == "weekly"
    # an existing thread keeps its live variant over the default and the file
    assert d(override=None, row=None, live="sender", default="weekly", file="weekly") == "sender"
    # an admin's row switches it; -V overrides everything (a candidate)
    assert d(override=None, row=_row("weekly"), live="sender", default=None, file="sender") == "weekly"
    assert d(override="body", row=_row("weekly"), live="sender", default="weekly", file="weekly") == "body"


def test_switch_posts_then_retires(tmp_path: Path):
    root, fake = _sender_thread(tmp_path)
    plot = _state(tmp_path, "sender")["plot_name"]
    S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    # the OP edited, the week's reply posted into the same thread, THEN the old replies deleted
    assert _calls(fake) == [("edit", "m1"), ("post", "m1"), ("delete", "m2"), ("delete", "m3")]
    assert fake.calls[1][3:6] == ("wk of 8/31", None, ":calendar:")
    assert _state(tmp_path, "weekly") == {"plot_name": plot, "op_ts": "m1", "variant": "weekly",
                                          "posted": {WEEK: {"ts": "m4", "scan": "2026-09-02T1200"}}}
    assert _at(_state(tmp_path, "sender")) == {"plot_name": plot, "variant": "sender", "op_ts": "m1", "retired_for": "weekly", "retired_at": "<t>"}
    assert E.live_of(E.thread_states(CFG, str(root), SEP, "C1", _tpl().variants)) == "weekly"
    # converged again: a plain weekly converge, nothing deleted
    fake.calls.clear()
    S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    assert _calls(fake) == [("edit", "m1")]


def test_an_unreviewed_reply_holds_the_switch(tmp_path: Path):
    # the dry run listed only m2 (m3 posted after it): the run converges sender as usual, posts no weekly reply, deletes
    # nothing, and says why
    root, fake = _sender_thread(tmp_path)
    with pytest.raises(S.SwitchRefused) as e:
        S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2"))
    assert str(e.value) == "digest: 2026-09: switch to weekly pending: 1 of sender's replies aren't in the reviewed dry run (m3); re-run it"
    assert _calls(fake) == [("edit", "m1")]
    assert not (tmp_path / "digest" / "cw" / "C1" / "weekly" / "2026-09.json").exists()
    # no row at all (the site unreachable, or -V for real by hand): the same
    with pytest.raises(S.SwitchRefused):
        S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=None)


def test_a_failed_post_is_finished_next_run(tmp_path: Path):
    # the weekly reply's post fails (the 4th post): sender stays live, nothing deleted; the next run finishes the switch
    root, fake = _sender_thread(tmp_path, fail_post_at=4)
    with pytest.raises(RuntimeError, match="slack down"):
        S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    assert _calls(fake) == [("edit", "m1")]
    weekly = _state(tmp_path, "weekly")
    assert (weekly["switching_from"], weekly.get("posted")) == ("sender", None)
    assert E.live_of(E.thread_states(CFG, str(root), SEP, "C1", _tpl().variants)) == "sender"
    fake.calls.clear()
    S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    assert _calls(fake) == [("edit", "m1"), ("post", "m1"), ("delete", "m2"), ("delete", "m3")]
    assert _state(tmp_path, "weekly")["posted"] == {WEEK: {"ts": "m5", "scan": "2026-09-02T1200"}}
    assert "switching_from" not in _state(tmp_path, "weekly")


def test_a_failed_delete_is_resumed(tmp_path: Path):
    root, fake = _sender_thread(tmp_path, fail_delete={"m2"})
    with pytest.raises(S.SwitchRefused) as e:
        S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    assert str(e.value) == "digest: 2026-09: switched to weekly, but 1 of the retiring sender state's replies not deleted yet (retried next run)"
    sender = _state(tmp_path, "sender")
    assert (sender["retired_for"], sender["stale"], "retired_at" in sender) == ("weekly", ["m2"], False)
    assert E.live_of(E.thread_states(CFG, str(root), SEP, "C1", _tpl().variants)) == "weekly"
    fake.fail_delete = set()
    fake.calls.clear()
    S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    assert _calls(fake) == [("delete", "m2"), ("edit", "m1")]
    assert _at(_state(tmp_path, "sender"))["retired_at"] == "<t>"
    assert "stale" not in _state(tmp_path, "sender")


def test_a_resumed_delete_must_be_reviewed(tmp_path: Path):
    root, fake = _sender_thread(tmp_path, fail_delete={"m2"})
    with pytest.raises(S.SwitchRefused):
        S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    fake.fail_delete = set()
    fake.calls.clear()
    with pytest.raises(S.SwitchRefused) as e:
        S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m3"))
    assert str(e.value) == "digest: 2026-09: 1 of the retiring sender state's replies aren't in the reviewed dry run (m2)"
    assert _calls(fake) == [("edit", "m1")]


def test_switch_back(tmp_path: Path):
    root, fake = _sender_thread(tmp_path)
    S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    fake.calls.clear()
    S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "sender", row=_row("sender", "m4"))
    assert _calls(fake) == [("edit", "m1"), ("post", "m1"), ("post", "m1"), ("delete", "m4")]
    assert _state(tmp_path, "sender")["posted"] == {"2026-09-01": {"ts": "m5", "scan": "2026-09-01T1200"},
                                                    "2026-09-02": {"ts": "m6", "scan": "2026-09-02T1200"}}
    assert "retired_for" not in _state(tmp_path, "sender")
    assert _at(_state(tmp_path, "weekly"))["retired_for"] == "sender"
    assert E.live_of(E.thread_states(CFG, str(root), SEP, "C1", _tpl().variants)) == "sender"


def test_two_live_states_are_refused(tmp_path: Path):
    root, _ = _sender_thread(tmp_path)
    weekly = tmp_path / "digest" / "cw" / "C1" / "weekly" / "2026-09.json"
    weekly.parent.mkdir(parents=True)
    weekly.write_text(json.dumps({"op_ts": "m1", "variant": "weekly", "posted": {}}))
    with pytest.raises(E.ThreadConflict) as e:
        E.live_of(E.thread_states(CFG, str(root), SEP, "C1", _tpl().variants))
    base = tmp_path / "digest" / "cw" / "C1"
    assert str(e.value) == (f"digest: sender ({base}/sender/2026-09.json) and weekly ({base}/weekly/2026-09.json) are both live; "
                            "retire one by hand (`retired_for`) before converging")


def test_the_next_month_links_to_a_switched_thread(tmp_path: Path):
    root, fake = _sender_thread(tmp_path)
    S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    assert E._prev_op(CFG, str(root), OCT, "C1", "sender", _tpl().variants) == "m1"
    assert E._prev_op(CFG, str(root), OCT, "C1", "sender") is None


def test_switch_plan(tmp_path: Path):
    root, fake = _sender_thread(tmp_path)
    fake._react("m2", "eyes", 2)
    states = E.thread_states(CFG, str(root), SEP, "C1", _tpl().variants)
    plan = S.switch_plan(_tpl(), str(root), SEP, "C1", states, "sender", "weekly", client=fake)
    assert {**plan, "at": "<t>"} == {
        "digest_switch": 1, "month": "2026-09", "channel": "C1", "from": "sender", "to": "weekly",
        "edit": [],
        "post": [{"key": WEEK, "scan": "2026-09-02T1200", "headline": WEEK_HEAD}],
        "delete": [{"key": "2026-09-01", "ts": "m2", "reactions": [{"name": "eyes", "count": 2}], "replies": 0, "files": 0, "links": None},
                   {"key": "2026-09-02", "ts": "m3", "reactions": [], "replies": 0, "files": 0, "links": None}],
        "thread_read": True, "links_checked": False, "reorder": [],
        "notes": ["no reply is edited in place: no weekly reply's sender matches a sender reply's (Slack can't change a message's sender)"],
        "at": "<t>",
    }
    # no search token: the links aren't checked, and the plan says so
    assert S.format_switch_plan(plan).split("\n") == [
        "digest 2026-09 in C1: switch sender -> weekly; dry-run, nothing posted, edited or deleted",
        "  note: no reply is edited in place: no weekly reply's sender matches a sender reply's (Slack can't change a message's sender)",
        "  replies to edit in place: 0",
        "  replies to post (weekly): 1",
        f"    {WEEK}  2026-09-02T1200  {WEEK_HEAD!r}",
        "  replies to delete (sender): 2",
        "    2026-09-01  ts=m2  (:eyes: 2)",
        "    2026-09-02  ts=m3",
        "  links not checked (no SLACK_SEARCH_TOKEN): a link elsewhere to a deleted reply would break",
    ]
    # a search token: each deleted reply's permalink id searched, its hits listed
    queries: list[str] = []

    def search(q: str) -> list[dict]:
        queries.append(q)
        return [{"channel": {"id": "C9", "name": "general"}, "username": "ann", "permalink": "https://x/p1"}] if q == "pm2" else []

    plan = S.switch_plan(_tpl(), str(root), SEP, "C1", states, "sender", "weekly", client=fake, search=search)
    assert queries == ["pm2", "pm3"]
    assert S.format_switch_plan(plan).split("\n")[6:] == [
        "    2026-09-01  ts=m2  (:eyes: 2; linked from #general by ann)",
        "    2026-09-02  ts=m3",
    ]
    # no Slack token: the thread isn't read either
    plan = S.switch_plan(_tpl(), str(root), SEP, "C1", states, "sender", "weekly")
    assert S.format_switch_plan(plan).split("\n")[-2:] == [
        "  thread not read (no Slack token): people's replies it would move, and what deleted replies lose, not checked",
        "  links not checked (no SLACK_SEARCH_TOKEN): a link elsewhere to a deleted reply would break",
    ]


def _units(*senders: str, prefix: str = "u") -> list:
    return [E.Unit(f"{prefix}{i}", f"s{i}", E.Reply(sender, f"body {prefix}{i}")) for i, sender in enumerate(senders, 1)]


class _Units:
    """A template whose two variants' units are given: ``a`` (the old replies' units) and ``b`` (the new)."""

    def __init__(self, a: list, b: list):
        self.by = {"a": a, "b": b}

    def units(self, data, variant):
        return self.by[variant]


def _pair(old: list, new: list) -> tuple[list, list, list]:
    state = {"posted": {u.key: {"ts": f"t{u.key}", "scan": u.scan} for u in old}}
    rs = S.plan_replies(_Units(old, new), None, state, "a", "b", {})
    return [(k, u.key) for k, _, u in rs.edit], [u.key for u in rs.post], [k for k, _ in rs.delete]


def test_pairing_edits_matching_senders_in_thread_order():
    # matching senders pair in order; the first new unit that pairs with none, and every one after it, is posted (Slack
    # appends: a post can't land between kept replies); old replies paired with none are deleted
    assert _pair(_units("S", "S", "X"), _units("S", "S", "S", "Y", prefix="n")) == ([("u1", "n1"), ("u2", "n2")], ["n3", "n4"], ["u3"])
    # nothing matches: every new reply posted, every old one deleted
    assert _pair(_units("S", "S"), _units("W", prefix="n")) == ([], ["n1"], ["u1", "u2"])
    # fewer new than old: the old left over deleted
    assert _pair(_units("S", "S"), _units("S", prefix="n")) == ([("u1", "n1")], [], ["u2"])
    # more new than old: the new left over posted
    assert _pair(_units("S"), _units("S", "S", prefix="n")) == ([("u1", "n1")], ["n2"], [])
    # an old reply skipped on the way to a match is deleted
    assert _pair(_units("X", "S"), _units("S", prefix="n")) == ([("u2", "n1")], [], ["u1"])


class _SameSender:
    """The real template, but its `body` units carry the `sender` units' senders (their keys and scans, a new body): a
    switch that can edit every reply in place."""

    def __init__(self, tpl):
        self._t = tpl

    def __getattr__(self, name):
        return getattr(self._t, name)

    def units(self, data, variant, platform="slack"):
        if variant != "body":
            return self._t.units(data, variant, platform)
        return [E.Unit(u.key, u.scan, E.Reply(u.reply.username, f"body: {u.reply.body}", u.reply.icon_url, u.reply.icon_emoji))
                for u in self._t.units(data, "sender", platform)]


def test_a_switch_edits_matching_replies_in_place(tmp_path: Path):
    root, fake = _sender_thread(tmp_path)
    S.converge_thread(_SameSender(_tpl()), str(root), SEP, fake, "C1", "body", row=_row("body", edits=("m2", "m3")))
    # both replies edited where they are, nothing posted or deleted, the OP refreshed
    assert _calls(fake) == [("edit", "m2"), ("edit", "m3"), ("edit", "m1")]
    assert fake.calls[0][2].startswith("body: ")
    assert _state(tmp_path, "body")["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T1200"}, "2026-09-02": {"ts": "m3", "scan": "2026-09-02T1200"}}
    assert _at(_state(tmp_path, "sender"))["retired_at"] == "<t>"
    assert "stale" not in _state(tmp_path, "sender")


def test_a_switch_that_would_move_peoples_replies_is_held(tmp_path: Path):
    # a person replied after the daily replies: the weekly reply would be posted below them
    root, fake = _sender_thread(tmp_path)
    fake._say("h1", "U9")
    states = E.thread_states(CFG, str(root), SEP, "C1", _tpl().variants)
    plan = S.switch_plan(_tpl(), str(root), SEP, "C1", states, "sender", "weekly", client=fake)
    assert plan["reorder"] == [{"ts": "h1", "user": "U9"}]
    assert S.format_switch_plan(plan).split("\n")[-2:] == [
        "  held unless reorder is allowed: 1 people's replies would end up above the new replies",
        "    ts=h1 by U9",
    ]
    with pytest.raises(S.SwitchRefused) as e:
        S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3"))
    assert str(e.value) == ("digest: 2026-09: switch to weekly pending: 1 people's replies would end up above its new replies (h1 by U9) "
                            "and the switch doesn't allow it; preview it again")
    assert _calls(fake) == [("edit", "m1")]
    # allowed (the admin confirmed the reorder): it goes ahead
    fake.calls.clear()
    S.converge_thread(_tpl(), str(root), SEP, fake, "C1", "weekly", row=_row("weekly", "m2", "m3", reorder=("h1",)))
    assert _calls(fake) == [("edit", "m1"), ("post", "m1"), ("delete", "m2"), ("delete", "m3")]
    assert [m["ts"] for m in fake.thread] == ["m1", "h1", "m4"]


def test_cli_dry_run_writes_the_switch_plan(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from dt_cloud import cli

    root, _ = _sender_thread(tmp_path)
    for v in ("SLACK_BOT_TOKEN", "SLACK_SEARCH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    out = tmp_path / "reapply" / "job.switch.json"
    args = ["digest", "-T", "quota", "-C", str(config_file(CFG, tmp_path / "digest.yml")), "-r", str(root), "-m", "2026-09", "-c", "C1",
            "-n", "-V", "weekly", "-j", str(out)]
    r = CliRunner().invoke(cli.main, args)
    assert r.exit_code == 0, r.output
    assert r.stdout.split("\n")[0] == "digest 2026-09 in C1: switch sender -> weekly; dry-run, nothing posted, edited or deleted"
    assert r.stdout.split("\n")[-2:] == [f"switch plan: 1 to post, 2 to delete -> {out}", ""]
    assert [d["ts"] for d in json.loads(out.read_text())["delete"]] == ["m2", "m3"]
    # no switch (-V names the live variant): the usual dry run, and no plan (the site's preview passes -j whenever it
    # names a variant)
    out.unlink()
    same = CliRunner().invoke(cli.main, [*args[:-5], "-n", "-V", "sender", "-j", str(out)])
    assert same.exit_code == 0, same.output
    assert (same.stdout.split("\n")[-2:], out.exists()) == (["switch plan: none, 2026-09's thread is already sender; nothing written", ""], False)
    # without -V, -j still needs a selector
    bad = CliRunner().invoke(cli.main, [*args[:-5], "-n", "-j", str(out)])
    assert (bad.exit_code, bad.output.strip().split("\n")[-1]) == (2, "Error: -j/--plan writes a dry run's edits (-n with -o, -d or -y)")


def test_lease_url(tmp_path: Path):
    assert S.lease_url(CFG, str(tmp_path / "snapshots" / "cw"), SEP, "C1") == f"{tmp_path}/digest/cw/C1/2026-09.lease.json"
    r2 = E.config_from_dict({"template": "quota", "state": "digest/r2/{channel}/{variant}"})
    assert S.lease_url(r2, "r2://idx/snapshots/r2", SEP, "C9") == "r2://idx/digest/r2/C9/2026-09.lease.json"
