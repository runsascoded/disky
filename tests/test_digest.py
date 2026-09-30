"""Specs for the digest engine (``disk_tree.notify.digest``) and the reference
``BytesProfile``.

Exact-equality against a hand-built month (no Slack, no Discord, no bucket): a
lead-in scan (8/2) plus two in-month scans (8/3 Mon, 8/4 Tue — same ISO week, so
the week label is unambiguous). Totals are chosen so every derived string is
checkable by hand. The engine's ``converge_*`` lifecycles are driven with fake
posting clients that record their calls."""
from datetime import date

import pytest

from disk_tree.notify import digest as D
from disk_tree.notify import sources
from disk_tree.notify.profile import BytesProfile, build_profile

TIB = 1024**4


def _meta(tot_tib: float) -> dict:
    return {"total_bytes": round(tot_tib * TIB)}


# lead-in 8/2 (base), then 8/3 (+30) and 8/4 (−20).
DATED_META = [
    ("2026-08-02", _meta(3000)),
    ("2026-08-03", _meta(3030)),
    ("2026-08-04", _meta(3010)),
]
P = BytesProfile(
    name="disk-tree usage",
    site_url="https://disk-tree.example",
    icons_base="https://disk-tree.example/icons",
    avatar_rev=1,
)
ROWS = P.rows_from_meta(DATED_META)[1:]  # slice the lead-in
AUG = D.period_of(date(2026, 8, 1), "month")


def test_aug_3_is_a_monday():
    # the fixture's week labelling assumes it
    assert date(2026, 8, 3).weekday() == 0


# ---- shared arrow math (engine) --------------------------------------------


def test_deg_daily_projection():
    # a daily reply uses mult=7 (project the day's rate to a weekly-equivalent)
    assert [D.deg(p, 7) for p in (0.1, 0.3, 0.5, 1.8, 4.0)] == [10, 30, 40, 60, 70]


def test_deg_weekly_and_signs():
    assert [D.deg(p, 1) for p in (0.4, 1.0, 2.5)] == [10, 20, 30]
    assert D.deg(-1.8, 7) == -60
    assert D.deg(0.0, 1) == 0
    assert D.deg(100.0, 1) == 80  # capped


def test_emoji_name():
    # Discord emoji names can't contain `-`; negatives get an `m`
    assert [D.emoji_name(d) for d in (-80, -10, 0, 10, 80)] == [
        "arrow_degm80", "arrow_degm10", "arrow_deg0", "arrow_deg10", "arrow_deg80",
    ]


EMOJI = {"arrow_deg0": "1", "arrow_deg20": "2", "arrow_degm40": "3", "arrow_deg50": "5"}


def test_discordify():
    assert D.discordify(":arrow_deg20: up, :arrow_deg-40: down, :arrow_deg0: flat, :calendar: kept", EMOJI) == (
        "<:arrow_deg20:2> up, <:arrow_degm40:3> down, <:arrow_deg0:1> flat, :calendar: kept"
    )


def test_discordify_missing_emoji_raises():
    with pytest.raises(ValueError, match="'arrow_deg30' missing"):
        D.discordify(":arrow_deg30:", EMOJI)


# ---- period model (engine) -------------------------------------------------


def test_period_of_and_key():
    assert D.period_of(date(2026, 8, 4), "month") == D.Period("month", date(2026, 8, 1))
    assert D.period_of(date(2026, 8, 4), "week") == D.Period("week", date(2026, 8, 3))  # Mon
    assert D.period_of(date(2026, 8, 4), "day") == D.Period("day", date(2026, 8, 4))
    assert [D.period_of(date(2026, 8, 4), k).key for k in ("month", "week", "day")] == [
        "2026-08", "2026-W32", "2026-08-04",
    ]


def test_select_window_month():
    dates = ["2026-07-31", "2026-08-01", "2026-08-02", "2026-09-01"]
    window, has_lead_in = D.select_window(dates, AUG)
    assert (window, has_lead_in) == (["2026-07-31", "2026-08-01", "2026-08-02"], True)


def test_select_window_no_lead_in():
    # the first scan ever is in-period: no lead-in to slice
    assert D.select_window(["2026-08-01", "2026-08-02"], AUG) == (["2026-08-01", "2026-08-02"], False)


def test_state_path():
    root = "gs://b/snapshots"
    assert D.state_path(root, AUG) == "gs://b/digest/2026-08.json"
    assert D.state_path(root, AUG, "discord", "123") == "gs://b/digest/discord/123/2026-08.json"
    with pytest.raises(ValueError, match="keyed by webhook id"):
        D.state_path(root, AUG, "discord")


# ---- row source (windowing over scan totals) -------------------------------


def _b(tib: float) -> int:
    return round(tib * TIB)


def test_rows_from_totals_month_with_prior_month_lead_in():
    # a July scan is the lead-in for August's first delta, then sliced off
    totals = [("2026-07-31", _b(3000)), ("2026-08-03", _b(3030)), ("2026-08-04", _b(3010))]
    rows = sources.rows_from_totals(P, totals, AUG)
    assert [(r.date, r.tb, r.dtb) for r in rows] == [("2026-08-03", 3030.0, 30.0), ("2026-08-04", 3010.0, -20.0)]


def test_rows_from_totals_no_prior_scan():
    # the month's first scan is the first ever: no lead-in, so it carries dtb=None
    totals = [("2026-08-01", _b(3000)), ("2026-08-03", _b(3030))]
    rows = sources.rows_from_totals(P, totals, AUG)
    assert [(r.date, r.tb, r.dtb) for r in rows] == [("2026-08-01", 3000.0, None), ("2026-08-03", 3030.0, 30.0)]


def test_rows_from_totals_empty_when_no_scans_in_period():
    assert sources.rows_from_totals(P, [("2026-06-01", _b(3000))], AUG) == []


# ---- profile factory -------------------------------------------------------


def test_build_profile_bytes_with_keys():
    p = build_profile({"profile": "bytes", "name": "X", "site_url": "https://x/", "icons_base": None})
    assert isinstance(p, BytesProfile)
    assert (p.name, p.site_url, p.icons_base) == ("X", "https://x", None)


def test_build_profile_defaults_to_bytes():
    assert isinstance(build_profile({}), BytesProfile)


def test_build_profile_unknown_raises():
    with pytest.raises(ValueError, match="unknown digest profile"):
        build_profile({"profile": "nope"})


# ---- reference profile: rows + content -------------------------------------


def test_rows_from_meta_deltas():
    r0, r1 = ROWS
    assert (r0.date, r0.tb, r0.dtb) == ("2026-08-03", 3030.0, 30.0)
    assert (r1.date, r1.tb, r1.dtb) == ("2026-08-04", 3010.0, -20.0)


def test_reply_grow():
    assert P.reply(ROWS[0]) == (
        "8/3 — 3,030 TiB (+30.0, 1.0%)",
        "[↗︎](https://disk-tree.example/?d=260803#over-time)",
        "https://disk-tree.example/icons/arrows/av_deg50.png?v=1",
    )


def test_reply_shrink():
    assert P.reply(ROWS[1]) == (
        "8/4 — 3,010 TiB (−20.0, 0.7%)",
        "[↗︎](https://disk-tree.example/?d=260804#over-time)",
        "https://disk-tree.example/icons/arrows/av_deg-40.png?v=1",
    )


def test_reply_discord_link_text():
    # Discord: the bare glyph is too small, so the link reads "view →"
    assert P.reply(ROWS[0], "discord")[1] == "· [view →](https://disk-tree.example/?d=260803#over-time)"


def test_reply_no_icons_host_degrades_to_plain_sender():
    p = BytesProfile(icons_base=None)
    assert p.reply(ROWS[0])[2] == ""
    assert p.op_avatar == ""


def test_op_body():
    assert P.op_body(ROWS, AUG, "https://x/p.png").split("\n") == [
        ":arrow_deg20: **+10.0 TiB** month-to-date · [dashboard](https://disk-tree.example/)",
        "",
        "*Weekly summaries*",
        ":arrow_deg0: [wk of 8/3](https://disk-tree.example/?d=260804-2d#over-time) _(partial)_ — **3,010 TiB** (+10.0, 0.3%)",
        "",
        "![disk-tree usage — August 2026](https://x/p.png)",
    ]


def test_op_body_without_plot():
    # `plot_url=None` (Discord attaches the PNG) drops the trailing image lines only
    assert P.op_body(ROWS, AUG, None).split("\n") == [
        ":arrow_deg20: **+10.0 TiB** month-to-date · [dashboard](https://disk-tree.example/)",
        "",
        "*Weekly summaries*",
        ":arrow_deg0: [wk of 8/3](https://disk-tree.example/?d=260804-2d#over-time) _(partial)_ — **3,010 TiB** (+10.0, 0.3%)",
    ]


def test_op_body_full_week_not_partial():
    # a 7-scan week (Mon 8/3 .. Sun 8/9) is not flagged partial
    dm = [("2026-08-02", _meta(3000))]
    for i, d in enumerate(range(3, 10)):
        dm.append((f"2026-08-0{d}", _meta(3000 + i)))
    rows = P.rows_from_meta(dm)[1:]
    bullet = P.op_body(rows, AUG, "https://x/p.png").split("\n")[3]
    assert bullet == (
        ":arrow_deg0: [wk of 8/3](https://disk-tree.example/?d=260809-7d#over-time) — "
        "**3,006 TiB** (+6.0, 0.2%)"
    )


# ---- Discord converge lifecycle (engine + reference profile) ---------------


class _Msg:
    def __init__(self, id: str):
        self.id = id


class _FakeHook:
    """Records webhook calls as tuples; message ids are sequential."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.n = 0

    def post(self, content, thread_id=None, *, username=None, icon_url=None, files=()):
        self.n += 1
        self.calls.append(("post", content, thread_id, username, icon_url, list(files)))
        return _Msg(f"m{self.n}")

    def edit(self, message_id, content, *, files=()):
        self.calls.append(("edit", message_id, content, list(files)))
        return _Msg(message_id)


class _FakeBot:
    def __init__(self):
        self.calls: list[tuple] = []

    def create_thread(self, message_id, name):
        self.calls.append(("create_thread", message_id, name))
        return "t1"


OP_ONE_SCAN = "\n".join([
    "<:arrow_deg50:5> **+30.0 TiB** month-to-date · [dashboard](https://disk-tree.example/)",
    "",
    "*Weekly summaries*",
    "<:arrow_deg20:2> [wk of 8/3](https://disk-tree.example/?d=260803-1d#over-time) _(partial)_ — **3,030 TiB** (+30.0, 1.0%)",
])
OP_TWO_SCANS = "\n".join([
    "<:arrow_deg20:2> **+10.0 TiB** month-to-date · [dashboard](https://disk-tree.example/)",
    "",
    "*Weekly summaries*",
    "<:arrow_deg0:1> [wk of 8/3](https://disk-tree.example/?d=260804-2d#over-time) _(partial)_ — **3,010 TiB** (+10.0, 0.3%)",
])
CAL = "https://disk-tree.example/icons/calendar.png?v=2"
AV = "https://disk-tree.example/icons/arrows/av_deg"


def test_converge_discord_fresh_then_incremental():
    hook, bot, saves = _FakeHook(), _FakeBot(), []
    plot = "/x/plot.png"
    # fresh month, one scan in: OP (webhook, calendar sender, plot attached) → bot opens the thread → one reply
    state = D.converge_discord(P, ROWS[:1], AUG, {}, hook=hook, bot=bot, emoji=EMOJI, plot=plot, save=lambda s: saves.append(dict(s)))
    assert hook.calls == [
        ("post", OP_ONE_SCAN, None, "disk-tree usage — August 2026", CAL, [plot]),
        ("post", "· [view →](https://disk-tree.example/?d=260803#over-time)", "t1", "8/3 — 3,030 TiB (+30.0, 1.0%)", f"{AV}50.png?v=1", []),
    ]
    assert bot.calls == [("create_thread", "m1", "disk-tree usage — August 2026")]
    assert state == {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2"}}
    assert saves == [
        {"op_id": "m1", "thread_id": "t1"},
        {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2"}},
        {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2"}},
    ]

    # next day: OP edited in place (plot re-attached), only the new scan replied, no new thread
    hook.calls.clear()
    bot.calls.clear()
    state = D.converge_discord(P, ROWS, AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot=plot)
    assert hook.calls == [
        ("edit", "m1", OP_TWO_SCANS, [plot]),
        ("post", "· [view →](https://disk-tree.example/?d=260804#over-time)", "t1", "8/4 — 3,010 TiB (−20.0, 0.7%)", f"{AV}-40.png?v=1", []),
    ]
    assert bot.calls == []
    assert state == {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2", "2026-08-04": "m3"}}

    # same scans again: nothing but the OP refresh
    hook.calls.clear()
    D.converge_discord(P, ROWS, AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot=plot)
    assert hook.calls == [("edit", "m1", OP_TWO_SCANS, [plot])]


def test_converge_discord_edit_replies():
    # backfill: every posted reply is re-edited (through the thread-bound hook) to its current body; nothing re-posted
    hook, bot, rhook = _FakeHook(), _FakeBot(), _FakeHook()
    state = {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2", "2026-08-04": "m3"}}
    D.converge_discord(P, ROWS, AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot="/x/plot.png", edit_replies=True, reply_hook=rhook)
    assert hook.calls == [("edit", "m1", OP_TWO_SCANS, ["/x/plot.png"])]
    assert rhook.calls == [
        ("edit", "m2", "· [view →](https://disk-tree.example/?d=260803#over-time)", []),
        ("edit", "m3", "· [view →](https://disk-tree.example/?d=260804#over-time)", []),
    ]
    assert state["posted"] == {"2026-08-03": "m2", "2026-08-04": "m3"}


# ---- Slack converge lifecycle ----------------------------------------------


class _FakeSlack:
    def __init__(self):
        self.calls: list[tuple] = []
        self.n = 0

    def post(self, content, thread_id=None, *, username=None, icon_emoji=None, icon_url=None):
        self.n += 1
        self.calls.append(("post", content, thread_id, username, icon_emoji, icon_url))
        return _Msg(f"m{self.n}")

    def edit(self, message_id, content):
        self.calls.append(("edit", message_id, content))
        return _Msg(message_id)


SLACK_OP = "\n".join([
    ":arrow_deg20: **+10.0 TiB** month-to-date · [dashboard](https://disk-tree.example/)",
    "",
    "*Weekly summaries*",
    ":arrow_deg0: [wk of 8/3](https://disk-tree.example/?d=260804-2d#over-time) _(partial)_ — **3,010 TiB** (+10.0, 0.3%)",
    "",
    "![disk-tree usage — August 2026](https://x/p.png)",
])


def test_converge_slack_fresh():
    client = _FakeSlack()
    state = D.converge_slack(P, ROWS, AUG, {}, client=client, plot_url="https://x/p.png")
    assert client.calls == [
        ("post", SLACK_OP, None, "disk-tree usage — August 2026", ":calendar:", None),
        ("post", "[↗︎](https://disk-tree.example/?d=260803#over-time)", "m1", "8/3 — 3,030 TiB (+30.0, 1.0%)", None, f"{AV}50.png?v=1"),
        ("post", "[↗︎](https://disk-tree.example/?d=260804#over-time)", "m1", "8/4 — 3,010 TiB (−20.0, 0.7%)", None, f"{AV}-40.png?v=1"),
    ]
    assert state == {"op_ts": "m1", "posted": {"2026-08-03": "m2", "2026-08-04": "m3"}}


def test_converge_slack_incremental_edits_op_and_posts_only_new():
    client = _FakeSlack()
    state = {"op_ts": "m1", "posted": {"2026-08-03": "old"}}
    D.converge_slack(P, ROWS, AUG, state, client=client, plot_url="https://x/p.png")
    # OP is edited (not posted), so the fake's counter isn't bumped: the reply is m1
    assert client.calls == [
        ("edit", "m1", SLACK_OP),
        ("post", "[↗︎](https://disk-tree.example/?d=260804#over-time)", "m1", "8/4 — 3,010 TiB (−20.0, 0.7%)", None, f"{AV}-40.png?v=1"),
    ]
    assert state["posted"] == {"2026-08-03": "old", "2026-08-04": "m1"}
