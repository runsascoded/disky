"""Specs for the CoreWeave digest (`dt_cloud.digest`): the pure helpers, the
framing-A content (OP + both reply variants, exact strings), the daily keying
rule, and the monthly-thread converge against a fake Slack client over a local
snapshot tree.

Hand-built series (12-hourly, 00:00Z / 12:00Z): a lead day Mon 8/31 (700,
705 TiB) then Tue 9/1 (710, 713) and Wed 9/2 (712, 715) — one ISO week, so
the bullet label is unambiguous. Quota = 10^15 B = 909.4947 TiB."""
import datetime as dt
import json
from datetime import date
from pathlib import Path

import pytest

from dt_cloud import cw_digest as D

TIB = 1024**4
UTC = dt.timezone.utc
SITE = "https://cw-s3.oa.dev"
AV = "https://gcs-usage-icons.pages.dev/arrows/av_deg"
SEP = date(2026, 9, 1)


def _meta(tot_tib: float, objs: int = 1_000_000) -> dict:
    return {"total_bytes": round(tot_tib * TIB), "total_objects": objs, "class_bytes": {}}


LEAD = [("2026-08-31T0000", _meta(700)), ("2026-08-31T1200", _meta(705))]
SEPT = [("2026-09-01T0000", _meta(710)), ("2026-09-01T1200", _meta(713)), ("2026-09-02T0000", _meta(712)), ("2026-09-02T1200", _meta(715))]
_all = D.rows_from_meta(LEAD + SEPT)
MONTH = D.Month(lead=_all[:2], rows=_all[2:])


def test_quota_constant():
    # 1 PB decimal, which the site's "910 TiB" comments round
    assert D.QUOTA_BYTES == 10**15
    assert round(D.QUOTA_TIB, 2) == 909.49
    assert (D._quota(715.0), D._free(715.0), D._free(D.QUOTA_TIB)) == ("78.6% of 1 PB", "194.5 TiB free", "0.0 TiB free")


def test_deg_projection():
    # a daily reply projects with 168/24 = 7; a 12-hourly interval with 14
    assert [D.deg(p, 7) for p in (0.1, 0.3, 0.5, 1.8, 4.0)] == [10, 30, 40, 60, 70]
    assert [D.deg(p, 14) for p in (0.1, 0.3, 0.5, 1.8)] == [20, 40, 50, 70]
    assert [D.deg(p, 1) for p in (0.4, 1.0, 2.5)] == [10, 20, 30]
    assert (D.deg(-1.8, 7), D.deg(0.0, 1), D.deg(100.0, 1)) == (-60, 0, 80)


def test_scan_ts():
    assert D.scan_ts("2026-09-07T1201") == dt.datetime(2026, 9, 7, 12, 1, tzinfo=UTC)
    assert D.scan_ts("2026-09-07") == dt.datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="not a scan id"):
        D.scan_ts("260907")


def test_formatters_and_links():
    assert [D._tb(v) for v in (3.0, 0.0, -1.25)] == ["+3.0", "+0.0", "−1.2"]
    assert [D._pct(3.0, 903.0), D._pct(-1.0, 902.0), D._pct(5.0, 5.0)] == ["0.3", "0.1", "0.0"]
    assert [D._dlink(s) for s in ("2026-09-07T1201", "2026-09-07")] == ["260907-1201", "260907"]
    t0 = D.scan_ts("2026-09-06T1200")
    assert [D._span(t0, D.scan_ts(s)) for s in ("2026-09-08T0000", "2026-09-13T1200", "2026-09-07T0000", "2026-09-06T1200", "2026-09-07T1159")] == ["1d12h", "7d", "12h", "0h", "1d"]
    assert D._diff_url("2026-09-02T1200", D.scan_ts("2026-08-31T1200"), SITE) == f"{SITE}/?d=260902-1200-2d#over-time"
    assert D._diff_url("2026-09-02T1200", None, SITE) == f"{SITE}/?d=260902-1200#over-time"


def _meta2(primary_tib: float, hero_tib: float, objs: int = 1_000_000) -> dict:
    """A multi-bucket scan's meta.json (specs/cw-multi-bucket.md §2)."""
    return {
        "total_bytes": round((primary_tib + hero_tib) * TIB), "total_objects": objs + 400_000, "class_bytes": {},
        "buckets": {
            "marin-us-east-02a": {"total_bytes": round(primary_tib * TIB), "total_objects": objs},
            "hero-checkpoints": {"total_bytes": round(hero_tib * TIB), "total_objects": 400_000},
        },
    }


def test_primary_totals():
    assert D.primary_totals(_meta(700)) == (700 * TIB, 1_000_000, {})
    assert D.primary_totals(_meta2(700, 89.25)) == (700 * TIB, 1_000_000, {"hero-checkpoints": 89.25})
    # a meta whose `buckets` lacks the primary reads as the flat totals
    assert D.primary_totals({**_meta(5), "buckets": {"x": {"total_bytes": TIB, "total_objects": 1}}}) == (5 * TIB, 1_000_000, {})


def test_rows_from_meta_buckets_switch():
    # flat metas then `buckets` metas: the primary's deltas stay continuous
    # across the switch (the flat totals were the primary's); the hero clause's
    # Δ appears once a prior scan has the bucket
    rows = D.rows_from_meta([("2026-09-01T0000", _meta(710)), ("2026-09-01T1200", _meta2(713, 89.25)), ("2026-09-02T0000", _meta2(712, 90.75))])
    assert [(r.tb, r.dtb, r.extra) for r in rows] == [(710.0, None, {}), (713.0, 3.0, {"hero-checkpoints": 89.2}), (712.0, -1.0, {"hero-checkpoints": 90.8})]
    month = D.Month(lead=[], rows=rows)
    d1, d2 = D.day_rows(month, "body")
    assert (d1.extra, d1.dextra) == ({"hero-checkpoints": 89.2}, {"hero-checkpoints": None})
    assert (d2.extra, d2.dextra) == ({"hero-checkpoints": 90.8}, {"hero-checkpoints": 1.6})
    assert D.reply(d1, "body").body == f":arrow_deg0: [9/1]({SITE}/?d=260901-1200#over-time) — **713 TiB (+0.0, 0.0%)** · 78.4% of 1 PB · 196.5 TiB free · hero-checkpoints 89 TiB"
    assert D.reply(d2, "sender") == D.Reply(
        "9/2 — 712 TiB (−1.0, 0.1%)",
        f"78.3% of 1 PB · 197.5 TiB free · hero-checkpoints 91 TiB (+1.6) [↗︎]({SITE}/?d=260902-0000-12h#over-time)",
        icon_url=f"{AV}-30.png?v=4",
    )
    # the OP headline carries the clause too, Δ vs the month's base scan
    assert D.op_body(month, SEP, None).split("\n")[0] == (
        f":arrow_deg30: **+2.0 TiB** [month-to-date]({SITE}/?d=260902-0000-1d#over-time) · 712 TiB · 78.3% of 1 PB · hero-checkpoints 91 TiB · [dashboard]({SITE}/)"
    )


def test_primary_node():
    a, b = _node("marin-us-east-02a", 1), _node("hero-checkpoints", 2)
    assert D.primary_node({"n": "root", "c": [b, a]}) is a
    assert D.primary_node({"n": "root", "c": [b]}) is b  # single-bucket scans: the only child


def test_rows_from_meta_deltas():
    r = MONTH.rows[0]
    assert (r.scan, r.date, r.tb, r.dtb, r.hours) == ("2026-09-01T0000", "2026-09-01", 710.0, 5.0, 12.0)
    assert (MONTH.lead[0].dtb, MONTH.lead[0].hours) == (None, None)
    assert MONTH.base.scan == "2026-08-31T1200"
    assert D.Month(lead=[], rows=_all[2:]).base.scan == "2026-09-01T0000"


def test_day_rows_variants():
    # sender: a day is its MORNING scan (first at/after 12:00Z), Δ vs the prior day's morning scan
    assert D.day_rows(MONTH, "sender") == [
        D.DayRow("2026-09-01", "2026-09-01T1200", 713.0, 8.0, 24.0, D.scan_ts("2026-08-31T1200")),
        D.DayRow("2026-09-02", "2026-09-02T1200", 715.0, 2.0, 24.0, D.scan_ts("2026-09-01T1200")),
    ]
    # a half-landed day (only its 00:00 scan, no later day yet) has no reply yet
    assert [d.date for d in D.day_rows(D.Month(lead=MONTH.lead, rows=MONTH.rows[:3]), "sender")] == ["2026-09-01"]
    # …but once the next day has started, the day's last scan stands in (a missed 12:01Z scan)
    assert D.day_rows(D.Month(lead=MONTH.lead, rows=[MONTH.rows[0], MONTH.rows[2]]), "sender") == [
        D.DayRow("2026-09-01", "2026-09-01T0000", 710.0, 5.0, 12.0, D.scan_ts("2026-08-31T1200")),
    ]
    # `reply_hour=0` is the old first-scan rule
    assert D.day_rows(MONTH, "sender", reply_hour=0)[0] == D.DayRow("2026-09-01", "2026-09-01T0000", 710.0, 10.0, 24.0, D.scan_ts("2026-08-31T0000"))
    # body: a day is its LAST scan so far, Δ vs the prior day's last
    assert D.day_rows(MONTH, "body") == [
        D.DayRow("2026-09-01", "2026-09-01T1200", 713.0, 8.0, 24.0, D.scan_ts("2026-08-31T1200")),
        D.DayRow("2026-09-02", "2026-09-02T1200", 715.0, 2.0, 24.0, D.scan_ts("2026-09-01T1200")),
    ]
    # a half-landed day on `body`: its reply is that day's 00:00 scan, 12 h after the prior day's last
    half = D.Month(lead=MONTH.lead, rows=MONTH.rows[:3])
    assert D.day_rows(half, "body")[1] == D.DayRow("2026-09-02", "2026-09-02T0000", 712.0, -1.0, 12.0, D.scan_ts("2026-09-01T1200"))
    # first month ever: no prior for day 1
    assert D.day_rows(D.Month(lead=[], rows=MONTH.rows), "sender")[0] == D.DayRow("2026-09-01", "2026-09-01T1200", 713.0, None, None, None)
    with pytest.raises(ValueError, match="variant must be one of"):
        D.day_rows(MONTH, "x")


def test_op_body():
    # month-to-date +10.0 on 705 over 2 days → 1.42%·3.5 = 5.0%/wk → deg40;
    # the (partial) week +10.0 → 1.4% → deg20, linked over 8/31 12:00 → 9/2 12:00 = 2d
    assert D.op_body(MONTH, SEP, "https://x/p.png").split("\n") == [
        f":arrow_deg40: **+10.0 TiB** [month-to-date]({SITE}/?d=260902-1200-2d#over-time) · 715 TiB · 78.6% of 1 PB · [dashboard]({SITE}/)",
        "",
        "*Weekly summaries*",
        f":arrow_deg20: [wk of 8/31]({SITE}/?d=260902-1200-2d#over-time) _(partial)_: **+10.0 TiB** → 715 TiB · 78.6% of 1 PB",
        "",
        "![CoreWeave usage — September 2026](https://x/p.png)",
    ]
    assert D.op_body(MONTH, SEP, None).split("\n")[-1].startswith(":arrow_deg20: [wk of 8/31]")


def test_op_body_two_weeks():
    # a completed week (Sunday scanned) is not partial; the next week's bullet is Δ vs that week's end
    metas = LEAD + [(f"2026-09-{d:02d}T{h}", _meta(700 + i)) for i, (d, h) in enumerate(((d, h) for d in range(1, 8) for h in ("0000", "1200")), start=1)]
    rows = D.rows_from_meta(metas)
    month = D.Month(lead=rows[:2], rows=rows[2:])
    bullets = D.op_body(month, SEP, None).split("\n")[3:]
    assert bullets == [
        f":arrow_deg20: [wk of 8/31]({SITE}/?d=260906-1200-6d#over-time): **+7.0 TiB** → 712 TiB · 78.3% of 1 PB",
        f":arrow_deg0: [wk of 9/7]({SITE}/?d=260907-1200-1d#over-time) _(partial)_: **+2.0 TiB** → 714 TiB · 78.5% of 1 PB",
    ]


def test_reply_sender_variant():
    d1, d2 = D.day_rows(MONTH, "sender")
    # +8.0 on 705 in 24 h → 1.13%·7 = 7.9%/wk → deg50; +2.0 on 713 → 0.28%·7 = 2.0% → deg30
    assert D.reply(d1, "sender") == D.Reply(
        "9/1 — 713 TiB (+8.0, 1.1%)",
        f"78.4% of 1 PB · 196.5 TiB free [↗︎]({SITE}/?d=260901-1200-1d#over-time)",
        icon_url=f"{AV}50.png?v=4",
    )
    assert D.reply(d2, "sender") == D.Reply(
        "9/2 — 715 TiB (+2.0, 0.3%)",
        f"78.6% of 1 PB · 194.5 TiB free [↗︎]({SITE}/?d=260902-1200-1d#over-time)",
        icon_url=f"{AV}30.png?v=4",
    )


def test_reply_body_variant():
    d1, d2 = D.day_rows(MONTH, "body")
    # +8.0 on 705 in 24 h → 1.13%·7 = 7.9%/wk → deg50; +2.0 on 713 → deg30
    assert D.reply(d1, "body") == D.Reply(
        "CoreWeave usage",
        f":arrow_deg50: [9/1]({SITE}/?d=260901-1200-1d#over-time) — **713 TiB (+8.0, 1.1%)** · 78.4% of 1 PB · 196.5 TiB free",
        icon_emoji=":calendar:",
    )
    assert D.reply(d2, "body") == D.Reply(
        "CoreWeave usage",
        f":arrow_deg30: [9/2]({SITE}/?d=260902-1200-1d#over-time) — **715 TiB (+2.0, 0.3%)** · 78.6% of 1 PB · 194.5 TiB free",
        icon_emoji=":calendar:",
    )


def test_reply_first_day_ever():
    # no prior scan: zero delta, flat arrow, link without a look-back
    day = D.day_rows(D.Month(lead=[], rows=MONTH.rows[:2]), "sender")[0]
    assert D.reply(day, "sender") == D.Reply("9/1 — 713 TiB (+0.0, 0.0%)", f"78.4% of 1 PB · 196.5 TiB free [↗︎]({SITE}/?d=260901-1200#over-time)", icon_url=f"{AV}0.png?v=4")


def test_state_path():
    # namespaced under cw/, keyed by channel AND variant: staging A/B and prod never share a thread
    assert D._state_path("gs://b/snapshots/cw", SEP, "C1", "sender") == "gs://b/digest/cw/C1/sender/2026-09.json"


# ---- converge mechanism ----------------------------------------------------


class _Msg:
    def __init__(self, id: str):
        self.id = id


class _FakeSlack:
    """Records `post`/`edit` calls as tuples; message ids are sequential."""

    def __init__(self, fail_post_at: int | None = None, fail_delete: set[str] = frozenset()):
        self.calls: list[tuple] = []
        self.n = 0
        self.fail_post_at = fail_post_at  # the n-th post raises
        self.fail_delete = fail_delete

    def post(self, content, thread_id=None, *, username=None, icon_url=None, icon_emoji=None):
        self.n += 1
        if self.n == self.fail_post_at:
            raise RuntimeError("slack down")
        self.calls.append(("post", content, thread_id, username, icon_url, icon_emoji))
        return _Msg(f"m{self.n}")

    def edit(self, ts, content):
        self.calls.append(("edit", ts, content))
        return _Msg(ts)

    def delete(self, message_id, orphans_ok=False):
        self.calls.append(("delete", message_id))
        if message_id in self.fail_delete:
            raise RuntimeError("cant_delete_message")


def _publish(root: Path, dated_meta) -> None:
    for scan, m in dated_meta:
        d = root / scan
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(m))


def test_load_month(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, [("2026-08-30T1200", _meta(690))] + LEAD + SEPT)
    month = D.load_month(str(root), SEP)
    # lead = every scan of the last pre-month day (not 8/30); rows = the in-month scans
    assert [r.scan for r in month.lead] == ["2026-08-31T0000", "2026-08-31T1200"]
    assert [r.scan for r in month.rows] == [s for s, _ in SEPT]
    assert (month.lead[0].dtb, month.rows[0].dtb, month.rows[0].hours) == (None, 5.0, 12.0)  # deltas only within the loaded window
    assert D.load_month(str(root), date(2026, 7, 1)) is None
    _publish(tmp_path / "fresh", SEPT)
    assert D.load_month(str(tmp_path / "fresh"), SEP).lead == []


def _post(c):
    return (c[0], c[2], c[3], c[4], c[5]) if c[0] == "post" else c[:2]


def test_post_digest_sender_variant(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT[:1])
    fake = _FakeSlack()
    # 9/1 00:00 lands: the OP under the month sender — no reply yet (the morning scan is still to come)
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    plot = state["plot_name"]
    assert plot.startswith("plot-") and plot.endswith(".png")
    assert [_post(c) for c in fake.calls] == [("post", None, "CoreWeave usage — September 2026", None, ":calendar:")]
    assert fake.calls[0][1].startswith(":arrow_deg") and f"https://cw.gcs-usage-icons.pages.dev/{plot}?v=" in fake.calls[0][1]
    assert state == {"plot_name": plot, "variant": "sender", "op_ts": "m1", "posted": {}}

    # 9/1 12:00 lands: OP refreshed + the day's reply (headline as sender, arrow avatar)
    _publish(root, SEPT[1:2])
    fake.calls.clear()
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/1 — 713 TiB (+8.0, 1.1%)", f"{AV}50.png?v=4", None)]
    assert state["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T1200"}}
    assert json.loads((tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").read_text()) == state

    # 9/2 00:00 lands: only the OP is refreshed — no reply, no edit
    _publish(root, SEPT[2:3])
    fake.calls.clear()
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1")]
    assert state["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T1200"}}

    # 9/2 12:00 lands: the new day's reply
    _publish(root, SEPT[3:4])
    fake.calls.clear()
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/2 — 715 TiB (+2.0, 0.3%)", f"{AV}30.png?v=4", None)]
    assert state["posted"]["2026-09-02"] == {"ts": "m3", "scan": "2026-09-02T1200"}
    assert state["plot_name"] == plot


def test_post_digest_body_variant(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT[:1])
    fake = _FakeSlack()
    # 9/1 00:00: OP + the day's reply under the static sender, headline in the body (Δ vs 8/31's last = 12 h)
    D.post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert [_post(c) for c in fake.calls] == [
        ("post", None, "CoreWeave usage — September 2026", None, ":calendar:"),
        ("post", "m1", "CoreWeave usage", None, ":calendar:"),
    ]
    assert fake.calls[1][1] == f":arrow_deg50: [9/1]({SITE}/?d=260901-0000-12h#over-time) — **710 TiB (+5.0, 0.7%)** · 78.1% of 1 PB · 199.5 TiB free"

    # 9/1 12:00 lands: the OP AND the day's reply are edited to the latest scan (now a 24 h Δ)
    _publish(root, SEPT[1:2])
    fake.calls.clear()
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert fake.calls[0][:2] == ("edit", "m1")
    assert fake.calls[1] == ("edit", "m2", f":arrow_deg50: [9/1]({SITE}/?d=260901-1200-1d#over-time) — **713 TiB (+8.0, 1.1%)** · 78.4% of 1 PB · 196.5 TiB free")
    assert state["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T1200"}}

    # same scans again: nothing but the OP refresh (the reply already reflects the latest scan)
    fake.calls.clear()
    D.post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1")]

    # both variants in one channel keep separate threads/state
    other = _FakeSlack()
    D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=other)
    assert [c[0] for c in other.calls] == ["post", "post"]
    assert (tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").exists()


def test_post_digest_empty_month(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT)
    fake = _FakeSlack()
    assert D.post_digest(str(root), date(2026, 10, 1), "xoxb", "C1", client=fake) == {}
    assert fake.calls == []


# ---- diff treemap ------------------------------------------------------------


def _node(n: str, b: float, kids=()) -> dict:
    d = {"n": n, "b": round(b * TIB), "o": 1, "d": 20000}
    if kids:
        d["c"] = list(kids)
    return d


BASE_TREE = _node("bkt", 160, [
    _node("marin", 100, [_node("a", 60), _node("s", 40)]),
    _node("tmp", 50, [_node("t14", 50)]),
    _node("models", 10),
])
LATEST_TREE = _node("bkt", 165, [
    _node("marin", 130, [_node("a", 50), _node("s", 70), _node("new", 10)]),
    _node("tmp", 30, [_node("t14", 20), _node("t30", 10)]),
    _node("iris", 5, [_node("x", 5)]),
])


def _cells(cs):
    return [(c.path, c.group, round(c.delta / TIB, 1)) for c in cs]


def test_tree_diff():
    # depth-2 cells by path; a dir on one side only is fully grown/shrunk; a
    # childless top-level dir is its own cell; groups by Σ|Δ| desc, cells by |Δ| desc then path
    assert _cells(D.tree_diff(BASE_TREE, LATEST_TREE)) == [
        ("marin/s", "marin", 30.0), ("marin/a", "marin", -10.0), ("marin/new", "marin", 10.0),
        ("tmp/t14", "tmp", -30.0), ("tmp/t30", "tmp", 10.0),
        ("models", "models", -10.0),
        ("iris/x", "iris", 5.0),
    ]


def test_tree_diff_residual():
    # what the (pruned) children don't account for lands in `<group>/…`
    base = _node("bkt", 100, [_node("marin", 100, [_node("a", 60)])])
    latest = _node("bkt", 130, [_node("marin", 130, [_node("a", 50)])])
    assert _cells(D.tree_diff(base, latest)) == [("marin/…", "marin", 40.0), ("marin/a", "marin", -10.0)]
    assert D.tree_diff(base, base) == []


def test_tree_diff_folds_small():
    # total |Δ| = 105 Ti; min_frac 0.2 → 21 Ti: small cells join their group's `…`
    # (tmp/t30; marin's a and new cancel to nothing), small groups (models −10,
    # iris +5) join `other`; groups re-rank on what's left (tmp 40 > marin 30)
    assert _cells(D.tree_diff(BASE_TREE, LATEST_TREE, min_frac=0.2)) == [
        ("tmp/t14", "tmp", -30.0), ("tmp/…", "tmp", 10.0),
        ("marin/s", "marin", 30.0),
        ("other", "other", -5.0),
    ]


def test_squarify():
    from dt_cloud.cw_digest_plot import squarify

    assert squarify([5], 0, 0, 3, 2) == [(0, 0, 3, 2)]
    # a wide rect splits into vertical strips; a tall one into horizontal strips
    assert squarify([1, 1], 0, 0, 2, 1) == [(0, 0, 1, 1), (1, 0, 1, 1)]
    assert squarify([1, 1], 0, 0, 1, 2) == [(0, 0, 1, 1), (0, 1, 1, 1)]
    # 4×2 with [2,1,1]: the 2 takes a 2×2 column (ratio 1); the two 1s share the
    # remaining 2×2 as stacked 2×1 (adding the second doesn't worsen the row)
    assert squarify([2, 1, 1], 0, 0, 4, 2) == [(0, 0, 2, 2), (2, 0, 2, 1), (2, 1, 2, 1)]
    rects = squarify([6, 6, 4, 3, 2, 2, 1], 10, 20, 6, 4)
    assert len(rects) == 7
    assert round(sum(w * h for _, _, w, h in rects), 9) == 24
    assert [round(w * h, 9) for _, _, w, h in rects] == [6, 6, 4, 3, 2, 2, 1]
    assert squarify([], 0, 0, 1, 1) == []


def test_render_smoke(tmp_path: Path):
    pytest.importorskip("matplotlib")
    from dt_cloud.cw_digest_plot import render

    rows = [{"scan": s, "tb": D.rows_from_meta([(s, m)])[0].tb} for s, m in SEPT]
    out = tmp_path / "p.png"
    render(rows, out, "t", diff=D.tree_diff(BASE_TREE, LATEST_TREE), diff_label="8/31 → 9/2")
    assert out.stat().st_size > 10_000
    render(rows, tmp_path / "s.png", "t")  # sparkline only
    assert (tmp_path / "s.png").stat().st_size > 5_000


# ---- redo replies (rule change) --------------------------------------------


def _call(c):
    return c if c[0] == "delete" else _post(c)


def _old_rule_thread(tmp_path: Path, **fake_kw):
    """A month converged under the old first-scan rule: OP m1, replies m2 (9/1 00:00), m3 (9/2 00:00)."""
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT)
    fake = _FakeSlack(**fake_kw)
    D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake, reply_hour=0)
    assert [c[0] for c in fake.calls] == ["post", "post", "post"]
    fake.calls.clear()
    return root, fake


def test_redo_replies(tmp_path: Path):
    root, fake = _old_rule_thread(tmp_path)
    # dry-run: the plan, nothing posted or deleted
    assert D.redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake) == {
        "old": [("2026-09-01", {"ts": "m2", "scan": "2026-09-01T0000"}), ("2026-09-02", {"ts": "m3", "scan": "2026-09-02T0000"})],
        "new": [("2026-09-01", "2026-09-01T1200", "9/1 — 713 TiB (+8.0, 1.1%)"), ("2026-09-02", "2026-09-02T1200", "9/2 — 715 TiB (+2.0, 0.3%)")],
    }
    assert fake.calls == []
    # for real: OP refreshed, the new replies appended to the same thread, THEN the old ones deleted in order
    state = D.redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake, for_real=True)
    assert [_call(c) for c in fake.calls] == [
        ("edit", "m1"),
        ("post", "m1", "9/1 — 713 TiB (+8.0, 1.1%)", f"{AV}50.png?v=4", None),
        ("post", "m1", "9/2 — 715 TiB (+2.0, 0.3%)", f"{AV}30.png?v=4", None),
        ("delete", "m2"),
        ("delete", "m3"),
    ]
    assert state["posted"] == {"2026-09-01": {"ts": "m4", "scan": "2026-09-01T1200"}, "2026-09-02": {"ts": "m5", "scan": "2026-09-02T1200"}}
    assert "stale" not in state
    assert json.loads((tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").read_text()) == state
    # a second redo is a plain re-thread of the (now current) replies
    fake.calls.clear()
    D.redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake, for_real=True)
    assert [_call(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/1 — 713 TiB (+8.0, 1.1%)", f"{AV}50.png?v=4", None), ("post", "m1", "9/2 — 715 TiB (+2.0, 0.3%)", f"{AV}30.png?v=4", None), ("delete", "m4"), ("delete", "m5")]


def test_redo_replies_post_failure_deletes_nothing(tmp_path: Path):
    # the 2nd new reply fails to post: stop — nothing deleted, the old ts kept as `stale` for a re-run
    root, fake = _old_rule_thread(tmp_path, fail_post_at=5)
    with pytest.raises(RuntimeError, match="slack down"):
        D.redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake, for_real=True)
    assert [_call(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/1 — 713 TiB (+8.0, 1.1%)", f"{AV}50.png?v=4", None)]
    saved = json.loads((tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").read_text())
    assert (saved["stale"], saved["posted"]) == (["m2", "m3"], {"2026-09-01": {"ts": "m4", "scan": "2026-09-01T1200"}})


def test_redo_replies_delete_failure_continues(tmp_path: Path):
    # a delete that fails is logged and kept in `stale`; the rest proceed and the new replies stand
    root, fake = _old_rule_thread(tmp_path, fail_delete={"m2"})
    state = D.redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake, for_real=True)
    assert [c for c in fake.calls if c[0] == "delete"] == [("delete", "m2"), ("delete", "m3")]
    assert state["stale"] == ["m2"]
    assert state["posted"] == {"2026-09-01": {"ts": "m4", "scan": "2026-09-01T1200"}, "2026-09-02": {"ts": "m5", "scan": "2026-09-02T1200"}}



def test_cli_cw_digest_binds_cw_module(tmp_path: Path, monkeypatch):
    """The `cw-digest` CLI must call `cw_digest.post_digest` (which takes
    `variant`/`reply_hour`), not `digest.post_digest`. The wrong binding raised
    `TypeError: post_digest() got multiple values for argument 'site_url'` and
    silently killed the in-job digest (a best-effort step, so the scan job still
    reported success) — no `#cw-s3-usage` post for 2026-09-17."""
    from click.testing import CliRunner

    from dt_cloud import cli

    calls: list[tuple] = []
    monkeypatch.setattr(D, "post_digest", lambda *a, **k: calls.append((a, k)) or {})

    root = tmp_path / "snapshots" / "cw"
    icons = tmp_path / "icons"
    res = CliRunner().invoke(
        cli.main,
        ["cw-digest", "-c", "C1", "-t", "xoxb", "-r", str(root), "-m", "2026-09", "-i", str(icons), "-V", "sender"],
    )
    assert res.exit_code == 0, (res.output, res.exception)
    assert len(calls) == 1
    args, kw = calls[0]
    assert args == (str(root), date(2026, 9, 1), "xoxb", "C1", "sender")
    assert (kw["site_url"], kw["reply_delay"], kw["reply_hour"]) == (D.DEFAULT_URL, 0.0, D.REPLY_HOUR_UTC)
    assert callable(kw["deploy_plot"]) and isinstance(kw["icons_dir"], Path)
