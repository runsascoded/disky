"""Specs for the `cw` digest template (`dt_cloud.digest_cw`, over the engine
`dt_cloud.digest`): the pure helpers, the
framing-A content (OP + both reply variants, exact strings), the daily keying
rule, and the monthly-thread converge against a fake Slack client over a local
snapshot tree.

Hand-built series (12-hourly, 00:00Z / 12:00Z): a lead day Mon 8/31 (700,
705 TiB) then Tue 9/1 (710, 713) and Wed 9/2 (712, 715) — one ISO week, so
the bullet label is unambiguous. Quota (main) = 910 TiB."""
import datetime as dt
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from dt_cloud import digest as E
from dt_cloud import digest_cw as D

from digest_examples import CW_EXAMPLE, config_file

CFG = CW_EXAMPLE
P = CFG.primary


def _tpl(reply_hour: int = 12):
    from dataclasses import replace

    return E.template(replace(CFG, reply_hour=reply_hour))


def post_digest(root, m, token, channel, variant="sender", client=None, reply_hour=12):
    return E.converge_slack(_tpl(reply_hour), root, m, client, channel, variant)


def redo_replies(root, m, token, channel, variant="sender", client=None, reply_hour=12, for_real=False):
    return E.redo_replies(_tpl(reply_hour), root, m, client, channel, variant, for_real=for_real)

TIB = 1024**4
UTC = dt.timezone.utc
SITE = "https://cw.example.org"
AV = "https://icons.example.org/arrows/av_deg"
SEP = date(2026, 9, 1)


def _meta(tot_tib: float, objs: int = 1_000_000) -> dict:
    return {"total_bytes": round(tot_tib * TIB), "total_objects": objs, "class_bytes": {}}


LEAD = [("2026-08-31T0000", _meta(700)), ("2026-08-31T1200", _meta(705))]
SEPT = [("2026-09-01T0000", _meta(710)), ("2026-09-01T1200", _meta(713)), ("2026-09-02T0000", _meta(712)), ("2026-09-02T1200", _meta(715))]
_all = D.rows_from_meta(LEAD + SEPT, P)
MONTH = D.Month(lead=_all[:2], rows=_all[2:])
SITE_TRIGGERS = D.site_triggers  # the real one: conftest stubs the module's attribute per test


def test_quota_config():
    # 910 TiB — CoreWeave's authoritative zone quota (`cwobject_quota_info`), ≈1 PB decimal
    assert (P, CFG.primary_quota) == ("main-data", E.Quota(910 * 1024**4, "1 PB", "1P"))
    assert CFG.buckets["hot-data"] == E.Bucket("hot", E.Quota(100 * 1024**4, "100 TiB", "100Ti"))
    assert (D._quota(715.0, CFG.primary_quota), D._quota(910.0, CFG.primary_quota), D._quota(715.0, None)) == (" · 78.6% of 1 PB", " · 100.0% of 1 PB", "")


def test_deg_projection():
    # a daily reply projects with 168/24 = 7; a 12-hourly interval with 14
    assert [E.deg(p, 7) for p in (0.1, 0.3, 0.5, 1.8, 4.0)] == [10, 30, 40, 60, 70]
    assert [E.deg(p, 14) for p in (0.1, 0.3, 0.5, 1.8)] == [20, 40, 50, 70]
    assert [E.deg(p, 1) for p in (0.4, 1.0, 2.5)] == [10, 20, 30]
    assert (E.deg(-1.8, 7), E.deg(0.0, 1), E.deg(100.0, 1)) == (-60, 0, 80)


def test_scan_ts():
    assert E.scan_ts("2026-09-07T1201") == dt.datetime(2026, 9, 7, 12, 1, tzinfo=UTC)
    assert E.scan_ts("2026-09-07") == dt.datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="not a scan id"):
        E.scan_ts("260907")


def test_formatters_and_links():
    assert [E._tb(v) for v in (3.0, 0.0, -1.25)] == ["+3.0", "+0.0", "−1.2"]
    assert [E._pct(3.0, 903.0), E._pct(-1.0, 902.0), E._pct(5.0, 5.0)] == ["0.3", "0.1", "0.0"]
    assert [E._dlink(s) for s in ("2026-09-07T1201", "2026-09-07")] == ["2609071201", "2609070000"]
    t0 = E.scan_ts("2026-09-06T1200")
    assert [E._span(t0, E.scan_ts(s)) for s in ("2026-09-08T0000", "2026-09-13T1200", "2026-09-07T0000", "2026-09-06T1200", "2026-09-07T1159")] == ["1d12h", "7d", "12h", "0h", "1d"]
    assert D._diff_url("2026-09-02T1200", E.scan_ts("2026-08-31T1200"), SITE) == f"{SITE}/?d=2609021200-2d#over-time"
    assert D._diff_url("2026-09-02T1200", None, SITE) == f"{SITE}/?d=2609021200#over-time"


def test_qlabel_and_bucket_clause():
    assert [D._qlabel(b) for b in (10**15, 10**14, 5 * 10**14, 2 * 10**15)] == ["1P", "100T", "500T", "2P"]
    since = E.scan_ts("2026-09-22T1201")
    # a known-quota bucket → linked `% of quota (free)`; unknown quota → raw TiB
    assert D._bucket_clause("main-data", 825.9, "2026-09-23T1201", since, CFG) == (
        f"[main]({SITE}/main-data?d=2609231201-1d#over-time): 90.8% of 1P (84.1 Ti free)"
    )
    assert D._bucket_clause("hot-data", 82.0, "2026-09-23T1201", since, CFG) == (
        f"[hot]({SITE}/hot-data?d=2609231201-1d#over-time): 82.0% of 100Ti (18.0 Ti free)"
    )
    assert D._bucket_clause("more-data", 1.9, "2026-09-23T1201", None, CFG) == (
        f"[more-data]({SITE}/more-data?d=2609231201#over-time): 2 Ti"
    )


def test_bucket_path_site_root():
    # a single-bucket store whose site serves the bucket at its root (hf.oa.dev: `open-athena` is `/`): `path: ""`
    # links the bucket's clause to the root, not `/<bucket>`; `path` unset keeps `/<bucket>`
    cfg = E.config_from_dict({"template": "cw", "site_url": SITE, "icons_base": "x", "primary": "org", "buckets": {
        "org": {"label": "org", "path": "", "quota": {"bytes": "253 TB", "name": "253 TB", "short": "253T"}},
    }})
    assert cfg.buckets["org"] == E.Bucket("org", E.Quota(253 * 10**12, "253 TB", "253T"), path="")
    assert D._bucket_clause("org", 14.3, "2026-10-16T0800", None, cfg) == (
        f"[org]({SITE}/?d=2610160800#over-time): 6.2% of 253T (215.8 Ti free)"
    )
    with pytest.raises(ValueError) as e:
        E.config_from_dict({"template": "cw", "buckets": {"b": {"path": 1}}})
    assert str(e.value) == "digest config buckets.b.path: expected a string, got 1"


def test_units_tb():
    # `units: TB` (a store billed in decimal TB, as Hugging Face): every size the digest renders is TB (10**12) —
    # rows, headline, weekly bullets, the reply and its tail (`T free`), the image body's points and `units`
    TB = 10**12
    cfg = E.config_from_dict({"template": "cw", "title": "HF usage", "site_url": SITE, "icons_base": "https://icons.example.org",
                              "primary": "org", "units": "TB", "buckets": {
        "org": {"label": "org", "path": "", "quota": {"bytes": "253 TB", "name": "253 TB public", "short": "253T"}},
    }})
    assert (cfg.units, cfg.unit) == ("TB", E.Units("TB", TB, "T"))
    meta = lambda tb: {"total_bytes": round(tb * TB), "total_objects": 9, "class_bytes": {}}  # noqa: E731
    rows = D.rows_from_meta([("2026-09-30T0800", meta(15.0)), ("2026-10-01T0800", meta(15.5)), ("2026-10-02T0800", meta(15.4))], "org", unit=TB)
    assert [r.tb for r in rows] == [15.0, 15.5, 15.4]
    month, oct1 = D.Month(lead=rows[:1], rows=rows[1:]), date(2026, 10, 1)
    assert D.op_body(month, oct1, None, cfg).split("\n") == [
        f":arrow_deg50: **+0.4 TB** [month-to-date]({SITE}/?d=261002-2d#over-time) · 15 TB · 6.1% of 253 TB public · [dashboard]({SITE}/)",
        "_as of 10/2 08:00Z_",
        "",
        "*Weekly summaries*",
        f":arrow_deg30: [wk of 9/28]({SITE}/?d=261002-2d#over-time) _(partial)_: **+0.4 TB** → 15 TB · 6.1% of 253 TB public",
    ]
    assert [D.reply(d, "sender", cfg) for d in D.day_rows(month, "sender", 0)] == [
        E.Reply("10/1 — 16 TB (+0.5, 3.3%)", f"[org]({SITE}/?d=261001-1d#over-time): 6.1% of 253T (237.5 T free)", icon_url="https://icons.example.org/arrows/av_deg60.png?v=4"),
        E.Reply("10/2 — 15 TB (−0.1, 0.6%)", f"[org]({SITE}/?d=261002-1d#over-time): 6.1% of 253T (237.6 T free)", icon_url="https://icons.example.org/arrows/av_deg-40.png?v=4"),
    ]
    assert D.image_body(month, oct1, cfg) == {
        "title": "HF usage — October 2026",
        "note": "cw.example.org · TB",
        "month": "2026-10",
        "points": [{"scan": s, "bytes": b * TB // 10} for s, b in (("2026-09-30T0800", 150), ("2026-10-01T0800", 155), ("2026-10-02T0800", 154))],
        "units": "TB",
        "quota": {"bytes": 253 * TB, "name": "253 TB public"},
        "diff": {"from": "2026-09-30T0800", "to": "2026-10-02T0800", "path": "org", "title": "What changed in org — 9/30 → 10/2"},
    }
    with pytest.raises(ValueError) as e:
        E.config_from_dict({"template": "cw", "units": "Ti"})
    assert str(e.value) == "digest config units: 'Ti' is not one of ('TiB', 'TB')"


def test_class_scoped_quotas():
    # a quota per billed storage class (Hugging Face: public 1 vs 253 TB, private 5 vs 5.1 TB; unbilled git 6 in
    # neither): `quota` as a list, each `classes`-scoped, over the meta's `class_bytes` (JSON keys are strings). The
    # headline and the deltas stay the total; each `% of` clause counts its own classes; the plot's line is the
    # first quota's classes
    TB = 10**12
    cfg = E.config_from_dict({"template": "cw", "title": "HF usage", "site_url": SITE, "icons_base": "https://icons.example.org",
                              "primary": "org", "units": "TB", "buckets": {"org": {"label": "org", "path": "", "quota": [
        {"bytes": "253 TB", "name": "253 TB public", "short": "253T public", "classes": [1]},
        {"bytes": "5.1 TB", "name": "5.1 TB private", "short": "5.1T private", "classes": [5]},
    ]}}})
    assert cfg.buckets["org"] == E.Bucket(
        "org", E.Quota(253 * TB, "253 TB public", "253T public", (1,)), path="",
        more=(E.Quota(5_100_000_000_000, "5.1 TB private", "5.1T private", (5,)),),
    )

    def meta(pub: float, priv: float, git: float) -> dict:
        cb = {"1": round(pub * TB), "5": round(priv * TB), "6": round(git * TB)}
        return {"total_bytes": sum(cb.values()), "total_objects": 9, "class_bytes": cb}

    rows = D.rows_from_meta([("2026-09-30T0800", meta(15.0, 0.02, 0.005)), ("2026-10-01T0800", meta(15.5, 0.5, 0.005))], "org", unit=TB)
    assert [(r.tb, r.classes) for r in rows] == [(15.0, {1: 15.0, 5: 0.02, 6: 0.005}), (16.0, {1: 15.5, 5: 0.5, 6: 0.005})]
    month, oct1 = D.Month(lead=rows[:1], rows=rows[1:]), date(2026, 10, 1)
    assert D.op_body(month, oct1, None, cfg).split("\n") == [
        f":arrow_deg70: **+1.0 TB** [month-to-date]({SITE}/?d=261001-1d#over-time) · 16 TB · 6.1% of 253 TB public · 9.8% of 5.1 TB private · [dashboard]({SITE}/)",
        "_as of 10/1 08:00Z_",
        "",
        "*Weekly summaries*",
        f":arrow_deg50: [wk of 9/28]({SITE}/?d=261001-1d#over-time) _(partial)_: **+1.0 TB** → 16 TB · 6.1% of 253 TB public · 9.8% of 5.1 TB private",
    ]
    assert [D.reply(d, "sender", cfg) for d in D.day_rows(month, "sender", 0)] == [
        E.Reply("10/1 — 16 TB (+1.0, 6.7%)", f"[org]({SITE}/?d=261001-1d#over-time): 6.1% of 253T public (237.5 T free), 9.8% of 5.1T private (4.6 T free)",
                icon_url="https://icons.example.org/arrows/av_deg70.png?v=4"),
    ]
    assert D.image_body(month, oct1, cfg)["points"] == [{"scan": "2026-09-30T0800", "bytes": 15 * TB}, {"scan": "2026-10-01T0800", "bytes": 15_500_000_000_000}]
    # a class-scoped quota over a meta without `class_bytes` refuses rather than counting every byte
    bare = D.rows_from_meta([("2026-10-01T0800", {"total_bytes": TB, "total_objects": 1})], "org", unit=TB)
    with pytest.raises(ValueError) as e:
        D.op_body(D.Month(lead=[], rows=bare), oct1, None, cfg)
    assert str(e.value) == "quota '253 TB public' counts storage classes [1], but the scan's meta.json has no class_bytes"


def test_tail_zone_shared_quota():
    # `hot-data` and `side-data` share zone `z8`'s 100 TiB: one clause, the zone's free;
    # `more-data` (no config) and the primary keep their own clauses, in meta order
    z8 = E.Quota(100 * TIB, "100 TiB", "100Ti")
    cfg = replace(CFG, buckets={
        **CFG.buckets,
        "hot-data": E.Bucket("hot", z8, "z8"),
        "side-data": E.Bucket("side", z8, "z8"),
    })
    day = D.DayRow(
        date="2026-09-23", scan="2026-09-23T1201", tb=825.9, dtb=1.0, hours=24.0,
        since=None, extra={"hot-data": 82.0, "more-data": 1.9, "side-data": 3.0},
    )
    u = lambda b: f"{SITE}/{b}?d=2609231201#over-time"
    assert D._tail(day, cfg) == (
        f"[main]({u('main-data')}): 90.8% of 1P (84.1 Ti free)"
        f" · [hot]({u('hot-data')}) 82 + [side]({u('side-data')}) 3 Ti: 85.0% of 100Ti (15.0 Ti free)"
        f" · [more-data]({u('more-data')}): 2 Ti"
    )


def _meta2(primary_tib: float, hero_tib: float, objs: int = 1_000_000) -> dict:
    """A multi-bucket scan's meta.json (specs/done/cw-multi-bucket.md §2)."""
    return {
        "total_bytes": round((primary_tib + hero_tib) * TIB), "total_objects": objs + 400_000, "class_bytes": {},
        "buckets": {
            "main-data": {"total_bytes": round(primary_tib * TIB), "total_objects": objs},
            "hot-data": {"total_bytes": round(hero_tib * TIB), "total_objects": 400_000},
        },
    }


def test_primary_totals():
    assert D.primary_totals(_meta(700), P) == (700 * TIB, 1_000_000, {})
    assert D.primary_totals(_meta2(700, 89.25), P) == (700 * TIB, 1_000_000, {"hot-data": 89.25})
    # a meta whose `buckets` lacks the primary reads as the flat totals
    assert D.primary_totals({**_meta(5), "buckets": {"x": {"total_bytes": TIB, "total_objects": 1}}}, P) == (5 * TIB, 1_000_000, {})


def test_rows_from_meta_buckets_switch():
    # flat metas then `buckets` metas: the primary's deltas stay continuous
    # across the switch (the flat totals were the primary's); the hot clause's
    # Δ appears once a prior scan has the bucket
    rows = D.rows_from_meta([("2026-09-01T0000", _meta(710)), ("2026-09-01T1200", _meta2(713, 89.25)), ("2026-09-02T0000", _meta2(712, 90.75))], P)
    assert [(r.tb, r.dtb, r.extra) for r in rows] == [(710.0, None, {}), (713.0, 3.0, {"hot-data": 89.2}), (712.0, -1.0, {"hot-data": 90.8})]
    month = D.Month(lead=[], rows=rows)
    d1, d2 = D.day_rows(month, "body", 12)
    assert (d1.extra, d1.dextra) == ({"hot-data": 89.2}, {"hot-data": None})
    assert (d2.extra, d2.dextra) == ({"hot-data": 90.8}, {"hot-data": 1.6})
    assert D.reply(d1, "body", CFG).body == (
        f":arrow_deg0: [9/1]({SITE}/?d=26090112#over-time) — **713 TiB (+0.0, 0.0%)** · "
        f"[main]({SITE}/main-data?d=26090112#over-time): 78.4% of 1P (197.0 Ti free) · "
        f"[hot]({SITE}/hot-data?d=26090112#over-time): 89.2% of 100Ti (10.8 Ti free)"
    )
    assert D.reply(d2, "sender", CFG) == E.Reply(
        "9/2 — 712 TiB (−1.0, 0.1%)",
        f"[main]({SITE}/main-data?d=260902-12h#over-time): 78.2% of 1P (198.0 Ti free) · "
        f"[hot]({SITE}/hot-data?d=260902-12h#over-time): 90.8% of 100Ti (9.2 Ti free)",
        icon_url=f"{AV}-30.png?v=4",
    )
    # the OP headline carries the clause too, Δ vs the month's base scan; each
    # bucket, the primary's total included, linked (as in the replies) over the
    # same month-to-date span
    assert D.op_body(month, SEP, None, CFG).split("\n")[0] == (
        f":arrow_deg30: **+2.0 TiB** [month-to-date]({SITE}/?d=260902-1d#over-time) · [main]({SITE}/main-data?d=260902-1d#over-time) 712 TiB · 78.2% of 1 PB · "
        f"[hot]({SITE}/hot-data?d=260902-1d#over-time) 91 TiB · [dashboard]({SITE}/)"
    )


def _node(n: str, b: float, kids=()) -> dict:
    d = {"n": n, "b": round(b * TIB), "o": 1, "d": 20000}
    if kids:
        d["c"] = list(kids)
    return d


def test_primary_node():
    a, b = _node("main-data", 1), _node("hot-data", 2)
    assert D.primary_node({"n": "root", "c": [b, a]}, P) is a
    assert D.primary_node({"n": "root", "c": [b]}, P) is b  # single-bucket scans: the only child


def test_rows_from_meta_deltas():
    r = MONTH.rows[0]
    assert (r.scan, r.date, r.tb, r.dtb, r.hours) == ("2026-09-01T0000", "2026-09-01", 710.0, 5.0, 12.0)
    assert (MONTH.lead[0].dtb, MONTH.lead[0].hours) == (None, None)
    assert MONTH.base.scan == "2026-08-31T1200"
    assert D.Month(lead=[], rows=_all[2:]).base.scan == "2026-09-01T0000"


def test_day_rows_variants():
    # sender: a day is its MORNING scan (first at/after 12:00Z), Δ vs the prior day's morning scan
    assert D.day_rows(MONTH, "sender", 12) == [
        D.DayRow("2026-09-01", "2026-09-01T1200", 713.0, 8.0, 24.0, E.scan_ts("2026-08-31T1200")),
        D.DayRow("2026-09-02", "2026-09-02T1200", 715.0, 2.0, 24.0, E.scan_ts("2026-09-01T1200")),
    ]
    # a half-landed day (only its 00:00 scan, no later day yet) has no reply yet
    assert [d.date for d in D.day_rows(D.Month(lead=MONTH.lead, rows=MONTH.rows[:3]), "sender", 12)] == ["2026-09-01"]
    # …but once the next day has started, the day's last scan stands in (a missed 12:01Z scan)
    assert D.day_rows(D.Month(lead=MONTH.lead, rows=[MONTH.rows[0], MONTH.rows[2]]), "sender", 12) == [
        D.DayRow("2026-09-01", "2026-09-01T0000", 710.0, 5.0, 12.0, E.scan_ts("2026-08-31T1200")),
    ]
    # `reply_hour=0` is the old first-scan rule
    assert D.day_rows(MONTH, "sender", 0)[0] == D.DayRow("2026-09-01", "2026-09-01T0000", 710.0, 10.0, 24.0, E.scan_ts("2026-08-31T0000"))
    # body: a day is its LAST scan so far, Δ vs the prior day's last
    assert D.day_rows(MONTH, "body", 12) == [
        D.DayRow("2026-09-01", "2026-09-01T1200", 713.0, 8.0, 24.0, E.scan_ts("2026-08-31T1200")),
        D.DayRow("2026-09-02", "2026-09-02T1200", 715.0, 2.0, 24.0, E.scan_ts("2026-09-01T1200")),
    ]
    # a half-landed day on `body`: its reply is that day's 00:00 scan, 12 h after the prior day's last
    half = D.Month(lead=MONTH.lead, rows=MONTH.rows[:3])
    assert D.day_rows(half, "body", 12)[1] == D.DayRow("2026-09-02", "2026-09-02T0000", 712.0, -1.0, 12.0, E.scan_ts("2026-09-01T1200"))
    # first month ever: no prior for day 1
    assert D.day_rows(D.Month(lead=[], rows=MONTH.rows), "sender", 12)[0] == D.DayRow("2026-09-01", "2026-09-01T1200", 713.0, None, None, None)
    with pytest.raises(ValueError, match="variant must be one of"):
        D.day_rows(MONTH, "x", 12)


def test_day_rows_sender_skips_manual_scans():
    # 9/1: a hand-run scan at 12:30Z lands before the 13:00Z cron scan; the cron's is the reply
    rows = D.rows_from_meta([
        *LEAD, ("2026-09-01T1230", _meta(711)), ("2026-09-01T1300", _meta(713)), ("2026-09-02T0000", _meta(712)),
    ], P, triggers={"2026-09-01T1230": "manual", "2026-09-01T1300": "schedule", "2026-09-02T0000": "schedule"})
    month = D.Month(lead=rows[:2], rows=rows[2:])
    assert [r.trigger for r in month.rows] == ["manual", "schedule", "schedule"]
    assert D.day_rows(month, "sender", 12) == [
        D.DayRow("2026-09-01", "2026-09-01T1300", 713.0, 8.0, 25.0, E.scan_ts("2026-08-31T1200")),
    ]
    # a day with only a hand-run scan: no reply while it's open, the scan stands in once the next day starts
    manual = D.Month(lead=rows[:2], rows=[rows[2]])
    assert D.day_rows(manual, "sender", 12) == []
    assert D.day_rows(replace(manual, closed=True), "sender", 12) == [
        D.DayRow("2026-09-01", "2026-09-01T1230", 711.0, 6.0, 24.5, E.scan_ts("2026-08-31T1200")),
    ]
    # scans without a trigger (stamped before triggers existed): the clock alone, as before
    assert D.day_rows(MONTH, "sender", 12)[0].scan == "2026-09-01T1200"


def test_scan_triggers():
    # a scan's trigger is its first run's: a hand resume of a cron scan stays `schedule`; an unrecorded first run, none
    runs = [
        {"scan": "2026-09-01T1300", "started_ts": 200, "trigger": "manual"},
        {"scan": "2026-09-01T1300", "started_ts": 100, "trigger": "schedule"},
        {"scan": "2026-09-01T1230", "started_ts": 90, "trigger": "manual"},
        {"scan": "2026-08-31T1200", "started_ts": 10, "trigger": None},
        {"scan": "2026-08-31T1200", "started_ts": 20, "trigger": "schedule"},
    ]
    assert D.scan_triggers(runs) == {"2026-09-01T1300": "schedule", "2026-09-01T1230": "manual"}


def test_site_triggers(monkeypatch):
    from dt_cloud import site

    calls = []
    res = {"configured": True, "runs": [{"run": {"scan": "2026-09-01T1300", "started_ts": 1, "trigger": "schedule"}}]}
    monkeypatch.setattr(site, "get_json", lambda url, token, path, params=None, timeout=30: calls.append((url, token, path)) or res)
    monkeypatch.setenv("SITE_TOKEN", "tok")
    assert (SITE_TRIGGERS(CFG), calls) == ({"2026-09-01T1300": "schedule"}, [(SITE, "tok", "/api/scan-runs")])
    monkeypatch.setattr(site, "get_json", lambda *a, **kw: {"configured": False, "runs": []})
    assert SITE_TRIGGERS(CFG) == {}
    monkeypatch.delenv("SITE_TOKEN")
    assert SITE_TRIGGERS(CFG) == {}


def test_day_rows_provisional():
    half = D.Month(lead=MONTH.lead, rows=MONTH.rows[:3])  # 9/2 has only its 00:00 scan
    # the still-open day gets a provisional row over its latest scan, Δ vs the prior day's reply scan
    assert D.day_rows(half, "sender", 12, provisional=True) == [
        D.DayRow("2026-09-01", "2026-09-01T1200", 713.0, 8.0, 24.0, E.scan_ts("2026-08-31T1200")),
        D.DayRow("2026-09-02", "2026-09-02T0000", 712.0, -1.0, 12.0, E.scan_ts("2026-09-01T1200"), provisional=True),
    ]
    # a day with its reply scan has none; nor does `body` (its reply is edited intra-day already)
    assert D.day_rows(MONTH, "sender", 12, provisional=True) == D.day_rows(MONTH, "sender", 12)
    assert D.day_rows(half, "body", 12, provisional=True) == D.day_rows(half, "body", 12)
    # a `closed` month (a later month's scan exists): its last day's last scan stands in, no provisional
    assert D.day_rows(D.Month(lead=half.lead, rows=half.rows, closed=True), "sender", 12, provisional=True)[1] == (
        D.DayRow("2026-09-02", "2026-09-02T0000", 712.0, -1.0, 12.0, E.scan_ts("2026-09-01T1200"))
    )


def test_provisional_reply():
    from dataclasses import replace

    half = D.Month(lead=MONTH.lead, rows=MONTH.rows[:3])
    # a fixed neutral sender + icon (both outlive edits); the numbers so far + their scan's time in the body
    assert D.Cw(CFG).provisional(half, "sender") == E.Unit("2026-09-02", "2026-09-02T0000", E.Reply(
        "9/2 · so far",
        f":arrow_deg-30: **712 TiB (−1.0, 0.1%)** · [as of 00:00Z]({SITE}/?d=26090200-12h#over-time) · "
        f"[main]({SITE}/main-data?d=26090200-12h#over-time): 78.2% of 1P (198.0 Ti free)",
        icon_emoji=":hourglass_flowing_sand:",
    ))
    assert (D.Cw(CFG).provisional(MONTH, "sender"), D.Cw(CFG).provisional(half, "body")) == (None, None)
    # `reply_hour=0`: every day's first scan is its reply scan, so never a provisional
    assert D.Cw(replace(CFG, reply_hour=0)).provisional(half, "sender") is None


def test_parse_bytes():
    assert [E.parse_bytes(v) for v in (10**15, "910 TiB", "100Ti", "1 PB", "1.5T", "512")] == [10**15, 910 * 1024**4, 100 * 1024**4, 10**15, 15 * 10**11, 512]
    with pytest.raises(ValueError, match="not a size"):
        E.parse_bytes("lots")


def test_config_file(tmp_path: Path):
    # a deployment's YAML overlays its template's preset; quotas are optional
    f = tmp_path / "digest.yml"
    f.write_text("""
template: cw
title: Lab usage
site_url: https://lab.example
primary: lab-main
buckets:
  lab-main: {label: main, quota: {bytes: 2 PB, name: 2 PB, short: 2P}}
  lab-scratch: {label: scratch}
""")
    cfg = E.load_config("cw", f)
    assert (cfg.template, cfg.title, cfg.site_url, cfg.primary, cfg.state) == ("cw", "Lab usage", "https://lab.example", "lab-main", E.PRESETS["cw"].state)
    assert cfg.buckets == {"lab-main": E.Bucket("main", E.Quota(2 * 10**15, "2 PB", "2P")), "lab-scratch": E.Bucket("scratch")}
    assert E.load_config("gcs") is E.PRESETS["gcs"]
    with pytest.raises(ValueError, match="unknown digest config keys: \\['colour'\\]"):
        E.config_from_dict({"template": "cw", "colour": "red"})


@pytest.mark.parametrize("d, msg", [
    ({"colour": "red", "title": "x"}, "unknown digest config keys: ['colour']"),
    ({"buckets": {"b": {"label": "b", "qouta": {}}}}, "unknown digest config keys in buckets.b: ['qouta']"),
    ({"buckets": {"b": {"quota": {"bytes": "1 PB", "name": "1 PB", "shrt": "1P"}}}}, "unknown digest config keys in buckets.b.quota: ['shrt']"),
    ({"buckets": {"b": {"quota": {"bytes": "1 PB"}}}}, "digest config buckets.b.quota: missing ['name']"),
    ({"buckets": {"b": {"label": 2}}}, "digest config buckets.b.label: expected a string, got 2"),
    ({"buckets": {"b": {"zone": 8}}}, "digest config buckets.b.zone: expected a string, got 8"),
    (
        {"buckets": {"a": {"zone": "z", "quota": {"bytes": "1 PB", "name": "1 PB"}}, "b": {"zone": "z"}}},
        "digest config buckets.b: zone 'z' quota differs from buckets.a's",
    ),
    ({"buckets": ["b"]}, "digest config buckets: expected a mapping, got list"),
    ({"buckets": {"b": {"quota": {"bytes": "lots", "name": "x"}}}}, "not a size: 'lots'"),
    ({"buckets": {"b": {"quota": []}}}, "digest config buckets.b.quota: an empty list"),
    ({"buckets": {"b": {"quota": [{"bytes": "1 PB", "name": "x", "classes": "1"}]}}}, "digest config buckets.b.quota[0].classes: expected a list of storage-class ids, got '1'"),
    ({"buckets": {"b": {"zone": "z", "quota": {"bytes": "1 PB", "name": "x", "classes": [1]}}}}, "digest config buckets.b: a zone's quota is one quota over every class"),
    ({"prices": {"1": "cheap"}}, "digest config prices: expected numbers, got {'1': 'cheap'}"),
    ({"reply_hour": "12"}, "digest config reply_hour: expected int, got '12'"),
    ({"reply_hour": True}, "digest config reply_hour: expected int, got True"),
    ({"reply_hour": 24}, "digest config reply_hour: 24 is not a UTC hour (0–23)"),
    ({"primary": 7}, "digest config primary: expected str or null, got 7"),
    ({"provisional": "yes"}, "digest config provisional: expected bool, got 'yes'"),
    ({"template": "s3"}, "digest config template: 's3' is not one of ('gcs', 'cw')"),
])
def test_config_invalid(d: dict, msg: str):
    with pytest.raises(ValueError) as e:
        E.config_from_dict({"template": "cw", **d})
    assert str(e.value) == msg


def test_config_file_invalid(tmp_path: Path):
    f = tmp_path / "digest.yml"
    f.write_text("- template: cw\n")
    with pytest.raises(ValueError) as e:
        E.load_config("cw", f)
    assert str(e.value) == f"digest config {f}: expected a mapping, got list"
    # nulls clear optional fields; an empty file is the preset
    f.write_text("primary: null\ndiscord_webhook_env: null\n")
    assert E.load_config("cw", f) == replace(E.PRESETS["cw"], primary=None)
    f.write_text("")
    assert E.load_config("cw", f) == E.PRESETS["cw"]


def test_no_quota():
    # no quota on the primary (or any bucket): the `% of quota` clauses drop out, the tail shows raw TiB
    from dataclasses import replace

    cfg = replace(CFG, buckets={})
    assert D.op_body(MONTH, SEP, None, cfg).split("\n") == [
        f":arrow_deg40: **+10.0 TiB** [month-to-date]({SITE}/?d=26090212-2d#over-time) · 715 TiB · [dashboard]({SITE}/)",
        "_as of 9/2 12:00Z_",
        "",
        "*Weekly summaries*",
        f":arrow_deg20: [wk of 8/31]({SITE}/?d=26090212-2d#over-time) _(partial)_: **+10.0 TiB** → 715 TiB",
    ]
    assert D.reply(D.day_rows(MONTH, "sender", 12)[0], "sender", cfg).body == f"[main-data]({SITE}/main-data?d=26090112-1d#over-time): 713 Ti"


def test_op_body():
    # month-to-date +10.0 on 705 over 2 days → 1.42%·3.5 = 5.0%/wk → deg40;
    # the (partial) week +10.0 → 1.4% → deg20, linked over 8/31 12:00 → 9/2 12:00 = 2d
    assert D.op_body(MONTH, SEP, "https://x/p.png", CFG).split("\n") == [
        f":arrow_deg40: **+10.0 TiB** [month-to-date]({SITE}/?d=26090212-2d#over-time) · 715 TiB · 78.6% of 1 PB · [dashboard]({SITE}/)",
        "_as of 9/2 12:00Z_",
        "",
        "*Weekly summaries*",
        f":arrow_deg20: [wk of 8/31]({SITE}/?d=26090212-2d#over-time) _(partial)_: **+10.0 TiB** → 715 TiB · 78.6% of 1 PB",
        "",
        "![S3 usage — September 2026](https://x/p.png)",
    ]
    assert D.op_body(MONTH, SEP, None, CFG).split("\n")[-1].startswith(":arrow_deg20: [wk of 8/31]")


def test_op_body_two_weeks():
    # a completed week (Sunday scanned) is not partial; the next week's bullet is Δ vs that week's end
    metas = LEAD + [(f"2026-09-{d:02d}T{h}", _meta(700 + i)) for i, (d, h) in enumerate(((d, h) for d in range(1, 8) for h in ("0000", "1200")), start=1)]
    rows = D.rows_from_meta(metas, P)
    month = D.Month(lead=rows[:2], rows=rows[2:])
    bullets = D.op_body(month, SEP, None, CFG).split("\n")[4:]
    assert bullets == [
        f":arrow_deg20: [wk of 8/31]({SITE}/?d=26090612-6d#over-time): **+7.0 TiB** → 712 TiB · 78.2% of 1 PB",
        f":arrow_deg0: [wk of 9/7]({SITE}/?d=26090712-1d#over-time) _(partial)_: **+2.0 TiB** → 714 TiB · 78.5% of 1 PB",
    ]


def test_reply_sender_variant():
    d1, d2 = D.day_rows(MONTH, "sender", 12)
    # +8.0 on 705 in 24 h → 1.13%·7 = 7.9%/wk → deg50; +2.0 on 713 → 0.28%·7 = 2.0% → deg30
    assert D.reply(d1, "sender", CFG) == E.Reply(
        "9/1 — 713 TiB (+8.0, 1.1%)",
        f"[main]({SITE}/main-data?d=26090112-1d#over-time): 78.4% of 1P (197.0 Ti free)",
        icon_url=f"{AV}50.png?v=4",
    )
    assert D.reply(d2, "sender", CFG) == E.Reply(
        "9/2 — 715 TiB (+2.0, 0.3%)",
        f"[main]({SITE}/main-data?d=26090212-1d#over-time): 78.6% of 1P (195.0 Ti free)",
        icon_url=f"{AV}30.png?v=4",
    )


def test_reply_body_variant():
    d1, d2 = D.day_rows(MONTH, "body", 12)
    # +8.0 on 705 in 24 h → 1.13%·7 = 7.9%/wk → deg50; +2.0 on 713 → deg30
    assert D.reply(d1, "body", CFG) == E.Reply(
        "S3 usage",
        f":arrow_deg50: [9/1]({SITE}/?d=26090112-1d#over-time) — **713 TiB (+8.0, 1.1%)** · "
        f"[main]({SITE}/main-data?d=26090112-1d#over-time): 78.4% of 1P (197.0 Ti free)",
        icon_emoji=":calendar:",
    )
    assert D.reply(d2, "body", CFG) == E.Reply(
        "S3 usage",
        f":arrow_deg30: [9/2]({SITE}/?d=26090212-1d#over-time) — **715 TiB (+2.0, 0.3%)** · "
        f"[main]({SITE}/main-data?d=26090212-1d#over-time): 78.6% of 1P (195.0 Ti free)",
        icon_emoji=":calendar:",
    )


def test_reply_first_day_ever():
    # no prior scan: zero delta, flat arrow, link without a look-back
    day = D.day_rows(D.Month(lead=[], rows=MONTH.rows[:2]), "sender", 12)[0]
    assert D.reply(day, "sender", CFG) == E.Reply("9/1 — 713 TiB (+0.0, 0.0%)", f"[main]({SITE}/main-data?d=26090112#over-time): 78.4% of 1P (197.0 Ti free)", icon_url=f"{AV}0.png?v=4")


def test_state_path():
    # namespaced under cw/, keyed by channel AND variant: staging A/B and prod never share a thread
    assert E.state_path("gs://b/snapshots/cw", SEP, CFG.state.format(channel="C1", variant="sender")) == "gs://b/digest/cw/C1/sender/2026-09.json"


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
    month = D.load_month(str(root), SEP, P)
    # lead = every scan of the last pre-month day (not 8/30); rows = the in-month scans
    assert [r.scan for r in month.lead] == ["2026-08-31T0000", "2026-08-31T1200"]
    assert [r.scan for r in month.rows] == [s for s, _ in SEPT]
    assert (month.lead[0].dtb, month.rows[0].dtb, month.rows[0].hours) == (None, 5.0, 12.0)  # deltas only within the loaded window
    assert D.load_month(str(root), date(2026, 7, 1), P) is None
    _publish(tmp_path / "fresh", SEPT)
    assert D.load_month(str(tmp_path / "fresh"), SEP, P).lead == []


def _post(c):
    return (c[0], c[2], c[3], c[4], c[5]) if c[0] == "post" else c[:2]


def test_post_digest_sender_variant(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT[:1])
    fake = _FakeSlack()
    # 9/1 00:00 lands: the OP under the month sender — no reply yet (the morning scan is still to come)
    state = post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    plot = state["plot_name"]
    assert plot.startswith("plot-") and plot.endswith(".png")
    assert [_post(c) for c in fake.calls] == [("post", None, "S3 usage — September 2026", None, ":calendar:")]
    assert fake.calls[0][1].startswith(":arrow_deg") and f"https://cw.icons.example.org/{plot}?v=" in fake.calls[0][1]
    assert state == {"plot_name": plot, "variant": "sender", "op_ts": "m1", "posted": {}}

    # 9/1 12:00 lands: OP refreshed + the day's reply (headline as sender, arrow avatar)
    _publish(root, SEPT[1:2])
    fake.calls.clear()
    state = post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/1 — 713 TiB (+8.0, 1.1%)", f"{AV}50.png?v=4", None)]
    assert state["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T1200"}}
    assert json.loads((tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").read_text()) == state

    # 9/2 00:00 lands: only the OP is refreshed — no reply, no edit
    _publish(root, SEPT[2:3])
    fake.calls.clear()
    state = post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1")]
    assert state["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T1200"}}

    # 9/2 12:00 lands: the new day's reply
    _publish(root, SEPT[3:4])
    fake.calls.clear()
    state = post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/2 — 715 TiB (+2.0, 0.3%)", f"{AV}30.png?v=4", None)]
    assert state["posted"]["2026-09-02"] == {"ts": "m3", "scan": "2026-09-02T1200"}
    assert state["plot_name"] == plot


def test_post_digest_body_variant(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT[:1])
    fake = _FakeSlack()
    # 9/1 00:00: OP + the day's reply under the static sender, headline in the body (Δ vs 8/31's last = 12 h)
    post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert [_post(c) for c in fake.calls] == [
        ("post", None, "S3 usage — September 2026", None, ":calendar:"),
        ("post", "m1", "S3 usage", None, ":calendar:"),
    ]
    assert fake.calls[1][1] == f":arrow_deg50: [9/1]({SITE}/?d=260901-12h#over-time) — **710 TiB (+5.0, 0.7%)** · [main]({SITE}/main-data?d=260901-12h#over-time): 78.0% of 1P (200.0 Ti free)"

    # 9/1 12:00 lands: the OP AND the day's reply are edited to the latest scan (now a 24 h Δ)
    _publish(root, SEPT[1:2])
    fake.calls.clear()
    state = post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert fake.calls[0][:2] == ("edit", "m1")
    assert fake.calls[1] == ("edit", "m2", f":arrow_deg50: [9/1]({SITE}/?d=26090112-1d#over-time) — **713 TiB (+8.0, 1.1%)** · [main]({SITE}/main-data?d=26090112-1d#over-time): 78.4% of 1P (197.0 Ti free)")
    assert state["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T1200"}}

    # same scans again: nothing but the OP refresh (the reply already reflects the latest scan)
    fake.calls.clear()
    post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1")]

    # both variants in one channel keep separate threads/state
    other = _FakeSlack()
    post_digest(str(root), SEP, "xoxb", "C1", "sender", client=other)
    assert [c[0] for c in other.calls] == ["post", "post"]
    assert (tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").exists()


def test_post_digest_empty_month(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT)
    fake = _FakeSlack()
    assert post_digest(str(root), date(2026, 10, 1), "xoxb", "C1", client=fake) == {}
    assert fake.calls == []


# ---- redo replies (rule change) --------------------------------------------


def _call(c):
    return c if c[0] == "delete" else _post(c)


def _old_rule_thread(tmp_path: Path, **fake_kw):
    """A month converged under the old first-scan rule: OP m1, replies m2 (9/1 00:00), m3 (9/2 00:00)."""
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT)
    fake = _FakeSlack(**fake_kw)
    post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake, reply_hour=0)
    assert [c[0] for c in fake.calls] == ["post", "post", "post"]
    fake.calls.clear()
    return root, fake


def test_redo_replies(tmp_path: Path):
    root, fake = _old_rule_thread(tmp_path)
    # dry-run: the plan, nothing posted or deleted
    assert redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake) == {
        "old": [("2026-09-01", {"ts": "m2", "scan": "2026-09-01T0000"}), ("2026-09-02", {"ts": "m3", "scan": "2026-09-02T0000"})],
        "new": [("2026-09-01", "2026-09-01T1200", "9/1 — 713 TiB (+8.0, 1.1%)"), ("2026-09-02", "2026-09-02T1200", "9/2 — 715 TiB (+2.0, 0.3%)")],
    }
    assert fake.calls == []
    # for real: OP refreshed, the new replies appended to the same thread, THEN the old ones deleted in order
    state = redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake, for_real=True)
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
    redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake, for_real=True)
    assert [_call(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/1 — 713 TiB (+8.0, 1.1%)", f"{AV}50.png?v=4", None), ("post", "m1", "9/2 — 715 TiB (+2.0, 0.3%)", f"{AV}30.png?v=4", None), ("delete", "m4"), ("delete", "m5")]


def test_redo_replies_post_failure_deletes_nothing(tmp_path: Path):
    # the 2nd new reply fails to post: stop — nothing deleted, the old ts kept as `stale` for a re-run
    root, fake = _old_rule_thread(tmp_path, fail_post_at=5)
    with pytest.raises(RuntimeError, match="slack down"):
        redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake, for_real=True)
    assert [_call(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/1 — 713 TiB (+8.0, 1.1%)", f"{AV}50.png?v=4", None)]
    saved = json.loads((tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").read_text())
    assert (saved["stale"], saved["posted"]) == (["m2", "m3"], {"2026-09-01": {"ts": "m4", "scan": "2026-09-01T1200"}})


def test_redo_replies_delete_failure_continues(tmp_path: Path):
    # a delete that fails is logged and kept in `stale`; the rest proceed and the new replies stand
    root, fake = _old_rule_thread(tmp_path, fail_delete={"m2"})
    state = redo_replies(str(root), SEP, "xoxb", "C1", "sender", client=fake, for_real=True)
    assert [c for c in fake.calls if c[0] == "delete"] == [("delete", "m2"), ("delete", "m3")]
    assert state["stale"] == ["m2"]
    assert state["posted"] == {"2026-09-01": {"ts": "m4", "scan": "2026-09-01T1200"}, "2026-09-02": {"ts": "m5", "scan": "2026-09-02T1200"}}



def test_cli_cw_digest_binds_cw_template(tmp_path: Path, monkeypatch):
    """The `cw-digest` CLI must converge with the `cw` template + its config (the
    variant, the morning reply hour, cw's icons dir). A wrong binding once
    raised `TypeError: post_digest() got multiple values for argument
    'site_url'` and silently killed the in-job digest (a best-effort step, so
    the scan job still reported success) — no `#cw-s3-usage` post for 2026-09-17."""
    from click.testing import CliRunner

    from dt_cloud import cli

    calls: list[tuple] = []
    monkeypatch.setattr(E, "converge_slack", lambda *a, **k: calls.append((a, k)) or {})

    root = tmp_path / "snapshots" / "cw"
    icons = tmp_path / "icons"
    res = CliRunner().invoke(
        cli.main,
        ["cw-digest", "-C", str(config_file(CFG, tmp_path / "digest.yml")), "-c", "C1", "-t", "xoxb", "-r", str(root), "-m", "2026-09", "-i", str(icons), "-V", "sender"],
    )
    assert res.exit_code == 0, (res.output, res.exception)
    assert len(calls) == 1
    (tpl, root_, m, _client, channel, variant), kw = calls[0]
    assert (type(tpl).__name__, tpl.cfg, root_, m, channel, variant) == ("Cw", CFG, str(root), date(2026, 9, 1), "C1", "sender")
    assert (kw["icons_dir"], kw["reply_delay"]) == (icons, 0.0)
    assert callable(kw["deploy_plot"])


def test_image_body():
    # the site draws the OP image: every scan from the lead-in on, the primary's quota, the headline interval's diff
    assert D.image_body(MONTH, SEP, CFG) == {
        "title": "S3 usage — September 2026",
        "note": "cw.example.org · TiB",
        "month": "2026-09",
        "points": [
            {"scan": "2026-08-31T1200", "bytes": 705 * TIB},
            {"scan": "2026-09-01T0000", "bytes": 710 * TIB},
            {"scan": "2026-09-01T1200", "bytes": 713 * TIB},
            {"scan": "2026-09-02T0000", "bytes": 712 * TIB},
            {"scan": "2026-09-02T1200", "bytes": 715 * TIB},
        ],
        "units": "TiB",
        "quota": {"bytes": 910 * TIB, "name": "1 PB"},
        "diff": {"from": "2026-08-31T1200", "to": "2026-09-02T1200", "path": "main-data", "title": "What changed in main-data — 8/31 → 9/2"},
    }


def test_image_body_first_scan():
    # a month whose only scan is its first (no lead-in): one point, no diff
    one = D.Month(lead=[], rows=MONTH.rows[:1])
    assert D.image_body(one, SEP, CFG) == {
        "title": "S3 usage — September 2026",
        "note": "cw.example.org · TiB",
        "month": "2026-09",
        "points": [{"scan": "2026-09-01T0000", "bytes": 710 * TIB}],
        "units": "TiB",
        "quota": {"bytes": 910 * TIB, "name": "1 PB"},
    }


def test_render_plot_posts_to_site(monkeypatch, tmp_path: Path):
    from dt_cloud import site

    calls = []
    monkeypatch.setenv("SITE_TOKEN", "tok")
    monkeypatch.setattr(site, "post_json", lambda url, token, path, body, timeout=120: calls.append((url, token, path, body)) or b"\x89PNG")
    D.Cw(CFG).render_plot(MONTH, SEP, tmp_path / "op.png")
    assert calls == [(SITE, "tok", "/api/digest-image", D.image_body(MONTH, SEP, CFG))]
    assert (tmp_path / "op.png").read_bytes() == b"\x89PNG"


def test_op_sender():
    """The OP's sender icon: `op_icon` as a URL, or a file under `icons_base` (the deployment's mark); unset, 📆."""
    base = {"template": "cw", "title": "t", "site_url": SITE, "icons_base": "https://icons.example.org"}
    assert [E.config_from_dict({**base, **extra}).op_sender() for extra in ({}, {"op_icon": "mark.png"}, {"op_icon": "https://x.example/m.png"})] == [
        {"icon_emoji": ":calendar:"}, {"icon_url": "https://icons.example.org/mark.png"}, {"icon_url": "https://x.example/m.png"},
    ]


def test_icons_dir_resolves_beside_the_config_from_a_task_work_dir(tmp_path, monkeypatch):
    """A Nextflow step runs in its task work dir, not /app: the config's relative `icons_dir` resolves against the
    config's repo root (`<app>/job/digest.yml` → `<app>/job/icons-x`), ahead of the cwd (hf's first NF scan: no digest)."""
    from dt_cloud.cli import _icons_dir

    app, work = tmp_path / "app", tmp_path / "work" / "ab" / "cd12"
    (app / "job" / "icons-x").mkdir(parents=True)
    (app / "job" / "digest.yml").write_text("{}\n")
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    assert (_icons_dir("job/icons-x", app / "job" / "digest.yml"), _icons_dir("/abs/icons", app / "job" / "digest.yml")) == (
        app / "job" / "icons-x", Path("/abs/icons"))
