"""disky's emoji vocabulary (specs/emoji-vocabulary.md §1, §3): semantic keys, each with a stock emoji and, for the
`disky` pack, the custom image it is drawn as.

A writer asks for a key (`code`), never a literal. A custom image renders only where the workspace has it: each run
reads the workspace's custom emoji once (`workspace_custom`, thrds' `fetch_workspace_emoji` over `emoji.list`), and a
custom name the workspace lacks is written as its key's stock emoji instead (`fallback`, `FallbackClient`). An unknown
workspace (``have`` None: no client, or `emoji.list` failed) keeps the custom names, as before the vocabulary.

Families:

- ``trend.<deg>`` (−80…80 by 10): the digest's trend arrow; custom ``arrow_deg<deg>`` (`job/gen-delta-arrows.py`'s set).
- ``runway.<n>`` (1 red … 8 green): a pool's runway colour (`quota_marks: runway-hue`); custom ``runway-<n>``.
- ``runway.<shape>.<n>``: the same step in a bucket's shape (`digest.QUOTA_SHAPES` but the circle); custom
  ``runway-<shape>-<n>``.
- ``pool.<n>.<deg>``: the arrow chip (`quota_marks: arrow-chip`), runway colour × slope; custom ``pool-chip-<n>-<deg>``.
- ``run.<outcome>``, ``trigger.<trigger>``, ``run.<outcome>.<trigger>``: the scan-run thread's and fleet post's marks.
- ``deploy.<env>.<result>``: a deploy notice's lead (`site/deploy-notify`'s ``DEFAULT_EMOJI``).

The TS side (fleet, site) reads the same table as `packages/pages-kit/src/emoji/vocab.json` (`vocab_json`;
`dt-cloud emoji vocab`, checked by the tests)."""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: The trend arrows' degrees, as `digest.deg` buckets them.
TREND_DEGS = tuple(range(-80, 81, 10))
#: Runway colour steps, 1 (red) … 8 (green); `digest_quota._hue_step`.
RUNWAY_STEPS = tuple(range(1, 9))
#: The runway emoji's colours (the site's `digest/mrkdwn.ts` `RUNWAY_HUE`; `job/gen-pool-chips.py`'s).
RUNWAY_COLORS = ("#e01e1e", "#ec4e0c", "#f27c00", "#f2a900", "#ddc800", "#9cc41a", "#4fae34", "#1e8e4a")
#: The shapes with their own runway emoji (`digest.QUOTA_SHAPES` but the circle, which is the plain ``runway-<n>``).
RUNWAY_SHAPES = ("square", "diamond", "triangle", "star")
OUTCOMES = ("clean", "issues", "failed", "running", "cancelled")
TRIGGERS = ("schedule", "manual", "unknown")
DEPLOY_ENVS = ("dev", "prod")
DEPLOY_RESULTS = ("success", "failure", "cancelled")

#: A Slack emoji code anywhere in text.
CODE_RE = re.compile(r":([a-z0-9_+'-]{1,100}):")


@dataclass(frozen=True)
class Key:
    """One vocabulary entry. ``stock``: the stock emoji as characters; ``code``: the same as Slack codes (what Slack text
    and ``icon_emoji`` carry). ``custom``: the `disky` pack's image, a Slack emoji name (no colons); ``discord`` its
    Discord application-emoji name (``[A-Za-z0-9_]`` only). ``reachable``: a writer can produce it today (`emoji build`
    and `emoji check` cover only those; an unreachable one still falls back)."""
    key: str
    meaning: str
    stock: str
    code: str
    custom: str | None = None
    discord: str | None = None
    reachable: bool = True

    @property
    def family(self) -> str:
        return self.key.split(".")[0]


def _signed(d: int) -> str:
    """A degree in a Discord name: Discord allows no ``-``, so −30 reads ``m30``."""
    return str(d) if d >= 0 else f"m{-d}"


#: The stock trend arrow by slope band: flat ➡️, rising ↗️ (⬆️ at the steep end), falling ↘️ (⬇️).
def _trend_stock(d: int) -> tuple[str, str]:
    if d == 0:
        return "➡️", ":arrow_right:"
    if d >= 70:
        return "⬆️", ":arrow_up:"
    if d > 0:
        return "↗️", ":arrow_upper_right:"
    if d <= -70:
        return "⬇️", ":arrow_down:"
    return "↘️", ":arrow_lower_right:"


#: The stock runway colour: the four circles spread over the eight steps (the `runway` marks' bands: under 3 days red,
#: under ~2 weeks orange, under ~a month yellow, else green).
_CIRCLES = (("🔴", ":red_circle:"), ("🟠", ":large_orange_circle:"), ("🟡", ":large_yellow_circle:"), ("🟢", ":large_green_circle:"))
_SQUARES = (("🟥", ":large_red_square:"), ("🟧", ":large_orange_square:"), ("🟨", ":large_yellow_square:"), ("🟩", ":large_green_square:"))


def _runway_stock(n: int, shape: str = "circle") -> tuple[str, str]:
    return (_SQUARES if shape == "square" else _CIRCLES)[(n - 1) // 2]


#: A shrinking pool's chip is only red (at or over quota), step 3 (nearly full, `RUNWAY_HUE_FULL_N`) or green: any other
#: step while shrinking can't be reached (`job/gen-pool-chips.py`).
def _chip_reachable(n: int, d: int) -> bool:
    return d >= 0 or n in (1, 3, 8)


_RUN = {"clean": ("✅", ":white_check_mark:"), "issues": ("⚠️", ":warning:"), "failed": ("❌", ":x:"),
        "running": ("⏳", ":hourglass_flowing_sand:"), "cancelled": ("🚫", ":no_entry_sign:")}
_TRIGGER = {"schedule": ("⏰", ":alarm_clock:"), "manual": ("👤", ":bust_in_silhouette:"), "unknown": ("❔", ":grey_question:")}
_DEPLOY = {"success": {"prod": ("🚀", ":rocket:"), "dev": ("🧪", ":test_tube:")},
           "failure": {"prod": ("💥", ":boom:"), "dev": ("💥", ":boom:")},
           "cancelled": {"prod": ("⏹️", ":black_square_for_stop:"), "dev": ("⏹️", ":black_square_for_stop:")}}


def _keys() -> Iterable[Key]:
    for d in TREND_DEGS:
        s, c = _trend_stock(d)
        yield Key(f"trend.{d}", f"trend arrow at {d}°", s, c, f"arrow_deg{d}", f"arrow_deg{_signed(d)}")
    for n in RUNWAY_STEPS:
        s, c = _runway_stock(n)
        yield Key(f"runway.{n}", f"runway colour {n} of 8 (1 red, 8 green)", s, c, f"runway-{n}", f"runway_{n}")
    for shape in RUNWAY_SHAPES:
        for n in RUNWAY_STEPS:
            s, c = _runway_stock(n, shape)
            yield Key(f"runway.{shape}.{n}", f"runway colour {n} of 8, {shape}", s, c, f"runway-{shape}-{n}", f"runway_{shape}_{n}")
    for n in RUNWAY_STEPS:
        s, c = _runway_stock(n)
        for d in TREND_DEGS:
            yield Key(f"pool.{n}.{d}", f"pool chip: runway colour {n}, weekly growth at {d}°", s, c, f"pool-chip-{n}-{d}",
                      f"pool_chip_{n}_{_signed(d)}", _chip_reachable(n, d))
    for o, (s, c) in _RUN.items():
        yield Key(f"run.{o}", f"scan run {o}", s, c)
    for t, (s, c) in _TRIGGER.items():
        yield Key(f"trigger.{t}", f"run trigger: {t}", s, c)
    for o, (s, c) in _RUN.items():
        for t in ("schedule", "manual"):
            ts, tc = _TRIGGER[t]
            yield Key(f"run.{o}.{t}", f"scan run {o}, {t}", s + ts, c + tc)
    for env in DEPLOY_ENVS:
        for r in DEPLOY_RESULTS:
            s, c = _DEPLOY[r][env]
            yield Key(f"deploy.{env}.{r}", f"{env} deploy {r}", s, c)


KEYS: dict[str, Key] = {k.key: k for k in _keys()}
#: Custom name → its key (`fallback`'s index).
BY_CUSTOM: dict[str, Key] = {k.custom: k for k in KEYS.values() if k.custom}
#: The packs (`emoji build -p`): `disky`, every key with a custom image.
PACKS = ("disky",)


def key(name: str) -> Key:
    try:
        return KEYS[name]
    except KeyError:
        raise KeyError(f"emoji vocabulary: no key {name!r}") from None


def code(name: str, have: Collection[str] | None = None) -> str:
    """Key ``name`` as Slack text: its custom ``:name:`` where it has one and the workspace has it (``have``: the
    workspace's custom names; None: unknown, assumed to), else its stock code."""
    k = key(name)
    return f":{k.custom}:" if k.custom and (have is None or k.custom in have) else k.code


def stock(name: str) -> str:
    """Key ``name``'s stock emoji, as characters."""
    return key(name).stock


def trend(d: int) -> str:
    """The trend arrow at ``d``° (`digest.deg`), as Slack text."""
    return code(f"trend.{d}")


def runway(n: int, shape: str = "circle") -> str:
    """Runway colour ``n`` (1 red … 8 green) in ``shape``, as Slack text."""
    return code(f"runway.{n}" if shape == "circle" else f"runway.{shape}.{n}")


def pool_chip(n: int, d: int) -> str:
    """The arrow chip for runway colour ``n`` at ``d``°, as Slack text."""
    return code(f"pool.{n}.{d}")


def fallback(text: str, have: Collection[str] | None) -> str:
    """``text`` with every vocabulary custom name the workspace lacks (not in ``have``) written as its key's stock code;
    ``text`` itself when the workspace is unknown (None). Codes outside the vocabulary are left alone: they may be stock."""
    if have is None:
        return text

    def sub(m: re.Match) -> str:
        k = BY_CUSTOM.get(m.group(1))
        return k.code if k is not None and m.group(1) not in have else m.group(0)
    return CODE_RE.sub(sub, text)


def discordify(text: str, app: Mapping[str, str]) -> str:
    """``text`` (Slack shortcodes) for Discord: each vocabulary custom name as ``<:name:id>`` from ``app`` (application
    emoji name → id, `dt-cloud discord-emoji`), or its key's stock characters when the app has no such emoji. Other codes
    are left as they are."""
    def sub(m: re.Match) -> str:
        k = BY_CUSTOM.get(m.group(1))
        if k is None:
            return m.group(0)
        return f"<:{k.discord}:{app[k.discord]}>" if k.discord in app else k.stock
    return CODE_RE.sub(sub, text)


def workspace_custom(client: Any, label: str) -> set[str] | None:
    """The workspace's custom emoji names, read once (thrds' `fetch_workspace_emoji`: ``emoji.list``, aliases followed,
    deleted ones dropped); None, logged once under ``label``, when that fails (no ``emoji:read``, network): the caller
    then writes custom names as it always has."""
    try:
        return set(client.fetch_workspace_emoji())
    except Exception as e:  # noqa: BLE001 — the fallback is best effort; the post goes out either way
        print(f"{label}: workspace emoji unread ({type(e).__name__}: {e}); custom emoji written as they are", file=sys.stderr)
        return None


_UNREAD = object()


class FallbackClient:
    """A thrds `SlackClient` (or a fake) whose posts and edits get `fallback`: text, and a vocabulary custom
    ``icon_emoji``. The workspace's custom emoji are read on the first write (`workspace_custom`, under ``label``) and
    kept for the client's life, so a run that writes nothing reads nothing. Everything else passes through."""

    def __init__(self, client: Any, label: str, have: Collection[str] | None | object = _UNREAD):
        self._client = client
        self._label = label
        self._have = have

    @property
    def have(self) -> Collection[str] | None:
        if self._have is _UNREAD:
            self._have = workspace_custom(self._client, self._label)
        return self._have  # type: ignore[return-value]

    def post(self, content: str, *args: Any, **kw: Any) -> Any:
        if kw.get("icon_emoji"):
            kw["icon_emoji"] = fallback(kw["icon_emoji"], self.have)
        return self._client.post(fallback(content, self.have), *args, **kw)

    def edit(self, message_id: str, content: str, *args: Any, **kw: Any) -> Any:
        return self._client.edit(message_id, fallback(content, self.have), *args, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def with_fallback(client: Any, label: str) -> FallbackClient:
    """``client`` wrapped in `FallbackClient`: its workspace's custom emoji read once, at its first write."""
    return FallbackClient(client, label)


def selected(keys: Iterable[str] = (), pack: str = "disky", everything: bool = False) -> list[Key]:
    """The pack's keys with a custom image: those under any of ``keys`` (exact keys or family prefixes, e.g. ``trend``,
    ``pool.3``), else all; only `Key.reachable` ones unless ``everything``."""
    if pack not in PACKS:
        raise ValueError(f"emoji pack {pack!r}: one of {', '.join(PACKS)}")
    want = tuple(keys)
    return [k for k in KEYS.values() if k.custom and (everything or k.reachable)
            and (not want or any(k.key == w or k.key.startswith(w + ".") for w in want))]


def missing(keys: Iterable[Key], have: Collection[str]) -> list[Key]:
    """Of ``keys``, those whose custom image the workspace (``have``) lacks."""
    return [k for k in keys if k.custom and k.custom not in have]


def vocab_json() -> str:
    """`KEYS` as the TS side's `packages/pages-kit/src/emoji/vocab.json`: one key a line, `{stock, code}` plus `custom`
    / `discord` where set, and `reachable: false` where not."""
    def row(k: Key) -> dict:
        out: dict[str, Any] = {"stock": k.stock, "code": k.code}
        if k.custom:
            out["custom"] = k.custom
            out["discord"] = k.discord
        if not k.reachable:
            out["reachable"] = False
        return out
    rows = [f"  {json.dumps(k.key)}: {json.dumps(row(k), ensure_ascii=False)}" for k in KEYS.values()]
    return "{\n" + ",\n".join(rows) + "\n}\n"
