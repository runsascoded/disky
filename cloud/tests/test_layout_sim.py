"""`interval-store layout-sim` over the committed `iv-churn` base generation (4 rows per group, 8 groups, 4 scans).

Its served `path` sort, by group (`seg`; rows as `depth path vf vt`, scans s0..s3 = 07-30, 07-31, 08-02, 08-03, O open):

    rg0 seg0      rows 0-3:  1 b1 s3 O | 1 b2 s2 O | 2 b1/a.bin s3 O | 2 b1/x s3 O
    rg1 seg0      rows 4-7:  2 b2/f.bin s2 O | 2 b2/z s2 O | 3 b1/x/b.bin s2 O | 3 b1/x/c.bin s3 O
    rg2 seg0      rows 8-9:  depth 3 (b2/z/…)
    rg3 seg1      rows 10-13: 1 b1 s0 s1 | 1 b2 s0 s1 | 2 b1/x s0 s1 | 3 b1/x/y s0 s1
    rg4 seg2      rows 14-17: 1 b1 s1 s2 | 1 b2 s1 s2 | 2 b1/x s1 s2 | 3 b1/x/y s1 s2
    rg5 seg3      rows 18-21: 1 b1 s2 s3 | 2 b1/a.bin s2 s3 | 2 b1/x s2 s3 | 3 b1/x/c.bin s2 s3
    rg6 seg3      rows 22-23: depth 3-4
    rg7 seg65537  row 24:     2 b2/z s0 s2   (level 1: KMAX = 1)

`b1` over 08-02 → 08-03 (rect: depth 2, [b1/, b10)): rect groups rg0, rg1, rg3, rg4, rg5; kept rg0, rg1, rg5 (12 rows);
in-rect rows in them 2 (rg0) + 0 (rg1) + 2 (rg5), all changed. Pages of 2: rg0's page 1 (rows 2-3), rg5's pages 0-1
(rows 18-21): 3 pages, 6 rows, runs (2,4) | (18,22). Bands (pages of 2) over the rect's 6 rows by (path, vf):
a.bin s2-s3 [seg3], a.bin s3-O [0], x s0-s1 [1], x s1-s2 [2], x s2-s3 [3], x s3-O [0]; B = 1: pages {0,1} and {4,5}
kept, {2,3} not (no bound in (s2, s3]); B ≥ 2: band 0 = {a.bin s3, x s3} (1 page), band 1 = the 4 closed (2 pages,
both kept: vt reaches s3).

`b2` over 07-30 → 08-02 (rect: depth 2, [b2/, b20)): rect and kept groups rg1, rg3, rg4, rg5, rg7 (17 rows); in-rect
rows: rg1's b2/f.bin, b2/z (changed: opened at s2) and rg7's b2/z (closed at s2). rg3-rg5 hold none: the rect would sit
at row 3 of each, so a page holding rows 2-3 straddles it (and passes the time test), a 1-row page doesn't.
"""
import json
import re
from pathlib import Path

from click.testing import CliRunner

from dt_cloud.cli import main
from dt_cloud.layout_sim import RUN_BYTES, RUN_GAP, RUN_READS, plan_runs, summary

ROOT = Path(__file__).parents[2]
GEN = ROOT / "site/functions/_lib/fixtures/iv-churn/interval-store/g"
REQS = [
    {"path": "b1", "from": "2026-08-02", "to": "2026-08-03", "case": "a"},
    {"path": "b2", "from": "2026-07-30", "to": "2026-08-02", "case": "b"},
]


def run(tmp_path: Path, *args: str, reqs_: list[dict] = REQS) -> list[dict]:
    reqs = tmp_path / "reqs.jsonl"
    reqs.write_text("".join(json.dumps(q) + "\n" for q in reqs_))
    out = tmp_path / f"out{len(list(tmp_path.glob('out*')))}.jsonl"
    res = CliRunner().invoke(main, ["interval-store", "layout-sim", "-c", str(tmp_path / "cache"), "-o", str(out), "-p", "1,2,4", "-b", "1,2,4", "-P", "2", "-t", "2", *args, str(GEN), str(reqs)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return [json.loads(line) for line in out.read_text().splitlines()]


def test_adjacent_runs(tmp_path):
    """`-g 0`: only touching spans merge, so runs count separate regions."""
    got = run(tmp_path, "-g", "0", "-j", "1")
    assert got == [
        {
            **REQS[0], "rect_groups": 5, "kept_groups": 3, "group_rows": 12, "in_rect": 4, "needed": 4,
            "groups": {"rows": 12, "runs": 2, "adj_runs": 2},
            "pages": {
                "1": {"pages": 4, "rows": 4, "runs": 2, "adj_runs": 2},
                "2": {"pages": 3, "rows": 6, "runs": 2, "adj_runs": 2},
                "4": {"pages": 2, "rows": 8, "runs": 2, "adj_runs": 2},
            },
            "rect_rows": 6,
            "bands": {
                "1": {"rows": 4, "runs": 2, "bands_hit": 1},
                "2": {"rows": 6, "runs": 2, "bands_hit": 2},
                "4": {"rows": 6, "runs": 2, "bands_hit": 2},
            },
        },
        {
            **REQS[1], "rect_groups": 5, "kept_groups": 5, "group_rows": 17, "in_rect": 3, "needed": 3,
            "groups": {"rows": 17, "runs": 3, "adj_runs": 3},
            "pages": {
                "1": {"pages": 3, "rows": 3, "runs": 2, "adj_runs": 2},
                "2": {"pages": 5, "rows": 9, "runs": 5, "adj_runs": 5},
                "4": {"pages": 5, "rows": 17, "runs": 3, "adj_runs": 3},
            },
            "rect_rows": 3,
            "bands": {
                "1": {"rows": 3, "runs": 1, "bands_hit": 1},
                "2": {"rows": 3, "runs": 2, "bands_hit": 2},
                "4": {"rows": 3, "runs": 2, "bands_hit": 2},
            },
        },
    ]
    assert summary(got) == [
        "2 requests: p50 / p90 / p99 / max",
        "needed    rows [3, 4, 4, 4]",
        "groups    rows [12, 17, 17, 17] runs [2, 3, 3, 3] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "p1        rows [3, 4, 4, 4] runs [2, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "p2        rows [6, 9, 9, 9] runs [2, 5, 5, 5] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "p4        rows [8, 17, 17, 17] runs [2, 3, 3, 3] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "B=1       rows [3, 4, 4, 4] runs [1, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "B=2       rows [3, 6, 6, 6] runs [2, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "B=4       rows [3, 6, 6, 6] runs [2, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
    ]


def test_site_gap_and_pool(tmp_path):
    """The site's `RUN_GAP` (~774 rows at the fixture's ~677 bytes a row) merges each region; bands never merge. A
    process pool answers as in-process."""
    got = run(tmp_path, "-j", "2")
    runs = [{"groups": r["groups"]["runs"], **{f"p{p}": v["runs"] for p, v in r["pages"].items()}, **{f"B={b}": v["runs"] for b, v in r["bands"].items()}} for r in got]
    assert runs == [{"groups": 1, "p1": 1, "p2": 1, "p4": 1, "B=1": 1, "B=2": 2, "B=4": 2}] * 2
    assert [r["pages"]["2"]["rows"] for r in got] == [6, 9]
    assert run(tmp_path, "-j", "1") == got


def test_modes_skipped(tmp_path):
    """`-b none`: only the kept groups are read, and no band fields; `-p none`: no page sizes."""
    got = run(tmp_path, "-b", "none", "-p", "none", "-j", "1")
    assert [sorted(r) for r in got] == [sorted([*REQS[0], "rect_groups", "kept_groups", "group_rows", "groups", "in_rect", "needed", "pages"])] * 2
    assert [r["pages"] for r in got] == [{}, {}]
    assert sorted(int(p.stem.removeprefix("rg-")) for p in (tmp_path / "cache").rglob("rg-*.parquet")) == [0, 1, 3, 4, 5, 7]


def test_chunks(tmp_path):
    """`-k`: `a`'s closed versions are all level 0 at scans 0, 1, 2 (`x` s0-s1, s1-s2; `a.bin`, `x` s2-s3). K0 = 0:
    each its own segment, only scan 2's (2 rows) and the open band (2 rows) kept. K0 = 1: chunks {0, 1} (not kept) and
    {2} (kept). K0 = 2: one chunk of 4 rows, both its pages reaching s3. `b`'s one closed version (`b2/z` s0-s1, level 1)
    is a band of its own under every K0."""
    got = run(tmp_path, "-g", "0", "-j", "1", "-p", "none", "-b", "none", "-k", "0,1,2")
    assert [(r["rect_rows"], r["chunks"]) for r in got] == [
        (6, {"0": {"rows": 4, "runs": 2, "bands_hit": 2}, "1": {"rows": 4, "runs": 2, "bands_hit": 2}, "2": {"rows": 6, "runs": 2, "bands_hit": 2}}),
        (3, {"0": {"rows": 3, "runs": 2, "bands_hit": 2}, "1": {"rows": 3, "runs": 2, "bands_hit": 2}, "2": {"rows": 3, "runs": 2, "bands_hit": 2}}),
    ]


def test_history(tmp_path):
    """`-H 2` on `a`: scans e0..e7 (copy 1 = +5 days), the request shifted to (e6, e7]. Copy 0's open `a.bin`, `x`
    close at e4 (level 0, scan 3); copy 1's versions sit at scans 4-6 (+ open at 7): 12 rect rows, 4 changed (`a.bin`,
    `x` at e6-e7 and open from e7). Pages of 2 by (path, vf):

        B=1: a e2-3 a e3-4 | a e6-7 a e7-O | x e0-1 x e1-2 | x e2-3 x e3-4 | x e4-5 x e5-6 | x e6-7 x e7-O → pages 1, 5
        B=2: open {a, x e7-O}; closed: a e2-3 a e3-4 | a e6-7 x e0-1 | x e1-2 x e2-3 | x e3-4 x e4-5 | x e5-6 x e6-7
        k=0: open; per scan i ∈ {0..6}: only i = 6's {a, x e6-7} kept
        k=2: open; chunk 0 (i < 4: 6 rows, none kept); chunk 1: a e6-7 x e4-5 | x e5-6 x e6-7, both kept
    """
    got = run(tmp_path, "-g", "0", "-j", "1", "-p", "none", "-b", "1,2", "-k", "0,2", "-H", "2", reqs_=REQS[:1])
    assert [{k: r[k] for k in ("rect_rows", "bands", "chunks", "history")} for r in got] == [{
        "rect_rows": 6,
        "bands": {"1": {"rows": 4, "runs": 2, "bands_hit": 1}, "2": {"rows": 6, "runs": 2, "bands_hit": 2}},
        "chunks": {"0": {"rows": 4, "runs": 2, "bands_hit": 2}, "2": {"rows": 6, "runs": 2, "bands_hit": 2}},
        "history": {
            "R": 2, "rect_rows": 12, "needed": 4,
            "bands": {"1": {"rows": 4, "runs": 2, "bands_hit": 1}, "2": {"rows": 6, "runs": 3, "bands_hit": 2}},
            "chunks": {"0": {"rows": 4, "runs": 2, "bands_hit": 2}, "2": {"rows": 6, "runs": 2, "bands_hit": 2}},
        },
    }]
    assert summary(got)[1:4] == [
        "needed    rows [4, 4, 4, 4]",
        "needed@R2 rows [4, 4, 4, 4]",
        "groups    rows [12, 12, 12, 12] runs [2, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
    ]
    assert [line.split(" rows")[0].strip() for line in summary(got)[4:]] == ["B=1", "B=2", "k=0", "k=2", "B=1@R2", "B=2@R2", "k=0@R2", "k=2@R2"]


def test_plan_runs():
    """As the site's `planRuns`: merge within `gap`, a run capped at `cap` (an oversized span is its own)."""
    assert plan_runs([(10, 12), (0, 4), (5, 8)], 1, 100) == 2
    assert plan_runs([(0, 4), (5, 8), (10, 12)], 2, 100) == 1
    assert plan_runs([(0, 4), (5, 8), (10, 12)], 2, 8) == 2
    assert plan_runs([(0, 50), (50, 60)], 0, 10) == 2


def test_constants_mirror_site():
    src = (ROOT / "site/functions/_lib/index.ts").read_text()
    consts = {k: v for k, v in re.findall(r"^export const (RUN_GAP|RUN_BYTES|RUN_READS) = ([\d <]+)$", src, re.M)}
    assert {k: eval(v) for k, v in consts.items()} == {"RUN_GAP": RUN_GAP, "RUN_BYTES": RUN_BYTES, "RUN_READS": RUN_READS}
