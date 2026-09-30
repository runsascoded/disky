"""Specs for the Shape-C digest content functions (`dt_cloud.digest`).

Exact-equality against a hand-built month (no Slack, no GCS): a lead-in scan
(8/2) plus two in-month scans (8/3 Mon, 8/4 Tue — same ISO week, so the label is
unambiguous). Totals chosen so every derived string is checkable by hand."""
from datetime import date

from dt_cloud import digest as D

TIB = 1024**4


def _meta(tot_tib: float, std: float, near: float, cold: float, arch: float) -> dict:
    return {
        "total_bytes": round(tot_tib * TIB),
        "class_bytes": {"1": round(std * TIB), "2": round(near * TIB), "3": round(cold * TIB), "4": round(arch * TIB)},
    }


# lead-in 8/2 (base), then 8/3 (+30) and 8/4 (−20); class sums match totals.
DATED_META = [
    ("2026-08-02", _meta(3000, 300, 600, 1500, 600)),
    ("2026-08-03", _meta(3030, 330, 600, 1500, 600)),
    ("2026-08-04", _meta(3010, 310, 600, 1500, 600)),
]
ROWS = D.rows_from_meta(DATED_META)[1:]  # slice the lead-in
AUG = date(2026, 8, 1)


def test_deg_daily_projection():
    # daily reply uses mult=7 (project the day's rate to a weekly-equivalent)
    assert [D.deg(p, 7) for p in (0.1, 0.3, 0.5, 1.8, 4.0)] == [10, 30, 40, 60, 70]


def test_deg_weekly_and_signs():
    assert [D.deg(p, 1) for p in (0.4, 1.0, 2.5)] == [10, 20, 30]
    assert D.deg(-1.8, 7) == -60
    assert D.deg(0.0, 1) == 0
    assert D.deg(100.0, 1) == 80  # capped


def test_rows_from_meta_deltas():
    r0, r1 = ROWS
    assert (r0.date, r0.tb, r0.cost, r0.dtb, r0.dcost) == ("2026-08-03", 3030.0, 19784, 30.0, 615)
    assert (r0.std, r0.near, r0.cold, r0.arch) == (330.0, 600.0, 1500.0, 600.0)
    assert (r1.date, r1.tb, r1.cost, r1.dtb, r1.dcost) == ("2026-08-04", 3010.0, 19374, -20.0, -410)


def test_reply_grow():
    assert D.reply(ROWS[0]) == (
        "8/3 — 3,030 TB (+30.0, 1.0%)",
        "$19,784/mo (+$615) [\u2197\ufe0e](https://gcs.oa.dev/?d=260803#diff)",
        "https://gcs-usage-icons.pages.dev/arrows/av_deg50.png?v=4",
    )


def test_reply_shrink():
    assert D.reply(ROWS[1]) == (
        "8/4 — 3,010 TB (−20.0, 0.7%)",
        "$19,374/mo (−$410) [\u2197\ufe0e](https://gcs.oa.dev/?d=260804#diff)",
        "https://gcs-usage-icons.pages.dev/arrows/av_deg-40.png?v=4",
    )


def test_reply_discord_link_text():
    # Discord: the bare glyph is too small, so the link reads "view →"
    assert D.reply(ROWS[0], platform="discord")[1] == "$19,784/mo (+$615) \u00b7 [view \u2192](https://gcs.oa.dev/?d=260803#diff)"


def test_op_body():
    assert D.op_body(ROWS, date(2026, 8, 1), "https://x/p.png").split("\n") == [
        ":arrow_deg20: **+10.0 TB** month-to-date · [dashboard](https://gcs.oa.dev/)",
        "",
        "*Weekly summaries*",
        ":arrow_deg0: [wk of 8/3](https://gcs.oa.dev/?d=260804-2d#over-time) _(partial)_ — **3,010 TB** (+10.0, 0.3%) · $19,374/mo (+$205)",
        "",
        "![GCS usage — August 2026](https://x/p.png)",
    ]


def test_op_body_full_week_not_partial():
    # a 7-scan week (Mon 8/3 .. Sun 8/9) is not flagged partial
    dm = [("2026-08-02", _meta(3000, 300, 600, 1500, 600))]
    for i, d in enumerate(range(3, 10)):
        dm.append((f"2026-08-0{d}", _meta(3000 + i, 300 + i, 600, 1500, 600)))
    rows = D.rows_from_meta(dm)[1:]
    bullet = D.op_body(rows, date(2026, 8, 1), "https://x/p.png").split("\n")[3]
    assert bullet.startswith(":arrow_deg0: [wk of 8/3](https://gcs.oa.dev/?d=260809-7d#over-time) — ")


# ---- Discord twin ---------------------------------------------------------


def test_op_body_without_plot():
    # `plot_url=None` (Discord attaches the PNG) drops the trailing image lines only
    assert D.op_body(ROWS, date(2026, 8, 1), None).split("\n") == [
        ":arrow_deg20: **+10.0 TB** month-to-date · [dashboard](https://gcs.oa.dev/)",
        "",
        "*Weekly summaries*",
        ":arrow_deg0: [wk of 8/3](https://gcs.oa.dev/?d=260804-2d#over-time) _(partial)_ — **3,010 TB** (+10.0, 0.3%) · $19,374/mo (+$205)",
    ]


def test_state_path():
    root = "gs://b/snapshots"
    assert D._state_path(root, AUG) == "gs://b/digest/2026-08.json"
    assert D._state_path(root, AUG, "discord", "123") == "gs://b/digest/discord/123/2026-08.json"
    import pytest

    with pytest.raises(ValueError, match="keyed by webhook id"):
        D._state_path(root, AUG, "discord")


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
    import pytest

    with pytest.raises(ValueError, match="'arrow_deg30' missing"):
        D.discordify(":arrow_deg30:", EMOJI)


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


# One scan in (8/3: +30 on 3000, 1 day → 7%/wk-equivalent → deg50; the week's
# bullet is +1.0% → deg20), then both scans (+10 net → deg20; week +0.3% → deg0).
OP_ONE_SCAN = "\n".join([
    "<:arrow_deg50:5> **+30.0 TB** month-to-date · [dashboard](https://gcs.oa.dev/)",
    "",
    "*Weekly summaries*",
    "<:arrow_deg20:2> [wk of 8/3](https://gcs.oa.dev/?d=260803-1d#over-time) _(partial)_ — **3,030 TB** (+30.0, 1.0%) · $19,784/mo (+$615)",
])
OP_TWO_SCANS = "\n".join([
    "<:arrow_deg20:2> **+10.0 TB** month-to-date · [dashboard](https://gcs.oa.dev/)",
    "",
    "*Weekly summaries*",
    "<:arrow_deg0:1> [wk of 8/3](https://gcs.oa.dev/?d=260804-2d#over-time) _(partial)_ — **3,010 TB** (+10.0, 0.3%) · $19,374/mo (+$205)",
])
CAL = "https://gcs-usage-icons.pages.dev/calendar.png?v=2"
AV = "https://gcs-usage-icons.pages.dev/arrows/av_deg"


def test_converge_discord_fresh_then_incremental():
    hook, bot, saves = _FakeHook(), _FakeBot(), []
    plot = "/x/plot.png"
    # fresh month, one scan in: OP (webhook, calendar sender, plot attached) → bot opens the thread → one reply
    state = D.converge_discord(ROWS[:1], AUG, {}, hook=hook, bot=bot, emoji=EMOJI, plot=plot, save=lambda s: saves.append(dict(s)))
    assert hook.calls == [
        ("post", OP_ONE_SCAN, None, "GCS usage — August 2026", CAL, [plot]),
        ("post", "$19,784/mo (+$615) \u00b7 [view \u2192](https://gcs.oa.dev/?d=260803#diff)", "t1", "8/3 — 3,030 TB (+30.0, 1.0%)", f"{AV}50.png?v=4", []),
    ]
    assert bot.calls == [("create_thread", "m1", "GCS usage — August 2026")]
    assert state == {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2"}}
    assert saves == [
        {"op_id": "m1", "thread_id": "t1"},
        {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2"}},
        {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2"}},
    ]

    # next day: OP edited in place (plot re-attached), only the new scan replied, no new thread
    hook.calls.clear()
    bot.calls.clear()
    state = D.converge_discord(ROWS, AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot=plot)
    assert hook.calls == [
        ("edit", "m1", OP_TWO_SCANS, [plot]),
        ("post", "$19,374/mo (−$410) \u00b7 [view \u2192](https://gcs.oa.dev/?d=260804#diff)", "t1", "8/4 — 3,010 TB (−20.0, 0.7%)", f"{AV}-40.png?v=4", []),
    ]
    assert bot.calls == []
    assert state == {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2", "2026-08-04": "m3"}}

    # same scans again: nothing but the OP refresh
    hook.calls.clear()
    D.converge_discord(ROWS, AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot=plot)
    assert hook.calls == [("edit", "m1", OP_TWO_SCANS, [plot])]


def test_converge_discord_edit_replies():
    # backfill mode: every posted reply is re-edited (through the thread-bound hook) to its current body; nothing is re-posted
    hook, bot, rhook = _FakeHook(), _FakeBot(), _FakeHook()
    state = {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2", "2026-08-04": "m3"}}
    D.converge_discord(ROWS, AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot="/x/plot.png", edit_replies=True, reply_hook=rhook)
    assert hook.calls == [("edit", "m1", OP_TWO_SCANS, ["/x/plot.png"])]
    assert rhook.calls == [
        ("edit", "m2", "$19,784/mo (+$615) \u00b7 [view \u2192](https://gcs.oa.dev/?d=260803#diff)", []),
        ("edit", "m3", "$19,374/mo (−$410) \u00b7 [view \u2192](https://gcs.oa.dev/?d=260804#diff)", []),
    ]
    assert state["posted"] == {"2026-08-03": "m2", "2026-08-04": "m3"}
