"""Score a filter engine against a query set's ground truth
(specs/done/filter-query-service.md §6 phase 1).

An **engine** answers one (query, view) with its match roots, net totals,
coverage flags and timings. `SubtreeEngine` is any server speaking the
site's `/api/subtree?q=…&full=1` (the deployed Worker, a local `wrangler`
stack with another index variant, or a `ps=` override);
other engines plug in by implementing `answer`.

**Verdicts** (per query × view):

- `exact` — the root set (count + md5 of the sorted paths) and the net totals
  (bytes, objects) match the truth, and nothing was flagged;
- `exact*` — exact, but flagged `partial` / `approximate` (conservative);
- `totals` — exact declared root count and net totals, but a capped list prevents verification of the full root set;
- `flagged` — inexact and flagged: a degraded answer that says so;
- `FAIL` — inexact and **not** flagged: a silently short (or wrong) answer, a
  correctness bug;
- `refused` — no answer, said so (a 413: "query too wide");
- `error` — no answer (any other non-200, transport failure).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import statistics
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Protocol

from ..probe import Fetch, cold_min_area
from .queryset import Case

CANVAS = "cv=2&w=1408&h=896"  # the browser's default canvas (`probe.scenarios`)


@dataclass(frozen=True)
class Answer:
    status: int
    ms: int
    server_ms: int | None
    bytes_in: int  # response size
    roots: list[str] | None  # None: no answer
    b: int | None
    o: int | None
    partial: bool = False
    approximate: bool = False
    reason: str | None = None
    truncated: bool = False
    tier: str | None = None
    cache: str | None = None
    url: str = ""
    # An in-process engine with a huge root set gives its count and md5 (of
    # the sorted list) instead of the list (`roots` then holds none of it).
    n_roots: int | None = None
    roots_md5: str | None = None
    # The transport returned only a declared subset of the roots, with
    # `n_roots` carrying the exact total. This is distinct from a tree walk's
    # `truncated` flag: the tree and totals can remain exact.
    roots_truncated: bool = False

    @property
    def answered(self) -> bool:
        return self.roots is not None or self.n_roots is not None

    @property
    def count(self) -> int | None:
        return self.n_roots if self.n_roots is not None else (None if self.roots is None else len(self.roots))

    @property
    def md5(self) -> str:
        return self.roots_md5 or md5_paths(self.roots or [])


class Engine(Protocol):
    name: str

    def answer(self, case: Case, view: str) -> Answer: ...


def md5_paths(paths: list[str]) -> str:
    return hashlib.md5("\n".join(sorted(paths)).encode()).hexdigest()


class SubtreeEngine:
    """`/api/subtree?…&q=&qs=&full=1` on any base URL `fetch` targets.
    `params` are appended verbatim (e.g. `ps=iv`); `cold` keys every request
    past the edge cache with a random `minArea` (`probe.cold_min_area`)."""

    def __init__(self, fetch: Fetch, date: str, *, name: str = "subtree", params: str = "", cold: bool = False, rng: random.Random | None = None):
        self.fetch = fetch
        self.date = date
        self.name = name
        self.params = params
        self.cold = cold
        self.rng = rng or random.Random()

    def url(self, case: Case, view: str) -> str:
        q = urllib.parse.quote
        cold = f"&minArea={cold_min_area(self.rng)}" if self.cold else ""
        extra = f"&{self.params}" if self.params else ""
        return f"/api/subtree?{CANVAS}{cold}&date={self.date}&path={q(view, safe='')}&q={q(case.q, safe='')}&qs={case.qs}&full=1{extra}"

    def answer(self, case: Case, view: str) -> Answer:
        url = self.url(case, view)
        r = self.fetch(url)
        if r.status != 200:
            return Answer(r.status, r.ms, r.server_ms, r.bytes, None, None, None, cache=r.cache, url=url, reason=r.body[:200].decode(errors="replace") or None)
        return parse_subtree(r.body, view, r.status, r.ms, r.server_ms, r.bytes, r.cache, url)


def parse_subtree(body: bytes, view: str, status: int, ms: int, server_ms: int | None, size: int, cache: str | None, url: str) -> Answer:
    """A `/api/subtree` filter response → the engine's answer. Without
    `matches` the query matched the view root itself (the plain view
    answers): one root, the view; the tree root's `b` / `o` are the totals
    (net of exclusions when there are any) either way."""
    d = json.loads(body)
    roots = d["matches"] if "matches" in d else [view]
    roots_truncated = bool(d.get("matchesTruncated"))
    reasons = " · ".join(x for x in (d.get("partialReason"), d.get("approximateReason")) if x) or None
    return Answer(
        status, ms, server_ms, size, roots, int(d["tree"]["b"]), int(d["tree"]["o"]),
        partial=bool(d.get("partial")), approximate=bool(d.get("approximate")), reason=reasons,
        truncated=bool(d.get("truncated")), tier=d.get("tier"), cache=cache, url=url,
        n_roots=int(d["matchesTotal"]) if roots_truncated else None, roots_truncated=roots_truncated,
    )


@dataclass(frozen=True)
class Score:
    id: str
    q: str
    qs: str
    view: str
    verdict: str
    roots: int | None
    roots_want: int
    missing: int | None  # vs the truth list, when it has one
    extra: int | None
    b_err: float | None  # (got − want) / want; 0 when both are 0
    o_err: float | None
    partial: bool
    approximate: bool
    reason: str | None
    status: int
    ms: list[int]
    server_ms: list[int | None]
    truncated: bool
    roots_truncated: bool
    tier: str | None

    @property
    def wall(self) -> int:
        return round(statistics.median(self.ms))

    @property
    def server(self) -> int | None:
        s = [x for x in self.server_ms if x is not None]
        return round(statistics.median(s)) if s else None


def _rel(got: int, want: int) -> float:
    return 0.0 if got == want else (got - want) / want if want else float("inf")


def within(got: int, want: int) -> bool:
    """Totals equal up to rounding (the response rounds its float sums once)."""
    return abs(got - want) <= 1


def score(case: Case, view: str, answers: list[Answer], truth: dict, listed: list[str] | None) -> Score:
    """`truth` is the view's summary (`truth.ViewTruth.summary`); `listed` its
    root list when the truth file has one. With several answers (repeats),
    the first decides the verdict and all of them the timings."""
    a = answers[0]
    flagged = a.partial or a.approximate
    want_n = truth["roots"]
    if not a.answered:
        verdict, missing, extra, be, oe = "refused" if a.status == 413 else "error", None, None, None, None
    else:
        if a.roots_truncated:
            # `matches` is now an explicitly auxiliary, bounded list. Check
            # its subset has no false positives when the truth list is
            # available. Count + totals cannot prove the omitted paths are
            # correct: never report full root-set exactness from them alone.
            extra = len(set(a.roots or []) - set(listed)) if listed is not None else None
            missing = None
            same = a.count == want_n and extra in (None, 0)
        else:
            same = a.count == want_n and a.md5 == truth["md5"]
        if listed is not None and a.n_roots is None:
            got, want = set(a.roots), set(listed)
            missing, extra = len(want - got), len(got - want)
        elif not a.roots_truncated:
            missing = extra = None
        be, oe = _rel(a.b, truth["bytes"]), _rel(a.o, truth["objects"])
        exact = same and within(a.b, truth["bytes"]) and within(a.o, truth["objects"])
        # A view whose matches hold no bytes is drawn empty: no roots, 0 B.
        if not exact and truth["bytes"] == 0 and not a.count and a.b == 0:
            exact = True
        verdict = ("totals" if a.roots_truncated else ("exact*" if flagged else "exact")) if exact else ("flagged" if flagged else "FAIL")
    return Score(
        case.id, case.q, case.qs, view, verdict, a.count, want_n, missing, extra, be, oe,
        a.partial, a.approximate, a.reason, a.status, [x.ms for x in answers], [x.server_ms for x in answers], a.truncated, a.roots_truncated, a.tier,
    )


class Truth:
    """A truth set (`truth.write`'s layout): `summary.json` plus per-query
    files, read on demand for their root lists."""

    def __init__(self, uri: str, opener=None):
        import fsspec

        self.base = uri.rstrip("/")
        self.open = opener or (lambda p: fsspec.open(p, "r"))
        with self.open(f"{self.base}/summary.json") as f:
            self.summary = json.load(f)
        self.by_id = {q["id"]: q for q in self.summary["queries"]}
        self._lists: dict[str, dict[str, list[str] | None]] = {}

    def view(self, qid: str, view: str) -> dict:
        for v in self.by_id[qid]["views"]:
            if v["view"] == view:
                return v
        raise KeyError(f"no truth for {qid} @ {view!r}")

    def listed(self, qid: str, view: str) -> list[str] | None:
        if qid not in self._lists:
            with self.open(f"{self.base}/{qid}.json") as f:
                d = json.load(f)
            self._lists[qid] = {v["view"]: [r[0] for r in v["list"]] if v.get("list") is not None else None for v in d["views"]}
        return self._lists[qid].get(view)


def run(engine: Engine, cases: list[Case], truth: Truth, *, repeat: int = 1, log=None) -> list[Score]:
    """Every case × view, sequentially (latency is measured one request at a
    time), each `repeat` times."""
    out = []
    for c in cases:
        if c.id not in truth.by_id:
            raise KeyError(f"query {c.id!r} has no truth in {truth.base}")
        for v in c.views:
            answers = [engine.answer(c, v) for _ in range(repeat)]
            want = truth.view(c.id, v)
            s = score(c, v, answers, want, truth.listed(c.id, v) if not _same(answers[0], want) else None)
            if log:
                log(line(s))
            out.append(s)
    return out


def _same(a: Answer, want: dict) -> bool:
    return a.answered and a.count == want["roots"] and a.md5 == want["md5"]


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    if x == 0:
        return "0"
    if x == float("inf"):
        return "+∞"
    return f"{x * 100:+.3g}%"


HEADER = f"{'verdict':<8} {'id':<22} {'view':<26} {'roots':>15} {'miss/extra':>13} {'Δbytes':>9} {'Δobj':>9} {'flags':<5} {'wall':>7} {'server':>7}"


def line(s: Score) -> str:
    roots = f"{'—' if s.roots is None else s.roots}/{s.roots_want}"
    me = "—" if s.missing is None else f"-{s.missing}/+{s.extra}"
    flags = ("P" if s.partial else "") + ("A" if s.approximate else "")
    srv = "—" if s.server is None else f"{s.server / 1000:.2f}s"
    view = s.view if s.view else "(root)"
    return f"{s.verdict:<8} {s.id:<22} {view[:26]:<26} {roots:>15} {me:>13} {_pct(s.b_err):>9} {_pct(s.o_err):>9} {flags:<5} {s.wall / 1000:6.2f}s {srv:>7}"


def tally(scores: list[Score]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in scores:
        out[s.verdict] = out.get(s.verdict, 0) + 1
    return out


def record(base: str, engine: Engine, date: str, truth_uri: str, scores: list[Score], *, cold: bool, repeat: int, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    return {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "base": base, "engine": engine.name, "date": date, "truth": truth_uri,
        "cold": cold, "repeat": repeat, "tally": tally(scores), "results": [asdict(s) for s in scores],
    }
