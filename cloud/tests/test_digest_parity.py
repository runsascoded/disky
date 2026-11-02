"""The site's OP renderer (`site/src/digest`) against the job's: each case here renders a month's OP text and plot
request in Python and pins them, with the rows and config they came from, as a JSON fixture under
`tests/fixtures/digest/parity/` (`DT_UPDATE_GOLDEN=1` rewrites). `site/src/digest/parity.test.ts` renders each
fixture's rows in TS and asserts the same text and request, byte for byte (specs/slack-preview-page.md, phase 1)."""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from dt_cloud import digest as E
from dt_cloud import digest_cw as DC

from digest_examples import CW_EXAMPLE, GCS_EXAMPLE
from test_digest_golden import CW6_SCANS, cw_root, gcs_root

TIB = 1024**4
PARITY = Path(__file__).parent / "fixtures" / "digest" / "parity"
PLOT, PREV = "<plot-url>", "<prev-thread>"


def _daily(root: Path, first: date, last: date, meta) -> None:
    for i in range((last - first).days + 1):
        d = first + dt.timedelta(days=i)
        (root / f"{d}T0800").mkdir(parents=True)
        (root / f"{d}T0800" / "meta.json").write_text(json.dumps(meta(i)))


def _gcs_daily(root: Path, first: date, last: date) -> None:
    for i in range((last - first).days + 1):
        d, t = first + dt.timedelta(days=i), 3000 + 2 * i
        (root / f"{d}").mkdir(parents=True)
        cb = {"1": (t - 2700) * TIB, "2": 600 * TIB, "3": 1500 * TIB, "4": 600 * TIB}
        (root / f"{d}" / "meta.json").write_text(json.dumps({"total_bytes": t * TIB, "class_bytes": cb}))


#: A decimal-TB store with class-scoped quotas, served at the site root (hf's shape).
TB_CFG = replace(
    CW_EXAMPLE, title="HF usage", units="TB", primary="org",
    buckets={"org": E.Bucket("org", E.Quota(253 * 10**12, "253 TB public", "253T", (1,)), path="",
                             more=(E.Quota(5_100_000_000_000, "5.1 TB private", "5.1T", (5,)),))},
)


def _tb_meta(i: int) -> dict:
    pub, priv, git = 15.0 + 0.137 * i, 0.4 + 0.0731 * i, 1.2
    return {"total_bytes": round((pub + priv + git) * 10**12), "total_objects": 1,
            "class_bytes": {"1": round(pub * 10**12), "5": round(priv * 10**12), "6": round(git * 10**12)}}


def _case(name: str, tmp_path: Path):
    """``(config, snapshot root, month, plot url, prev url)`` for case ``name``."""
    if name == "gcs-aug":
        return GCS_EXAMPLE, gcs_root(tmp_path), date(2026, 8, 1), PLOT, PREV
    if name == "gcs-aug-discord":
        return GCS_EXAMPLE, gcs_root(tmp_path), date(2026, 8, 1), None, None
    if name == "gcs-first":
        _gcs_daily(tmp_path / "snapshots", date(2026, 7, 27), date(2026, 9, 1))
        return GCS_EXAMPLE, tmp_path / "snapshots", date(2026, 9, 1), PLOT, PREV
    if name == "cw-sep":
        return CW_EXAMPLE, cw_root(tmp_path), date(2026, 9, 1), PLOT, PREV
    if name == "cw6-oct":
        return CW_EXAMPLE, cw_root(tmp_path, scans=CW6_SCANS), date(2026, 10, 1), PLOT, None
    if name == "cw-first":
        root = tmp_path / "snapshots" / "cw"
        _daily(root, date(2026, 7, 27), date(2026, 9, 1), lambda i: {"total_bytes": (700 + i) * TIB, "total_objects": 1, "class_bytes": {}})
        return CW_EXAMPLE, root, date(2026, 9, 1), PLOT, PREV
    if name == "cw-tb":
        root = tmp_path / "snapshots" / "hf"
        _daily(root, date(2026, 9, 20), date(2026, 10, 9), _tb_meta)
        return TB_CFG, root, date(2026, 10, 1), PLOT, PREV
    raise ValueError(name)


CASES = ["gcs-aug", "gcs-aug-discord", "gcs-first", "cw-sep", "cw6-oct", "cw-first", "cw-tb"]


@pytest.mark.parametrize("name", CASES)
def test_parity_fixture(name: str, tmp_path: Path):
    cfg, root, month, plot_url, prev_url = _case(name, tmp_path)
    tpl = E.template(cfg)
    data = tpl.load(str(root), month)
    actual = {
        "cfg": E.cfg_json(cfg), "month": f"{month:%Y-%m}", "plot_url": plot_url, "prev_url": prev_url,
        "rows": tpl.rows_json(data), "op": tpl.op_body(data, month, plot_url, prev_url), "image": tpl.image_request(data, month),
    }
    path = PARITY / f"{name}.json"
    text = json.dumps(actual, indent=1, ensure_ascii=False) + "\n"
    if os.environ.get("DT_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert text == path.read_text()


def test_preview_bundle_is_the_parity_shape():
    # the bundle the job writes carries the same fields the parity fixtures pin (plus the thread's)
    tpl = E.template(CW_EXAMPLE)
    rows = DC.rows_from_meta([("2026-09-01T0800", {"total_bytes": 700 * TIB, "total_objects": 1, "class_bytes": {}})], CW_EXAMPLE.primary)
    data = DC.Month(lead=[], rows=rows)
    b = E.preview_bundle(tpl, data, date(2026, 9, 1), op="op", plot_url=PLOT, prev_url=None, channel="C1", variant="sender",
                         op_ts="m1", permalink="https://slack.example.org/p1")
    assert sorted(b) == ["cfg", "channel", "image", "month", "op", "op_ts", "params", "permalink", "plot_url", "posted_at",
                         "prev_url", "replies", "rows", "variant", "version"]
    assert (b["version"], b["month"], b["rows"]["template"], b["image"]["month"], b["params"]) == (1, "2026-09", "cw", "2026-09", "")


def test_converge_writes_the_preview_bundle(tmp_path: Path):
    # with `preview` set, each Slack converge writes the month's bundle there: the OP as posted, the thread, and
    # what the site needs to re-render it; without it, nothing
    from test_digest_golden import FakeSlack

    root = cw_root(tmp_path)
    cfg = replace(CW_EXAMPLE, preview=str(tmp_path / "served" / "digest" / "preview" / "{channel}" / "{variant}"))
    log: list = []
    E.converge_slack(E.template(cfg), str(root), date(2026, 9, 1), FakeSlack(log), "C1", "sender")
    b = json.loads((tmp_path / "served" / "digest" / "preview" / "C1" / "sender" / "2026-09.json").read_text())
    op = next(c for c in log if c["op"] == "post" and c["thread_id"] is None)
    assert (b["op"], b["op_ts"], b["permalink"], b["channel"], b["variant"], b["rows"]["template"]) == (
        op["content"], "m1", "https://slack.example.org/archives/C1/pm1", "C1", "sender", "cw",
    )
    # the replies as posted (the thread's, oldest first), newest first
    posted = [(c["username"], c["content"], c["icon_url"], c["icon_emoji"]) for c in log if c["op"] == "post" and c["thread_id"] == "m1"]
    assert [(r["username"], r["body"], r["icon_url"], r["icon_emoji"]) for r in reversed(b["replies"])] == posted
    E.converge_slack(E.template(CW_EXAMPLE), str(cw_root(tmp_path / "bare")), date(2026, 9, 1), FakeSlack([]), "C1", "sender")
    assert not (tmp_path / "bare" / "served").exists()


@pytest.mark.parametrize("name", ["cw-sep", "gcs-aug"])
def test_site_renderer_posts_the_rows_and_uses_the_answer(name: str, tmp_path: Path, monkeypatch):
    """`renderer: site` (phase 2): the OP text and plot request come from `POST /api/digest-op`, sent exactly the
    parity fixture's request under the config's params; the site's answer here is the fixture's own (what
    `digest-op.test.ts` asserts the site returns for it)."""
    from dt_cloud import site

    cfg, root, month, plot_url, prev_url = _case(name, tmp_path)
    fx = json.loads((PARITY / f"{name}.json").read_text())
    calls: list = []

    def post_json(url, token, path, body, timeout=120):
        calls.append((url, token, path, body))
        return json.dumps({"text": fx["op"], "image": fx["image"]}).encode()

    monkeypatch.setattr(site, "post_json", post_json)
    monkeypatch.setenv("SITE_TOKEN", "tok")
    tpl = E.template(replace(cfg, renderer="site", params="qc"))
    data = tpl.load(str(root), month)
    assert (tpl.op_body(data, month, plot_url, prev_url), tpl.image_request(data, month)) == (fx["op"], fx["image"])
    req = {"month": fx["month"], "rows": fx["rows"], "cfg": fx["cfg"], "params": "qc"}
    assert [(u, t, p, json.loads(json.dumps(b))) for u, t, p, b in calls] == [
        (cfg.site_url, "tok", "/api/digest-op", {**req, "plot_url": plot_url, "prev_url": prev_url}),
        (cfg.site_url, "tok", "/api/digest-op", {**req, "plot_url": None, "prev_url": None}),
    ]
    monkeypatch.delenv("SITE_TOKEN")
    with pytest.raises(SystemExit) as e:
        tpl.op_body(data, month, plot_url, prev_url)
    assert str(e.value) == "digest: `renderer: site` needs the site token ($SITE_TOKEN)"


def test_renderer_config():
    assert (E.config_from_dict({"template": "cw"}).renderer, E.config_from_dict({"template": "cw", "renderer": "site", "params": "qc,pp"}).params) == (
        "local", "qc,pp")
    with pytest.raises(ValueError) as e:
        E.config_from_dict({"template": "cw", "renderer": "ts"})
    assert str(e.value) == "digest config renderer: 'ts' is not one of ('local', 'site')"
    with pytest.raises(ValueError) as e:
        E.config_from_dict({"template": "cw", "params": "qc"})
    assert str(e.value) == "digest config params: 'qc' needs `renderer: site` (the local renderer has no params)"
