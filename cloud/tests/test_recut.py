"""`index-recut` and `bysize_check` over the site's `v2-slices` fixture: a store
generation over owner slices whose `bysize` is keyed on each path's total
(`site/functions/_lib/fixtures/gen.py`, spec `bysize-path-total.md`)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from disk_tree.find import groups as G
from dt_cloud.bysize_check import check_views
from dt_cloud.cli import main
from dt_cloud.index import recut_sorts

FIXTURE = Path(__file__).resolve().parents[2] / "site/functions/_lib/fixtures/v2-slices"
VIEWS = ["", "bk/m", "bk/w"]


def _check(bysize: str | Path) -> list[tuple]:
    return [
        (c.path, round(c.thr), c.ref_paths, c.cand_paths, c.equal, c.slice_short, c.slice_missing, c.slice_missing_bytes, c.groups, c.rows, c.exact)
        for c in check_views(str(FIXTURE / "path-index.parquet"), str(bysize), VIEWS, w=134, h=134)
    ]


#: `w`, `h` = 134: thresholds between `w/d*`'s 300 KiB slices and their 600 KiB totals.
EXACT = [
    ("", 456864, 4, 4, 4, 3, 0, 819200, 2, 4096, True),
    ("bk/m", 4652, 19, 19, 19, 3, 0, 0, 3, 6144, True),
    ("bk/w", 451664, 1100, 1100, 1100, 0, 1100, 675840000, 2, 4096, True),
]


def test_recut_reproduces_the_writers_bysize(tmp_path: Path):
    """Re-cut from the `path` sort alone, `bysize` is the writer's: same rows,
    order and group stats (`b_max = MAX(tot)`), and the same metadata."""
    out = tmp_path / "g2"
    sorts = recut_sorts(str(FIXTURE), str(out), work=tmp_path / "work", mem="1GB", threads=1, row_group_rows=2048)
    assert sorts == {"bysize": {"file": str(out / "path-index-bysize.parquet"), "rows": 10243, "groups": 6}}
    assert sorted(p.name for p in out.iterdir()) == ["path-index-bysize.groups.json", "path-index-bysize.groups.parquet", "path-index-bysize.parquet"]
    pd.testing.assert_frame_equal(pd.read_parquet(out / "path-index-bysize.parquet"), pd.read_parquet(FIXTURE / "path-index-bysize.parquet"))
    got = json.loads((out / "path-index-bysize.groups.json").read_text())
    want = json.loads((FIXTURE / "path-index-bysize.groups.json").read_text())
    # The fixture's sidecar is `index_footer.groups_blob`'s (rg_json byte offsets differ by writer): compare the stats.
    stats = lambda doc: [g[:6] + g[8:10] + g[11:] for g in doc["groups"]]  # noqa: E731
    assert stats(got) == stats(want)
    assert [g[5] for g in got["groups"]] == [683620352, 614400, 307200, 307200, 1024, 1024]
    assert _check(out / "path-index-bysize.parquet") == EXACT
    # Never over a published generation.
    with pytest.raises(FileExistsError, match="already exist"):
        recut_sorts(str(FIXTURE), str(out), work=tmp_path / "work2", mem="1GB", threads=1, row_group_rows=2048)


def test_check_catches_slice_stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The check's mutation: the same rows with group stats off each slice's
    `size` (`b_max` = the biggest slice) prune the groups holding only `w/`'s
    300 KiB slices at a 450 KiB threshold — 427 of its 1100 dirs go missing."""
    assert _check(FIXTURE / "path-index-bysize.parquet") == EXACT
    shutil.copy(FIXTURE / "path-index-bysize.parquet", tmp_path / "path-index-bysize.parquet")
    monkeypatch.setattr(G, "SIZE_COLS", ("size", "b"))
    G.write_groups(str(tmp_path / "path-index-bysize.parquet"))
    assert _check(tmp_path / "path-index-bysize.parquet") == [
        EXACT[0][:8] + (1, 2048, True),
        EXACT[1],
        ("bk/w", 451664, 1100, 673, 673, 0, 1100, 675840000, 1, 2048, False),
    ]


def test_index_recut_cli_checks(tmp_path: Path):
    """`index-recut -c`: the re-cut, then the root and depth-1 views checked, as JSON."""
    res = CliRunner().invoke(main, ["index-recut", "-c", "-m", "1GB", "-p", "1", "-r", "2048", "-W", str(tmp_path / "w"), str(FIXTURE), str(tmp_path / "g2")])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout.strip().splitlines()[-1])
    assert [(c["path"], c["ref_paths"], c["equal"], c["exact"]) for c in doc["check"]] == [("", 5526, 5526, True), ("bk", 5526, 5526, True)]
