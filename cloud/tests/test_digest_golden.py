"""Golden specs for both digest templates: what each posts, end to end, from a
small snapshot tree — no network, no secrets.

Each test renders one template over a fixture month and compares the result,
byte for byte, against a file under `tests/fixtures/digest/`: the `--dry-run`
CLI output (OP + every reply), and the full call log of a converge against a
fake Slack / Discord client (every post/edit/delete with all its arguments)
plus the persisted state. They pin the digests' observable behaviour across
refactors; `DT_UPDATE_GOLDEN=1 pytest …` rewrites them (review the diff).

Fixtures: gcs = daily date-id scans with storage-class bytes (a lead-in on
7/31, a missing 8/6, three ISO weeks); cw = 00:01Z/12:01Z scans with
`meta.buckets` (a flat pre-`buckets` lead-in, a day missing its morning scan,
a third quota-less bucket appearing mid-month, and a `tree.json` per scan
for the diff treemap)."""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import cli
from dt_cloud import digest as DG

TIB = 1024**4
GOLDEN = Path(__file__).parent / "fixtures" / "digest"
AUG = date(2026, 8, 1)
SEP = date(2026, 9, 1)


def golden(name: str, actual: str) -> None:
    path = GOLDEN / name
    if os.environ.get("DT_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
    assert actual == path.read_text()


def _norm(s: str) -> str:
    """The plot's random name + cache-busting timestamp, the only nondeterminism."""
    return re.sub(r"plot-[0-9a-f]{32}\.png", "plot-<id>.png", re.sub(r"(\.png)\?v=\d{6,}", r"\1?v=<ts>", s))


# ---- fixtures ---------------------------------------------------------------

# (date, total, std, near, cold, arch) TiB
GCS_SCANS = [
    ("2026-07-31", 3000.0, 300.0, 600.0, 1500.0, 600.0),
    ("2026-08-01", 3012.5, 312.5, 600.0, 1500.0, 600.0),
    ("2026-08-02", 3010.0, 300.0, 610.0, 1500.0, 600.0),
    ("2026-08-03", 3045.0, 335.0, 610.0, 1500.0, 600.0),
    ("2026-08-04", 3040.2, 320.2, 610.0, 1510.0, 600.0),
    ("2026-08-05", 2990.0, 270.0, 610.0, 1510.0, 600.0),
    ("2026-08-07", 2991.0, 271.0, 610.0, 1510.0, 600.0),
    ("2026-08-08", 3100.0, 380.0, 610.0, 1510.0, 600.0),
    ("2026-08-09", 3100.0, 380.0, 610.0, 1510.0, 600.0),
    ("2026-08-10", 3150.5, 300.0, 700.5, 1550.0, 600.0),
    ("2026-08-11", 3149.0, 298.5, 700.5, 1550.0, 600.0),
    ("2026-09-01", 3200.0, 350.0, 700.0, 1550.0, 600.0),
]


def _gcs_meta(tot: float, std: float, near: float, cold: float, arch: float) -> dict:
    return {
        "total_bytes": round(tot * TIB),
        "class_bytes": {"1": round(std * TIB), "2": round(near * TIB), "3": round(cold * TIB), "4": round(arch * TIB)},
    }


def gcs_root(tmp_path: Path, upto: str = "9999") -> Path:
    root = tmp_path / "snapshots"
    for d, *v in GCS_SCANS:
        if d <= upto:
            (root / d).mkdir(parents=True, exist_ok=True)
            (root / d / "meta.json").write_text(json.dumps(_gcs_meta(*v)))
    (root / "rules.json").write_text("{}")  # a non-scan sibling the listing ignores
    return root


P, HERO, WEST = "marin-us-east-02a", "hero-checkpoints", "marin-us-west-04a"
# (scan, {bucket: TiB}); the first is a flat pre-`buckets` meta (the primary's totals)
CW_SCANS = [
    ("2026-08-30T1201", {P: 690.0}),
    ("2026-08-31T0001", {P: 700.0}),
    ("2026-08-31T1201", {P: 705.4, HERO: 88.0}),
    ("2026-09-01T0001", {P: 710.0, HERO: 88.6}),
    ("2026-09-01T1201", {P: 713.2, HERO: 89.25}),
    ("2026-09-02T0001", {P: 712.0, HERO: 90.75}),
    ("2026-09-02T1201", {P: 715.0, HERO: 91.0}),
    ("2026-09-03T0001", {P: 760.5, HERO: 91.0}),
    ("2026-09-04T0001", {P: 758.0, HERO: 92.5}),
    ("2026-09-04T1201", {P: 701.0, HERO: 92.5, WEST: 1.9}),
    ("2026-09-07T1201", {P: 702.5, HERO: 99.0, WEST: 2.4}),
    ("2026-09-08T0001", {P: 790.0, HERO: 99.5, WEST: 2.4}),
]
FLAT = {"2026-08-30T1201", "2026-08-31T0001"}


def _cw_meta(scan: str, buckets: dict[str, float]) -> dict:
    if scan in FLAT:
        return {"total_bytes": round(buckets[P] * TIB), "total_objects": 1_000_000, "class_bytes": {}}
    return {
        "total_bytes": round(sum(buckets.values()) * TIB), "total_objects": 1_000_000 * len(buckets), "class_bytes": {},
        "buckets": {b: {"total_bytes": round(t * TIB), "total_objects": 1_000_000} for b, t in buckets.items()},
    }


def _node(n: str, tib: float, kids=()) -> dict:
    d = {"n": n, "b": round(tib * TIB), "o": 1, "d": 20000}
    if kids:
        d["c"] = list(kids)
    return d


def _cw_tree(i: int, buckets: dict[str, float]) -> dict:
    """A per-scan `tree.json`: the primary's dirs drift scan to scan (one grows,
    one shrinks, one churns, a new one appears halfway)."""
    tot = buckets[P]
    tmp = 50.0 + 3 * i
    ckpt = 300.0 - 2 * i
    users = _node("users", 40.0 + (i % 3), [_node("romain", 20.0 + (i % 3), [_node("run-a", 20.0 + (i % 3))]), _node("ahmed", 20.0)])
    kids = [_node("tmp", tmp, [_node("ttl=14d", tmp - 10), _node("ttl=30d", 10.0)]), _node("checkpoints", ckpt), users]
    if i >= 6:
        kids.append(_node("evals", 5.0 * (i - 5), [_node("lm", 3.0 * (i - 5)), _node("vision", 2.0 * (i - 5))]))
    rest = tot - sum(k["b"] for k in kids) / TIB
    kids.append(_node("(other)", rest))
    return {"n": "cw", "b": round(sum(buckets.values()) * TIB), "c": [_node(P, tot, kids)] + [_node(b, t) for b, t in buckets.items() if b != P]}


def cw_root(tmp_path: Path, upto: str = "9999") -> Path:
    root = tmp_path / "snapshots" / "cw"
    for i, (scan, buckets) in enumerate(CW_SCANS):
        if scan <= upto:
            (root / scan).mkdir(parents=True, exist_ok=True)
            (root / scan / "meta.json").write_text(json.dumps(_cw_meta(scan, buckets)))
            (root / scan / "tree.json").write_text(json.dumps(_cw_tree(i, buckets)))
    return root


# ---- fakes ------------------------------------------------------------------


class _Msg:
    def __init__(self, id: str):
        self.id = id


class FakeSlack:
    """Logs every call with all its arguments; message ids are sequential."""

    def __init__(self, log: list):
        self.log = log

    def post(self, content, thread_id=None, *, username=None, icon_url=None, icon_emoji=None):
        self.log.append({"op": "post", "content": content, "thread_id": thread_id, "username": username, "icon_url": icon_url, "icon_emoji": icon_emoji})
        return _Msg(f"m{sum(c['op'] == 'post' for c in self.log)}")

    def edit(self, ts, content):
        self.log.append({"op": "edit", "ts": ts, "content": content})
        return _Msg(ts)

    def delete(self, message_id, orphans_ok=False):
        self.log.append({"op": "delete", "ts": message_id, "orphans_ok": orphans_ok})


class FakeHook:
    def __init__(self, log: list, name: str = "hook"):
        self.log, self.name = log, name

    def post(self, content, thread_id=None, *, username=None, icon_url=None, files=()):
        self.log.append({"op": f"{self.name}.post", "content": content, "thread_id": thread_id, "username": username, "icon_url": icon_url, "files": [str(f) for f in files]})
        return _Msg(f"d{sum(c['op'].endswith('.post') for c in self.log)}")

    def edit(self, message_id, content, *, files=()):
        self.log.append({"op": f"{self.name}.edit", "id": message_id, "content": content, "files": [str(f) for f in files]})
        return _Msg(message_id)


class FakeBot:
    def __init__(self, log: list):
        self.log = log

    def create_thread(self, message_id, name):
        self.log.append({"op": "bot.create_thread", "id": message_id, "name": name})
        return "t1"


# every arrow glyph the Discord twin might reference
EMOJI = {DG.emoji_name(d): f"e{d}" for d in range(-80, 81, 10)}


def _dump(log: list, state: dict | None = None) -> str:
    out = "\n".join(json.dumps(c, ensure_ascii=False) for c in log)
    if state is not None:
        out += "\n--- state ---\n" + json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True)
    return _norm(out) + "\n"


@pytest.fixture
def slack(monkeypatch):
    """Route `thrds.slack.SlackClient(token, channel)` to a logging fake."""
    import thrds.slack

    log: list = []
    monkeypatch.setattr(thrds.slack, "SlackClient", lambda token, channel: FakeSlack(log))
    return log


# ---- adapters: the only lines a refactor of the digest API may touch ---------


GCS = DG.template(DG.PRESETS["gcs"])
CW = DG.template(DG.PRESETS["cw"])


def _client():
    import thrds.slack

    return thrds.slack.SlackClient("xoxb", "C1")


def converge_gcs(root: Path, month: date) -> dict:
    return DG.converge_slack(GCS, str(root), month, _client(), "C1")


def converge_cw(root: Path, month: date, variant: str, reply_hour: int = 12) -> dict:
    from dataclasses import replace

    return DG.converge_slack(DG.template(replace(DG.PRESETS["cw"], reply_hour=reply_hour)), str(root), month, _client(), "C1", variant)


def redo_cw(root: Path, month: date, for_real: bool) -> dict:
    return DG.redo_replies(CW, str(root), month, _client(), "C1", "sender", for_real=for_real)


def converge_gcs_discord(root: Path, month: date, state: dict, log: list, edit_replies: bool = False) -> dict:
    return DG.converge_discord(
        GCS, GCS.load(str(root), month), month, state,
        hook=FakeHook(log), bot=FakeBot(log), emoji=EMOJI, plot="/x/plot.png",
        edit_replies=edit_replies, reply_hook=FakeHook(log, "reply_hook"),
    )


# ---- dry-run renders (the CLI) ---------------------------------------------


def _dry_run(monkeypatch, tmp_path: Path, args: list[str]) -> str:
    pytest.importorskip("matplotlib")
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "plots"))
    (tmp_path / "plots").mkdir()
    r = CliRunner().invoke(cli.main, args)
    assert r.exit_code == 0, r.output
    return r.stdout


def test_gcs_dry_run(monkeypatch, tmp_path: Path):
    root = gcs_root(tmp_path)
    golden("gcs-dry-run.txt", _dry_run(monkeypatch, tmp_path, ["digest", "-n", "-r", str(root), "-m", "2026-08"]))


@pytest.mark.parametrize("variant", ["sender", "body"])
def test_cw_dry_run(monkeypatch, tmp_path: Path, variant: str):
    root = cw_root(tmp_path)
    golden(f"cw-dry-run-{variant}.txt", _dry_run(monkeypatch, tmp_path, ["cw-digest", "-n", "-r", str(root), "-m", "2026-09", "-V", variant]))


# ---- converges against fake clients ------------------------------------------


def test_gcs_slack(slack, tmp_path: Path):
    # mid-month (through 8/4), then the rest of the month, then an idempotent re-run
    root = gcs_root(tmp_path, upto="2026-08-04")
    converge_gcs(root, AUG)
    gcs_root(tmp_path)
    converge_gcs(root, AUG)
    state = converge_gcs(root, AUG)
    assert json.loads((tmp_path / "digest" / "2026-08.json").read_text()) == state
    golden("gcs-slack.txt", _dump(slack, state))


def test_gcs_discord(tmp_path: Path):
    log: list = []
    root = gcs_root(tmp_path, upto="2026-08-04")
    state = converge_gcs_discord(root, AUG, {}, log)
    gcs_root(tmp_path)
    state = converge_gcs_discord(root, AUG, state, log)
    state = converge_gcs_discord(root, AUG, state, log, edit_replies=True)
    golden("gcs-discord.txt", _dump(log, state))


@pytest.mark.parametrize("variant", ["sender", "body"])
def test_cw_slack(slack, tmp_path: Path, variant: str):
    # scan by scan, as the 12-hourly job lands them, then an idempotent re-run
    for scan, _ in CW_SCANS:
        root = cw_root(tmp_path, upto=scan)
        if scan >= "2026-09":
            slack.append({"op": "-- lands", "scan": scan})
            state = converge_cw(root, SEP, variant)
    state = converge_cw(root, SEP, variant)
    assert json.loads((tmp_path / "digest" / "cw" / "C1" / variant / "2026-09.json").read_text()) == state
    golden(f"cw-slack-{variant}.txt", _dump(slack, state))


def test_cw_redo(slack, tmp_path: Path):
    # a month threaded under the old first-scan rule (reply_hour 0), re-threaded under the morning rule
    root = cw_root(tmp_path)
    converge_cw(root, SEP, "sender", reply_hour=0)
    plan = redo_cw(root, SEP, for_real=False)
    slack.append({"op": "-- plan", "plan": plan})
    state = redo_cw(root, SEP, for_real=True)
    golden("cw-redo.txt", _dump(slack, state))
