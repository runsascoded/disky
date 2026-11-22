"""The emoji vocabulary (specs/emoji-vocabulary.md §1, §3, §4): keys, the custom → stock fallback, Discord rendering,
the TS table."""
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from dt_cloud import emoji_vocab as V
from dt_cloud import nominal as N

ROOT = Path(__file__).resolve().parents[2]


def test_the_ts_table_is_the_vocabulary():
    """`packages/pages-kit/src/emoji/vocab.json` (what fleet and the site read) is `dt-cloud emoji vocab`'s output, byte
    for byte: regenerate it with `dt-cloud emoji vocab > packages/pages-kit/src/emoji/vocab.json` after changing a key."""
    assert (ROOT / "packages/pages-kit/src/emoji/vocab.json").read_text() == V.vocab_json()


def test_families_and_the_pack():
    fams: dict[str, int] = {}
    for k in V.KEYS.values():
        fams[k.family] = fams.get(k.family, 0) + 1
    assert fams == {"trend": 17, "runway": 40, "pool": 136, "run": 15, "trigger": 3, "deploy": 6}
    assert [len(V.selected(f)) for f in (["trend"], ["runway"], ["pool"], [])] == [17, 40, 96, 153]
    assert len(V.selected(everything=True)) == 193
    assert [k.custom for k in V.selected(["runway.star", "pool.3.-80"])] == [
        *(f"runway-star-{n}" for n in range(1, 9)), "pool-chip-3--80"]
    assert V.selected(["pool.2.-10"]) == []


def test_codes_by_key():
    assert [V.trend(d) for d in (-80, -30, 0, 50)] == [":arrow_deg-80:", ":arrow_deg-30:", ":arrow_deg0:", ":arrow_deg50:"]
    assert [V.runway(3), V.runway(3, "star"), V.pool_chip(8, -50)] == [":runway-3:", ":runway-star-3:", ":pool-chip-8--50:"]
    assert [V.code(f"trend.{d}", set()) for d in (-80, -60, 0, 60, 70)] == [
        ":arrow_down:", ":arrow_lower_right:", ":arrow_right:", ":arrow_upper_right:", ":arrow_up:"]
    assert [V.code(f"runway.{n}", set()) for n in range(1, 9)] == [
        ":red_circle:", ":red_circle:", ":large_orange_circle:", ":large_orange_circle:",
        ":large_yellow_circle:", ":large_yellow_circle:", ":large_green_circle:", ":large_green_circle:"]
    assert [V.code("runway.square.1", set()), V.code("pool.8.40", set()), V.code("run.failed.schedule")] == [
        ":large_red_square:", ":large_green_circle:", ":x::alarm_clock:"]
    assert [V.key(f"trend.{d}").discord for d in (-30, 30)] + [V.key("runway.star.2").discord, V.key("pool.8.-50").discord] == [
        "arrow_degm30", "arrow_deg30", "runway_star_2", "pool_chip_8_m50"]
    with pytest.raises(KeyError, match="no key 'trend.90'"):
        V.code("trend.90")


TEXT = ":arrow_deg-30: **710 TiB** · :runway-2: [main](u) · :pool-chip-8--50: [cold](u) · :calendar: :ryan: 12:30:45"


def test_fallback_writes_what_the_workspace_lacks_as_stock():
    assert V.fallback(TEXT, None) == TEXT
    assert V.fallback(TEXT, {"runway-2"}) == (
        ":arrow_lower_right: **710 TiB** · :runway-2: [main](u) · :large_green_circle: [cold](u) · :calendar: :ryan: 12:30:45")
    assert V.fallback(TEXT, {"arrow_deg-30", "runway-2", "pool-chip-8--50"}) == TEXT


def test_discordify_uses_app_emoji_else_stock():
    assert V.discordify(TEXT, {"arrow_degm30": "1", "pool_chip_8_m50": "9"}) == (
        "<:arrow_degm30:1> **710 TiB** · 🔴 [main](u) · <:pool_chip_8_m50:9> [cold](u) · :calendar: :ryan: 12:30:45")


class Recorder:
    def __init__(self, emoji=None):
        self.calls = []
        self.emoji = emoji
        self.reads = 0

    def fetch_workspace_emoji(self):
        self.reads += 1
        if self.emoji is None:
            raise RuntimeError("missing_scope")
        return self.emoji

    def post(self, content, thread_id=None, **kw):
        self.calls.append(("post", content, thread_id, kw))
        return SimpleNamespace(id="m1")

    def edit(self, ts, content):
        self.calls.append(("edit", ts, content))

    def permalink(self, ts):
        return f"https://x.test/{ts}"


def test_fallback_client_reads_once_at_the_first_write():
    rec = Recorder({"runway-2": "https://e.test/r2.png"})
    c = V.with_fallback(rec, "digest")
    assert (c.permalink("m0"), rec.reads) == ("https://x.test/m0", 0)
    c.post(":runway-2: :runway-1:", thread_id="op", username="u", icon_emoji=":arrow_deg10:")
    c.edit("m1", ":arrow_deg0: x")
    c.post("plain", icon_emoji=None)
    assert rec.reads == 1
    assert rec.calls == [
        ("post", ":runway-2: :red_circle:", "op", {"username": "u", "icon_emoji": ":arrow_upper_right:"}),
        ("edit", "m1", ":arrow_right: x"),
        ("post", "plain", None, {"icon_emoji": None}),
    ]


def test_an_unread_workspace_logs_once_and_writes_as_before(capsys):
    rec = Recorder(None)
    c = V.with_fallback(rec, "digest")
    c.post(":runway-2:")
    c.edit("m1", ":arrow_deg0:")
    assert (rec.reads, rec.calls) == (1, [("post", ":runway-2:", None, {}), ("edit", "m1", ":arrow_deg0:")])
    assert capsys.readouterr().err.split("\n") == [
        "digest: workspace emoji unread (RuntimeError: missing_scope); custom emoji written as they are", ""]


def test_nominal_drops_a_starters_emoji_the_workspace_lacks():
    rows = [{"run": {"trigger": "manual", "trigger_by": "a@x.test", "trigger_emoji": ":ryan:"}},
            {"run": {"trigger": "manual", "trigger_by": "b@x.test", "trigger_emoji": ":bea:"}},
            {"run": {"trigger": "schedule"}}]
    assert N.faces(rows, None) is rows
    assert N.faces(rows, {"ryan"}) == [rows[0], {"run": {"trigger": "manual", "trigger_by": "b@x.test"}}, rows[2]]
    assert [N.starter(r) for r in N.faces(rows, {"ryan"})] == [
        (":ryan:", None), (":bust_in_silhouette:", "b@x.test"), (":alarm_clock:", None)]


def test_nominal_and_deploy_notify_stock_match_the_vocabulary():
    assert N.EMOJI == {"clean": "✅", "issues": "⚠️", "failed": "❌", "running": "⏳", "cancelled": "🚫"}
    assert N.TRIGGER_EMOJI == {"schedule": ":alarm_clock:", "manual": ":bust_in_silhouette:", None: ":grey_question:"}
    notify = ModuleType("deploy_notify")
    notify.__file__ = str(ROOT / "site/deploy-notify")
    SourceFileLoader(notify.__name__, notify.__file__).exec_module(notify)
    assert notify.DEFAULT_EMOJI == {env: {r: V.stock(f"deploy.{env}.{r}") for r in V.DEPLOY_RESULTS} for env in ("prod", "dev")}
