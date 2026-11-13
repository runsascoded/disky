"""The digest replies' goldens: each case under `tests/fixtures/digest/replies/` holds a month's `cfg`, `rows` and
`reply` as the preview bundle carries them and the replies the job renders from them (`DT_UPDATE_GOLDEN=1` rewrites
them), which `site/src/digest/replyParity.test.ts` must answer byte for byte."""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from dt_cloud import digest as E
from dt_cloud import digest_cw as DC

from digest_examples import CW_EXAMPLE

TIB = 1024**4
GOLDEN = Path(__file__).parent / "fixtures" / "digest" / "replies"
ZONE = E.Quota(100 * TIB, "100 TiB", "100Ti")
CFG = replace(CW_EXAMPLE, provisional=True, alert_pct=90, op_icon="mark.png", buckets={
    "main-data": E.Bucket("main", E.Quota(910 * TIB, "1 PB", "1P")),
    "hot-data": E.Bucket("hot", E.Quota(100 * TIB, "100 TiB", "100Ti"), shape="square"),
    "z-a": E.Bucket("za", ZONE, "z8", shape="star"),
    "z-b": E.Bucket("zb", ZONE, "z8", shape="star"),
})
SOLO = replace(CW_EXAMPLE, buckets={"main-data": E.Bucket("main", E.Quota(910 * TIB, "1 PB", "1P"))})
#: A scan a day at 00, 06, 12 and 18Z from 10/28 to 11/5's 06Z: November's reply scans, its open day's provisional one;
#: 11/2's 12Z is by hand, so its 18Z replies; 11/3 has no scan past 06Z, which stands in; `new-data` joins on 11/4.
SCANS = [f"{d}T{h}01" for d in (f"2026-{m}" for m in ("10-28", "10-29", "10-30", "10-31", "11-01", "11-02", "11-03", "11-04"))
         for h in ("00", "06", "12", "18") if not (d == "2026-11-03" and h in ("12", "18"))] + ["2026-11-05T0001", "2026-11-05T0601"]
TRIGGERS = {"2026-11-02T1201": "manual"}


def _meta(i: int, scan: str) -> dict:
    sizes = {"main-data": 700 + 2.5 * i, "hot-data": 99.2 + 0.1 * (i % 9), "z-a": max(0.0, 60 - 2.25 * i), "z-b": 3 + 1.5 * i}
    if scan >= "2026-11-04":
        sizes["new-data"] = 4.25
    return {"total_bytes": round(sum(sizes.values()) * TIB), "total_objects": 1,
            "buckets": {b: {"total_bytes": round(v * TIB), "total_objects": 1} for b, v in sizes.items()}}


def _month(primary: str, solo: bool = False, closed: bool = False) -> DC.Month:
    metas = [(s, {"total_bytes": round((700 + 2.5 * i) * TIB), "total_objects": 1} if solo else _meta(i, s)) for i, s in enumerate(SCANS)]
    rows = DC.rows_from_meta(metas, primary, now=dt.datetime(2026, 11, 5, 7, tzinfo=dt.timezone.utc), triggers=TRIGGERS)
    pre = [r for r in rows if r.date < "2026-10-31"]
    lead = [r for r in rows if r.date == "2026-10-31"]
    return DC.Month(lead=lead, rows=[r for r in rows if r.date >= "2026-11-01"], closed=closed, pre=pre)


CASES = {
    **{f"{m}-sender": (replace(CFG, quota_marks=m), "sender", False) for m in ("warning", "circles", "runway", "runway-hue")},
    "runway-total-sender": (replace(CFG, quota_marks="runway", total_headline=True), "sender", False),
    "runway-closed-sender": (replace(CFG, quota_marks="runway"), "sender", True),
    "warning-body": (CFG, "body", False),
    "solo-sender": (SOLO, "sender", False),
}


def golden(name: str) -> dict:
    cfg, variant, closed = CASES[name]
    data = _month(cfg.primary, solo=cfg is SOLO, closed=closed)
    tpl = E.template(cfg)
    return json.loads(json.dumps({"cfg": E.cfg_json(cfg), "rows": tpl.rows_json(data), "reply": tpl.reply_json(data), "variant": variant,
                                  "replies": E.replies_json(tpl, data, variant)}))


@pytest.mark.parametrize("name", CASES)
def test_reply_golden(name: str):
    path = GOLDEN / f"{name}.json"
    actual = golden(name)
    if os.environ.get("DT_UPDATE_GOLDEN"):
        path.write_text(json.dumps(actual, indent=1, ensure_ascii=False) + "\n")
    assert json.loads(path.read_text()) == actual


def test_every_golden_is_a_case():
    assert sorted(p.stem for p in GOLDEN.glob("*.json")) == sorted(CASES)
