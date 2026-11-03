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

import pyarrow.parquet as pq
from click.testing import CliRunner

from dt_cloud.blobstore import Store
from dt_cloud.cli import main
from dt_cloud.layout_sim import RUN_BYTES, RUN_GAP, RUN_READS, level_cost, plan_runs, summary

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
        "needed   rows [3, 4, 4, 4]",
        "groups   rows [12, 17, 17, 17] runs [2, 3, 3, 3] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "p1       rows [3, 4, 4, 4] runs [2, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "p2       rows [6, 9, 9, 9] runs [2, 5, 5, 5] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "p4       rows [8, 17, 17, 17] runs [2, 3, 3, 3] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "B=1      rows [3, 4, 4, 4] runs [1, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "B=2      rows [3, 6, 6, 6] runs [2, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
        "B=4      rows [3, 6, 6, 6] runs [2, 2, 2, 2] rounds×2 [2, 2, 2, 2] rounds×3 [3, 3, 3, 3]",
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


LEVEL_REQS = [
    {"path": "b1", "from": "2026-08-02", "to": "2026-08-06"},
    {"path": "b1", "from": "2026-08-04", "to": "2026-08-05"},
]


def test_level(tmp_path):
    """`-l` over the base (scans s0..s3 = 07-30 … 08-03) and the newest manifest's runs (`m001`: A = 08-04, 08-05 (t4,
    t5); B = 08-06 (t6)). `b1`'s children's versions, base + runs combined (smallest `vt`):

        a.bin: s2-s3, s3-t5 (base open, A closes it), t6-O → opens s2, s3, t6 + deleted at t5 (nothing starts there)
        x:     s0-s1, s1-s2, s2-s3, s3-t4, t4-t5, t5-t6, t6-O → 7 opens, no deletions

    11 transitions. (s2, t6]: base {a s3, x s3} = 2, A {a t5, x t4, x t5} = 3, B {a t6, x t6} = 2: 7 needed, 3 files,
    rows at the worst alignment ⌈(c + p − 1) / p⌉·p: p=1 7, p=2 4+4+4, p=4 8+8+8. (t4, t5]: A only, {a t5, x t5}."""
    got = run(tmp_path, "-j", "1", "-b", "none", "-l", reqs_=LEVEL_REQS)
    assert [r["level"] for r in got] == [
        {"transitions": 11, "needed": 7, "files": 3, "files_total": 3, "pages": {"1": {"rows": 7, "runs": 3}, "2": {"rows": 12, "runs": 3}, "4": {"rows": 24, "runs": 3}}},
        {"transitions": 11, "needed": 2, "files": 1, "files_total": 3, "pages": {"1": {"rows": 2, "runs": 1}, "2": {"rows": 4, "runs": 1}, "4": {"rows": 8, "runs": 1}}},
    ]
    assert summary(got)[2] == "needed L rows [2, 7, 7, 7] files [1, 3, 3, 3]"
    assert [line.split(" rows")[0].strip() for line in summary(got)[3:]] == ["groups", "p1", "p2", "p4", "L1", "L2", "L4"]


def test_level_files(tmp_path):
    """`-f N`: the 7 scans in N contiguous files. N = 1: (s2, t6]'s 7 transitions in one range; N = 2: {s0..s3} holds
    2 of them, {t4..t6} 5."""
    one = run(tmp_path, "-j", "1", "-b", "none", "-l", "-f", "1", reqs_=LEVEL_REQS[:1])
    two = run(tmp_path, "-j", "1", "-b", "none", "-l", "-f", "2", reqs_=LEVEL_REQS[:1])
    assert [r["level"] for r in one + two] == [
        {"transitions": 11, "needed": 7, "files": 1, "files_total": 1, "pages": {"1": {"rows": 7, "runs": 1}, "2": {"rows": 8, "runs": 1}, "4": {"rows": 12, "runs": 1}}},
        {"transitions": 11, "needed": 7, "files": 2, "files_total": 2, "pages": {"1": {"rows": 7, "runs": 2}, "2": {"rows": 10, "runs": 2}, "4": {"rows": 16, "runs": 2}}},
    ]


def test_level_cost():
    """Every path's versions over base (25 rows) + A (25) + B (17), one version's rows counted once: b1 7, b2 5, b1/a.bin
    3, b1/x 7, b2/f.bin 2, b2/z 3, b1/x/b.bin 2, b1/x/c.bin 5, b2/z/g.bin 2, b2/z/h.bin 2, b1/x/y 6, b1/x/y/d.bin 3,
    b1/x/y/e.bin 3 = 50; deletions: a.bin at t5, f.bin at t5, y at s3, d.bin at s3 and t6 = 5."""
    store = Store(str(GEN))
    got = level_cost(store, "path", ["", "deltas/2026-08-04_2026-08-05", "deltas/2026-08-06"], 2)
    meta = pq.ParquetFile(GEN / "served/path.parquet").metadata
    names = [meta.schema.column(i).name for i in range(meta.num_columns)]
    cols = ["path", "depth", "vf", "size", "n_files", "churn_cum", "churn_end"]
    bpr = sum(meta.row_group(g).column(names.index(c)).total_compressed_size for g in range(meta.num_row_groups) for c in cols) / meta.num_rows
    assert {k: got[k] for k in ("versions", "deletions", "transitions", "bytes")} == {"versions": 50, "deletions": 5, "transitions": 55, "bytes": round(55 * bpr)}


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
