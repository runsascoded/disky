"""A digest thread's reply style per month (`digest_switch`, specs/digest-thread-variant.md): which variant is live, which
one a run converges, and the switch between them (post, retiring, reviewed deletes, retired), resumable at each step."""
import json
from datetime import date
from pathlib import Path

import pytest

from dt_cloud import digest as E
from dt_cloud import digest_switch as S

from digest_examples import config_file
from test_digest_quota import CFG, LEAD, SEP, SEPT, _FakeSlack, _publish, _tpl

OCT = date(2026, 10, 1)
WEEK = "wk-2026-08-31"
WEEK_HEAD = ":arrow_deg40: *week:* **+10.0 TiB (1.4%)**"


def _sender_thread(tmp_path: Path, **fake_kw):
    """September converged as `sender`: OP m1, replies m2 (9/1) and m3 (9/2)."""
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT)
    fake = _FakeSlack(**fake_kw)
    E.converge_slack(_tpl(), str(root), SEP, fake, "C1", "sender")
    assert [c[0] for c in fake.calls] == ["post", "post", "post"]
    fake.calls.clear()
    return root, fake


def _state(tmp_path: Path, variant: str) -> dict:
    return json.loads((tmp_path / "digest" / "cw" / "C1" / variant / "2026-09.json").read_text())


def _calls(fake) -> list[tuple]:
    """Each call as (kind, the message it touches: the edited / deleted ts, a post's thread)."""
    return [(c[0], c[2] if c[0] == "post" else c[1]) for c in fake.calls]


def _row(variant: str, *deletes: str) -> dict:
    return {"variant": variant, "deletes": list(deletes), "plan_job": "cw-digest-20260927-120000z", "updated_by": "admin@example.org", "updated_at": 1}


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
    root, _ = _sender_thread(tmp_path)
    states = E.thread_states(CFG, str(root), SEP, "C1", _tpl().variants)
    plan = S.switch_plan(_tpl(), str(root), SEP, "C1", states, "sender", "weekly")
    assert {**plan, "at": "<t>"} == {
        "digest_switch": 1, "month": "2026-09", "channel": "C1", "from": "sender", "to": "weekly",
        "post": [{"key": WEEK, "scan": "2026-09-02T1200", "headline": WEEK_HEAD}],
        "delete": [{"key": "2026-09-01", "ts": "m2"}, {"key": "2026-09-02", "ts": "m3"}],
        "at": "<t>",
    }
    assert S.format_switch_plan(plan).split("\n") == [
        "digest 2026-09 in C1: switch sender -> weekly; dry-run, nothing posted or deleted",
        "  replies to post (weekly): 1",
        f"    {WEEK}  2026-09-02T1200  {WEEK_HEAD!r}",
        "  replies to delete (sender): 2",
        "    2026-09-01  ts=m2",
        "    2026-09-02  ts=m3",
    ]


def test_cli_dry_run_writes_the_switch_plan(tmp_path: Path):
    from click.testing import CliRunner

    from dt_cloud import cli

    root, _ = _sender_thread(tmp_path)
    out = tmp_path / "reapply" / "job.switch.json"
    args = ["digest", "-T", "quota", "-C", str(config_file(CFG, tmp_path / "digest.yml")), "-r", str(root), "-m", "2026-09", "-c", "C1",
            "-n", "-V", "weekly", "-j", str(out)]
    r = CliRunner().invoke(cli.main, args)
    assert r.exit_code == 0, r.output
    assert r.stdout.split("\n")[0] == "digest 2026-09 in C1: switch sender -> weekly; dry-run, nothing posted or deleted"
    assert r.stdout.split("\n")[-2:] == [f"switch plan: 1 to post, 2 to delete -> {out}", ""]
    assert [d["ts"] for d in json.loads(out.read_text())["delete"]] == ["m2", "m3"]
    # no switch (-V names the live variant): the usual dry run, and -j is refused as before
    bad = CliRunner().invoke(cli.main, [*args[:-5], "-n", "-V", "sender", "-j", str(out)])
    assert (bad.exit_code, bad.output.strip().split("\n")[-1]) == (2, "Error: -j/--plan writes a dry run's edits (-n with -o, -d or -y)")


def test_lease_url(tmp_path: Path):
    assert S.lease_url(CFG, str(tmp_path / "snapshots" / "cw"), SEP, "C1") == f"{tmp_path}/digest/cw/C1/2026-09.lease.json"
    r2 = E.config_from_dict({"template": "quota", "state": "digest/r2/{channel}/{variant}"})
    assert S.lease_url(r2, "r2://idx/snapshots/r2", SEP, "C9") == "r2://idx/digest/r2/C9/2026-09.lease.json"
