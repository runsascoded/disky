"""Specs for the `gcs` digest template's content functions (`dt_cloud.digest_gcs`)
and the engine pieces it exercises (`dt_cloud.digest`: arrows, emoji, state
paths, the Discord converge).

Exact-equality against a hand-built month (no Slack, no GCS): a lead-in scan
(8/2) plus two in-month scans (8/3 Mon, 8/4 Tue — same ISO week, so the label is
unambiguous). Totals chosen so every derived string is checkable by hand."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from thrds import DiscordClient, DiscordWebhookClient, Image

from dt_cloud import digest as E
from dt_cloud import digest_gcs as D

from conftest import thrds_fake
from digest_examples import GCS_EXAMPLE

CFG = GCS_EXAMPLE
TPL = E.template(CFG)

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
ROWS = D.rows_from_meta(DATED_META, CFG.prices)[1:]  # slice the lead-in
AUG = date(2026, 8, 1)


def test_deg_daily_projection():
    # daily reply uses mult=7 (project the day's rate to a weekly-equivalent)
    assert [E.deg(p, 7) for p in (0.1, 0.3, 0.5, 1.8, 4.0)] == [10, 30, 40, 60, 70]


def test_deg_weekly_and_signs():
    assert [E.deg(p, 1) for p in (0.4, 1.0, 2.5)] == [10, 20, 30]
    assert E.deg(-1.8, 7) == -60
    assert E.deg(0.0, 1) == 0
    assert E.deg(100.0, 1) == 80  # capped


def test_rows_from_meta_deltas():
    r0, r1 = ROWS
    assert (r0.date, r0.tb, r0.cost, r0.dtb, r0.dcost) == ("2026-08-03", 3030.0, 19784, 30.0, 615)
    assert (r0.std, r0.near, r0.cold, r0.arch) == (330.0, 600.0, 1500.0, 600.0)
    assert (r1.date, r1.tb, r1.cost, r1.dtb, r1.dcost) == ("2026-08-04", 3010.0, 19374, -20.0, -410)


def test_reply_grow():
    assert D.reply(ROWS[0], CFG) == E.Reply(
        "8/3 — 3,030 TiB (+30.0, 1.0%)",
        "$19,784/mo (+$615) [\u2197\ufe0e](https://site.example.org/?d=260803#diff)",
        icon_url="https://icons.example.org/arrows/av_deg50.png?v=4",
    )


def test_reply_shrink():
    assert D.reply(ROWS[1], CFG) == E.Reply(
        "8/4 — 3,010 TiB (−20.0, 0.7%)",
        "$19,374/mo (−$410) [\u2197\ufe0e](https://site.example.org/?d=260804#diff)",
        icon_url="https://icons.example.org/arrows/av_deg-40.png?v=4",
    )


def test_reply_discord_link_text():
    # Discord: the bare glyph is too small, so the link reads "view →"
    assert D.reply(ROWS[0], CFG, platform="discord").body == "$19,784/mo (+$615) \u00b7 [view \u2192](https://site.example.org/?d=260803#diff)"




# ---- a reply per day: the day's first scheduled scan; a manual one only stands in ------------------------------------

def test_senders_skip_manual_scans_unless_a_day_has_nothing_else():
    scans = ["2026-08-02T0700", "2026-08-03T0300", "2026-08-03T0700", "2026-08-03T1500", "2026-08-04T0900", "2026-08-05T0300"]
    manual = {s: "manual" for s in ("2026-08-03T0300", "2026-08-03T1500", "2026-08-04T0900", "2026-08-05T0300")}
    # 8/4's manual scan stands in (8/5 has a scan); 8/5's waits for its scheduled one, unless the month is over
    assert D.senders(scans, manual) == ["2026-08-02T0700", "2026-08-03T0700", "2026-08-04T0900"]
    assert D.senders(scans, manual, closed=True) == ["2026-08-02T0700", "2026-08-03T0700", "2026-08-04T0900", "2026-08-05T0300"]
    # unrecorded triggers count as scheduled: each day's first scan
    assert D.senders(scans, {}) == ["2026-08-02T0700", "2026-08-03T0300", "2026-08-04T0900", "2026-08-05T0300"]


@pytest.fixture
def sub_root(tmp_path):
    """8/2 (date-only), then 8/3 a manual 06:01Z (+30) before the scheduled 18:01Z (−20), 8/4 07:00Z (+10), and a
    manual 8/5 03:00Z still waiting for its day's scheduled scan."""
    import json

    for s, m in [("2026-08-02", _meta(3000, 300, 600, 1500, 600)), ("2026-08-03T0601", _meta(3030, 330, 600, 1500, 600)),
                 ("2026-08-03T1801", _meta(3010, 310, 600, 1500, 600)), ("2026-08-04T0700", _meta(3020, 320, 600, 1500, 600)),
                 ("2026-08-05T0300", _meta(3040, 340, 600, 1500, 600))]:
        (tmp_path / s).mkdir()
        (tmp_path / s / "meta.json").write_text(json.dumps(m))
    return str(tmp_path)


SUB_TRIGGERS = {"2026-08-02": "schedule", "2026-08-03T0601": "manual", "2026-08-03T1801": "schedule", "2026-08-04T0700": "schedule",
                "2026-08-05T0300": "manual"}


def test_daily_rows_run_sender_to_sender(sub_root):
    rows = D.load_month(sub_root, AUG, CFG.prices, SUB_TRIGGERS).rows
    # 8/3's row is its scheduled scan, Δ vs 8/2 (the manual 06:01Z neither replies nor baselines); 8/5 not yet
    assert [(r.date, r.prev, r.tb, r.dtb, r.dcost, r.slug) for r in rows] == [
        ("2026-08-02", None, 3000.0, None, None, "260802"),
        ("2026-08-03T1801", "2026-08-02", 3010.0, 10.0, 205, "26080318"),
        ("2026-08-04T0700", "2026-08-03T1801", 3020.0, 10.0, 205, "260804"),
    ]


def test_daily_replies_are_keyed_by_date(sub_root):
    # one unit per day, keyed by its date, over its sender scan (whose canonical `?d=` slug names its time when the day
    # has others); the arrow projects the Δ over the senders' real interval (42h, then 13h) to a weekly rate
    assert TPL.units(D.load_month(sub_root, AUG, CFG.prices, SUB_TRIGGERS), "sender")[1:] == [
        E.Unit("2026-08-03", "2026-08-03T1801", E.Reply(
            "8/3 — 3,010 TiB (+10.0, 0.3%)",
            "$19,374/mo (+$205) [\u2197\ufe0e](https://site.example.org/?d=26080318#diff)",
            icon_url="https://icons.example.org/arrows/av_deg20.png?v=4",
        )),
        E.Unit("2026-08-04", "2026-08-04T0700", E.Reply(
            "8/4 — 3,020 TiB (+10.0, 0.3%)",
            "$19,579/mo (+$205) [\u2197\ufe0e](https://site.example.org/?d=260804#diff)",
            icon_url="https://icons.example.org/arrows/av_deg40.png?v=4",
        )),
    ]


def test_a_manual_scan_replies_for_its_day_once_a_later_scan_lands(sub_root):
    import shutil

    # 9/1's scan closes August: 8/5's manual scan stands in for its day
    shutil.copytree(Path(sub_root) / "2026-08-04T0700", Path(sub_root) / "2026-09-01")
    assert [(r.date, r.prev) for r in D.load_month(sub_root, AUG, CFG.prices, SUB_TRIGGERS).rows][-1] == ("2026-08-05T0300", "2026-08-04T0700")




def test_render_plot_posts_to_site(monkeypatch, tmp_path):
    from dt_cloud import site

    calls = []
    monkeypatch.setenv("SITE_TOKEN", "tok")
    monkeypatch.setattr(site, "post_json", lambda url, token, path, body, timeout=120: calls.append((url, token, path, body)) or b"\x89PNG")
    TPL.render_plot(D.Month(ROWS), AUG, tmp_path / "op.png")
    # the request is the site's (`POST /api/digest-op`'s `image`; conftest's stand-in here)
    assert calls == [("https://site.example.org", "tok", "/api/digest-image", {"site_image": "gcs", "month": "2026-08", "to": "2026-08-04"})]
    assert (tmp_path / "op.png").read_bytes() == b"\x89PNG"


def test_render_plot_without_site_token(monkeypatch, tmp_path):
    monkeypatch.delenv("SITE_TOKEN", raising=False)
    monkeypatch.delenv("GCS_USAGE_TOKEN", raising=False)
    with pytest.raises(E.PlotUnavailable) as e:
        TPL.render_plot(D.Month(ROWS), AUG, tmp_path / "op.png")
    assert str(e.value) == "digest image: no site token ($SITE_TOKEN)"
    assert sorted(tmp_path.iterdir()) == []


# ---- Discord twin ---------------------------------------------------------



def test_state_path():
    # Slack: one prod thread; Discord: keyed by webhook (a webhook can only edit its own messages)
    root = "gs://b/snapshots"
    assert E.state_path(root, AUG, CFG.state.format(channel="C1", variant="sender")) == "gs://b/digest/2026-08.json"
    assert E.state_path(root, AUG, CFG.discord_state.format(webhook="123")) == "gs://b/digest/discord/123/2026-08.json"


def test_emoji_name():
    # Discord emoji names can't contain `-`; negatives get an `m`
    assert [E.emoji_name(d) for d in (-80, -10, 0, 10, 80)] == [
        "arrow_degm80", "arrow_degm10", "arrow_deg0", "arrow_deg10", "arrow_deg80",
    ]


EMOJI = {"arrow_deg0": "1", "arrow_deg20": "2", "arrow_degm40": "3", "arrow_deg50": "5"}


def test_discordify():
    assert E.discordify(":arrow_deg20: up, :arrow_deg-40: down, :arrow_deg0: flat, :calendar: kept", EMOJI) == (
        "<:arrow_deg20:2> up, <:arrow_degm40:3> down, <:arrow_deg0:1> flat, :calendar: kept"
    )


def test_discordify_missing_emoji_raises():
    import pytest

    with pytest.raises(ValueError, match="'arrow_deg30' missing"):
        E.discordify(":arrow_deg30:", EMOJI)


class _Msg:
    def __init__(self, id: str):
        self.id = id


@thrds_fake(DiscordWebhookClient)
class _FakeHook:
    """Records webhook calls as tuples; message ids are sequential."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.n = 0

    def post(self, content, thread_id=None, *, username=None, icon_url=None, images=()):
        self.n += 1
        self.calls.append(("post", content, thread_id, username, icon_url, list(images)))
        return _Msg(f"m{self.n}")

    def edit(self, message_id, content, *, images=(), keep_attachments=True):
        self.calls.append(("edit", message_id, content, list(images)))
        return _Msg(message_id)

    def delete(self, message_id):
        self.calls.append(("delete", message_id))


@thrds_fake(DiscordClient)
class _FakeBot:
    def __init__(self):
        self.calls: list[tuple] = []

    def create_thread(self, message_id, name):
        self.calls.append(("create_thread", message_id, name))
        return "t1"


# The OP after one scan in (8/3), then both (8/4): the site's render (conftest's stand-in), posted as Discordified
OP_ONE_SCAN = "<site OP: gcs 2026-08, 1 scans to 2026-08-03, params ->"
OP_TWO_SCANS = "<site OP: gcs 2026-08, 2 scans to 2026-08-04, params ->"
CAL = "https://icons.example.org/calendar.png?v=2"
AV = "https://icons.example.org/arrows/av_deg"


def test_converge_discord_fresh_then_incremental():
    hook, bot, saves = _FakeHook(), _FakeBot(), []
    plot = "/x/plot.png"
    # fresh month, one scan in: OP (webhook, calendar sender, plot attached) → bot opens the thread → one reply
    state = E.converge_discord(TPL, D.Month(ROWS[:1]), AUG, {}, hook=hook, bot=bot, emoji=EMOJI, plot=plot, save=lambda s: saves.append(dict(s)))
    assert hook.calls == [
        ("post", OP_ONE_SCAN, None, "GCS usage — August 2026", CAL, [Image(path=plot)]),
        ("post", "$19,784/mo (+$615) \u00b7 [view \u2192](https://site.example.org/?d=260803#diff)", "t1", "8/3 — 3,030 TiB (+30.0, 1.0%)", f"{AV}50.png?v=4", []),
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
    state = E.converge_discord(TPL, D.Month(ROWS), AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot=plot)
    assert hook.calls == [
        ("edit", "m1", OP_TWO_SCANS, [Image(path=plot)]),
        ("post", "$19,374/mo (−$410) \u00b7 [view \u2192](https://site.example.org/?d=260804#diff)", "t1", "8/4 — 3,010 TiB (−20.0, 0.7%)", f"{AV}-40.png?v=4", []),
    ]
    assert bot.calls == []
    assert state == {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2", "2026-08-04": "m3"}}

    # same scans again: nothing but the OP refresh
    hook.calls.clear()
    E.converge_discord(TPL, D.Month(ROWS), AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot=plot)
    assert hook.calls == [("edit", "m1", OP_TWO_SCANS, [Image(path=plot)])]


def test_converge_discord_edit_replies():
    # backfill mode: every posted reply is re-edited (through the thread-bound hook) to its current body; nothing is re-posted
    hook, bot, rhook = _FakeHook(), _FakeBot(), _FakeHook()
    state = {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2", "2026-08-04": "m3"}}
    E.converge_discord(TPL, D.Month(ROWS), AUG, state, hook=hook, bot=bot, emoji=EMOJI, plot="/x/plot.png", edit_replies=True, reply_hook=rhook)
    assert hook.calls == [("edit", "m1", OP_TWO_SCANS, [Image(path="/x/plot.png")])]
    assert rhook.calls == [
        ("edit", "m2", "$19,784/mo (+$615) \u00b7 [view \u2192](https://site.example.org/?d=260803#diff)", []),
        ("edit", "m3", "$19,374/mo (−$410) \u00b7 [view \u2192](https://site.example.org/?d=260804#diff)", []),
    ]
    assert state["posted"] == {"2026-08-03": "m2", "2026-08-04": "m3"}


def test_redo_discord_since():
    # an old rule's thread: 8/3 under its scan's key + an extra, 8/4 under a scan key. Redo from 8/4: 8/3's reply kept
    # (re-keyed), its extra kept as `extra` (still in the thread); 8/4 posted fresh, then its old reply deleted through the thread hook
    hook, bot, rhook = _FakeHook(), _FakeBot(), _FakeHook()
    hook.n = 4  # m1–m4 already posted
    state = {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03T0700": "m2", "2026-08-03T1500": "m3", "2026-08-04T0700": "m4"}}
    kw = dict(hook=hook, reply_hook=rhook, bot=bot, emoji=EMOJI, plot="/x/plot.png")
    assert E.redo_discord(TPL, D.Month(ROWS), AUG, state, since=date(2026, 8, 4), **kw) == {
        "old": [("2026-08-04T0700", "m4")],
        "new": [("2026-08-04", "2026-08-04", "8/4 — 3,010 TiB (−20.0, 0.7%)")],
        "rekey": [("2026-08-03", "2026-08-03T0700")],
        "left": ["2026-08-03T1500"],
    }
    assert (hook.calls, rhook.calls) == ([], [])
    state = E.redo_discord(TPL, D.Month(ROWS), AUG, state, since=date(2026, 8, 4), for_real=True, **kw)
    assert hook.calls == [
        ("edit", "m1", OP_TWO_SCANS, [Image(path="/x/plot.png")]),
        ("post", "$19,374/mo (−$410) \u00b7 [view \u2192](https://site.example.org/?d=260804#diff)", "t1", "8/4 — 3,010 TiB (−20.0, 0.7%)", f"{AV}-40.png?v=4", []),
    ]
    assert rhook.calls == [("delete", "m4")]
    assert state == {"op_id": "m1", "thread_id": "t1", "posted": {"2026-08-03": "m2", "2026-08-04": "m5"}, "extra": {"2026-08-03T1500": "m3"}}


def test_md_takes_sub_daily_scan_ids() -> None:
    assert [E._md(s) for s in ("2026-10-09", "2026-10-09T1236")] == ["10/9", "10/9"]


def test_links_use_each_scans_canonical_slug():
    # 10/8 alone on its day; 10/9's date-only run (started 04:30Z, `meta.started`) and a 12:36Z scan. At 16:00Z the
    # 10/8 link is its day, 10/9's are their hours; at 12:50Z the 12:36Z scan's hour hasn't ended: its minute.
    import datetime as dt

    dm = [
        ("2026-10-08", _meta(3000, 300, 600, 1500, 600)),
        ("2026-10-09", {**_meta(3030, 330, 600, 1500, 600), "started": "2026-10-09T04:30:12.345Z"}),
        ("2026-10-09T1236", _meta(3010, 310, 600, 1500, 600)),
    ]
    at = lambda h, m=0: dt.datetime(2026, 10, 9, h, m, tzinfo=dt.timezone.utc)  # noqa: E731
    urls = lambda now: [D.reply(r, CFG).body.rsplit("(", 1)[1] for r in D.rows_from_meta(dm, CFG.prices, now)]  # noqa: E731
    assert urls(at(16)) == [
        "https://site.example.org/?d=261008#diff)",
        "https://site.example.org/?d=26100904#diff)",
        "https://site.example.org/?d=26100912#diff)",
    ]
    assert urls(at(12, 50))[2] == "https://site.example.org/?d=2610091236#diff)"
    # without the start, 10/9's hours stay unknown: both by their minutes
    dm[1] = ("2026-10-09", _meta(3030, 330, 600, 1500, 600))
    assert urls(at(16))[1:] == ["https://site.example.org/?d=2610090000#diff)", "https://site.example.org/?d=2610091236#diff)"]


def test_load_month_on_the_first(tmp_path: Path):
    # 11/1: the window's rows reach back into October (the site's OP reads them: the `gcs-first` parity fixture), the
    # month's own are November's only
    import json

    root = tmp_path / "snapshots"
    for i in range(38):  # 9/25 .. 11/1, +2 TiB a day of Standard
        d, t = date(2026, 9, 25) + timedelta(days=i), 3000 + 2 * i
        (root / f"{d}").mkdir(parents=True)
        (root / f"{d}" / "meta.json").write_text(json.dumps(_meta(t, t - 2700, 600, 1500, 600)))
    month = D.load_month(str(root), date(2026, 11, 1), CFG.prices, {})
    # from 9/28, the window's first week's Monday (9/27, its baseline, only seeds 9/28's delta)
    assert (month.pre[0].date, month.pre[-1].date, len(month.pre)) == ("2026-09-28", "2026-10-31", 34)
    # one reply a day, November's only
    assert [r.date for r in month.rows] == ["2026-11-01"]
