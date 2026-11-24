"""The digest OP's month links (`digest.relink`, `dt-cloud digest-relink`): each month's OP links the previous and
next months' threads (`← August · October →`), written into the text Slack stores (never a re-render), recorded in
each month's state so a re-run reads and edits nothing."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner
from thrds.slack import SlackClient

from dt_cloud import cli
from dt_cloud import digest as E

from conftest import thrds_fake
from digest_examples import GCS_EXAMPLE, config_file

URL = "https://slack.example.org/archives/C1/p"
IMAGE = {"type": "image", "image_url": "https://icons.example.org/plot-a.png?v=1", "alt_text": "GCS usage — August 2026"}
# what Slack adds to an image block it stores
STORED_IMAGE = {**IMAGE, "image_width": 1600, "image_height": 900, "image_bytes": 81234, "fallback": "1600x900px image", "is_animated": False}


@thrds_fake(SlackClient)
class StoredSlack:
    """A channel's messages as Slack stores them (``ts`` → the raw message), read and updated through the Web API calls
    thrds's rewrite makes; ``calls`` records each call's method and message ts."""

    channel = "C1"
    token = "xoxb-test"
    bot_ids = ("U1", "B1")

    def __init__(self, messages: dict[str, dict]):
        self.messages = messages
        self.calls: list[tuple[str, str | None]] = []

    def permalink(self, message_ts, channel=None):
        return f"{URL}{message_ts}"

    def _request(self, api, args, **kw):
        # (thrds passes the HTTP verb as `method=`)
        self.calls.append((api, args.get("ts")))
        if api == "conversations.replies":
            m = self.messages.get(args["ts"])
            return {"messages": [m] if m else []}
        if api == "conversations.history":
            return {"messages": [self.messages[max(self.messages)]]}
        if api == "chat.update":
            m = self.messages[args["ts"]]
            m["text"] = args["text"]
            # no blocks: Slack derives its own rich_text rendering of the text
            m["blocks"] = args["blocks"] or [{"type": "rich_text", "elements": []}]
            return {"ok": True}
        raise AssertionError(f"unexpected Slack call {api}")


def _op(ts: str, text: str, blocks: list[dict] | None) -> dict:
    return {"ts": ts, "text": text, "blocks": blocks, "bot_id": "B1", "subtype": "bot_message", "username": "GCS usage"}


def _section(text: str) -> dict:
    return {"type": "section", "block_id": "b1", "text": {"type": "mrkdwn", "text": text, "verbatim": False}}


AUG = "*August*\n\n*Weekly summaries*\nwk"
SEP = f"*September*\n\n<{URL}1001.0|← August>"
OCT = f"*October*\n\n<{URL}1002.0|← September>"


def _three_months(tmp_path: Path) -> tuple[Path, StoredSlack]:
    """August's OP (no month links yet: posted before they existed, over a plot), September's (linked back to August,
    over a plot) and October's (linked back, text only), each with its thread state."""
    root = tmp_path / "snapshots"
    for m, ts in (("2026-08", "1001.0"), ("2026-09", "1002.0"), ("2026-10", "1003.0")):
        E.save_state(str(tmp_path / "digest" / f"{m}.json"), {"op_ts": ts, "posted": {}})
    fake = StoredSlack({
        "1001.0": _op("1001.0", AUG, [_section(AUG), dict(STORED_IMAGE)]),
        "1002.0": _op("1002.0", SEP, [_section(SEP), dict(STORED_IMAGE)]),
        "1003.0": _op("1003.0", OCT, [{"type": "rich_text", "elements": []}]),
    })
    return root, fake


def _state(tmp_path: Path, m: str) -> dict:
    return json.loads((tmp_path / "digest" / f"{m}.json").read_text())


@pytest.fixture(autouse=True)
def _no_pace(monkeypatch):
    """thrds's rewrite paces its writes (1.2 s apart)."""
    import thrds.rewrite

    monkeypatch.setattr(thrds.rewrite.time, "sleep", lambda s: None)


def test_relink_text():
    line = f"<{URL}1|← August> · <{URL}3|October →>"
    # replaced in place
    assert E.relink_text(f"a\n\n<{URL}1|← August>", line) == f"a\n\n{line}"
    assert E.relink_text(f"a\n\n<{URL}1|← August>\n\nb", line) == f"a\n\n{line}\n\nb"
    # appended after the weekly lines when the OP has none
    assert E.relink_text("a\nb\n", line) == f"a\nb\n\n{line}"
    # a line that's more than month links is not the link line
    assert E.relink_text(f"see <{URL}1|← August> here", line) == f"see <{URL}1|← August> here\n\n{line}"
    # none to link: the line goes, with the blank line before it
    assert E.relink_text(f"a\n\n<{URL}1|← August>", "") == "a"
    assert E.relink_text("a", "") == "a"


def test_month_links():
    assert [E.month_links(date(2026, 9, 1), p, n) for p, n in (("P", "N"), ("P", None), (None, "N"), (None, None))] == [
        "[← August](P) · [October →](N)", "[← August](P)", "[October →](N)", "",
    ]
    assert (E.month_links(date(2026, 1, 1), "P", "N"), E.month_links(date(2026, 12, 1), "P", "N")) == (
        "[← December](P) · [February →](N)", "[← November](P) · [January →](N)",
    )


def test_relink_three_months(tmp_path: Path):
    root, fake = _three_months(tmp_path)
    months = [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    todo, plan, report = E.relink(GCS_EXAMPLE, str(root), months, fake, "C1", "sender")
    assert ([r.month for r in todo], [(a.ts, a.kind.value) for a in plan.actions], [(o.ts, o.status.value) for o in report.outcomes]) == (
        months, [("1001.0", "edit"), ("1002.0", "edit"), ("1003.0", "noop")], [("1001.0", "done"), ("1002.0", "done")],
    )
    # August gains its line after the weekly lines (text and section alike; the image as Slack takes it), September
    # its forward link beside its back link; October, the newest, already shows its one link
    aug = f"{AUG}\n\n<{URL}1002.0|September →>"
    sep = f"*September*\n\n<{URL}1001.0|← August> · <{URL}1003.0|October →>"
    assert fake.messages == {
        "1001.0": _op("1001.0", aug, [_section(aug), IMAGE]),
        "1002.0": _op("1002.0", sep, [_section(sep), IMAGE]),
        "1003.0": _op("1003.0", OCT, [{"type": "rich_text", "elements": []}]),
    }
    assert [c for c in fake.calls if c[0] == "chat.update"] == [("chat.update", "1001.0"), ("chat.update", "1002.0")]
    assert [_state(tmp_path, m) for m in ("2026-08", "2026-09", "2026-10")] == [
        {"op_ts": "1001.0", "posted": {}, "prev_url": None, "next_url": f"{URL}1002.0"},
        {"op_ts": "1002.0", "posted": {}, "prev_url": f"{URL}1001.0", "next_url": f"{URL}1003.0"},
        {"op_ts": "1003.0", "posted": {}, "prev_url": f"{URL}1002.0", "next_url": None},
    ]

    # a re-run reads nothing and edits nothing
    fake.calls.clear()
    assert E.relink(GCS_EXAMPLE, str(root), months, fake, "C1", "sender")[0] == []
    assert fake.calls == []


def test_digest_relink_cli(monkeypatch, tmp_path: Path):
    root, fake = _three_months(tmp_path)
    import thrds.slack

    monkeypatch.setattr(thrds.slack, "SlackClient", lambda token, channel: fake)
    args = ["digest-relink", "-C", str(config_file(GCS_EXAMPLE, tmp_path / "digest.yml")), "-r", str(root), "-c", "C1", "-t", "xoxb-test"]
    r = CliRunner().invoke(cli.main, [*args, "-n"])
    assert (r.exit_code, r.stdout.split("\n")) == (0, [
        "digest-relink C1 (sender): 3 month(s), 3 to relink; plan: 2 edit, 1 noop",
        "2026-08  OP 1001.0  edit",
        "--- 1001.0 (now)",
        "+++ 1001.0 (planned)",
        "@@ -2,3 +2,5 @@",
        " ",
        " *Weekly summaries*",
        " wk",
        "+",
        f"+<{URL}1002.0|September →>",
        "2026-09  OP 1002.0  edit",
        "--- 1002.0 (now)",
        "+++ 1002.0 (planned)",
        "@@ -1,3 +1,3 @@",
        " *September*",
        " ",
        f"-<{URL}1001.0|← August>",
        f"+<{URL}1001.0|← August> · <{URL}1003.0|October →>",
        "2026-10  OP 1003.0  noop",
        "",
    ])
    # a dry run edits and records nothing
    assert ([c for c in fake.calls if c[0] == "chat.update"], _state(tmp_path, "2026-08")) == ([], {"op_ts": "1001.0", "posted": {}})

    r = CliRunner().invoke(cli.main, [*args, "-m", "2026-09..2026-10"])
    assert (r.exit_code, r.stdout.split("\n")) == (0, [
        "digest-relink C1 (sender): 2 month(s), 2 to relink; plan: 1 edit, 1 noop",
        "applied: 1 done",
        "",
    ])
    assert ([c for c in fake.calls if c[0] == "chat.update"], "prev_url" in _state(tmp_path, "2026-08")) == ([("chat.update", "1002.0")], False)
