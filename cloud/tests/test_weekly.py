"""Specs for the weekly Discord report's pure functions (`dt_cloud.weekly`).

A hand-built pair of scans (`A` = from, `B` = to) in GiB, one directory per
status, so every mover line and the composed message are checkable by hand."""
import datetime as dt

from dt_cloud import weekly as W

GIB = W.GIB


def _rows(spec: dict[str, tuple[int, dict[str, int]]]) -> list[W.Row]:
    """``{path: (total_gib, {usr: gib})}`` → owner slices per path plus the
    unowned remainder as the ``usr`` None row (the tier's own shape)."""
    out = []
    for path, (tot, users) in spec.items():
        depth = path.count("/") + 1
        rest = tot - sum(users.values())
        assert rest >= 0, path
        if rest or not users:
            out.append(W.Row(path, depth, rest * GIB, rest))
        out.extend(W.Row(path, depth, g * GIB, g, usr) for usr, g in users.items())
    return out


A = W.Table(_rows({
    "mb": (2000, {}),
    "mb/old": (300, {"bob-b": 180, "cat-c": 120}),          # gone (Bob owns 60%)
    "mb/ck": (1000, {"ann-a": 1000}),                        # grows via one child
    "mb/ck/run1": (100, {"ann-a": 100}),
    "mb/ck/run1/x": (50, {"ann-a": 50}),
    "mb/ck/run2": (900, {"ann-a": 900}),
    "mb/data": (600, {"cat-c": 240, "dan-d": 200, "eve-e": 160}),  # shrinks, spread over children
    "mb/data/p": (200, {"cat-c": 200}),
    "mb/data/q": (200, {"dan-d": 200}),
    "mb/data/r": (200, {"eve-e": 200}),
    "mb/scratch": (400, {"bob-b": 400}),                     # gone under a sweep band
    "mb/small": (10, {"ann-a": 10}),                         # below threshold
    "mb/same": (500, {"ann-a": 500}),                        # unchanged
}))
B = W.Table(_rows({
    "mb": (2350, {}),
    "mb/new": (200, {"ann-a": 200}),                         # new
    "mb/ck": (1500, {"ann-a": 1500}),
    "mb/ck/run1": (550, {"ann-a": 550}),                     # +450 of the parent's +500
    "mb/ck/run1/x": (250, {"ann-a": 250}),                   # +200 — under 80% of +450, so run1 is the answer
    "mb/ck/run2": (950, {"ann-a": 950}),
    "mb/data": (450, {"cat-c": 180, "dan-d": 140, "eve-e": 130}),
    "mb/data/p": (140, {"cat-c": 140}),
    "mb/data/q": (140, {"dan-d": 140}),
    "mb/data/r": (170, {"eve-e": 170}),
    "mb/small": (60, {"ann-a": 60}),
    "mb/same": (500, {"ann-a": 500}),
}))

MOVERS = W.movers(A, B, swept=["mb/scratch"])


def test_movers_selects_maximal_explanatory_prefixes():
    assert MOVERS == W.Movers(
        up=[
            W.Mover("mb/ck/run1", 450 * GIB, "grew", "ann-a"),
            W.Mover("mb/new", 200 * GIB, "new", "ann-a"),
        ],
        down=[
            W.Mover("mb/scratch", -400 * GIB, "swept", "bob-b"),
            W.Mover("mb/old", -300 * GIB, "gone", "bob-b"),
            W.Mover("mb/data", -150 * GIB, "shrank", None),
        ],
        more_up=0,
        more_down=0,
    )


def test_movers_top_truncates_into_more_counts():
    m = W.movers(A, B, top=1, swept=["mb/scratch"])
    assert (m.up, m.down, m.more_up, m.more_down) == (
        [W.Mover("mb/ck/run1", 450 * GIB, "grew", "ann-a")],
        [W.Mover("mb/scratch", -400 * GIB, "swept", "bob-b")],
        1,
        2,
    )


def test_movers_threshold_and_descent_floor():
    # 1 TiB threshold: only the sweep-sized changes qualify... none do (max |Δ| is 500 GiB at mb/ck)
    assert W.movers(A, B, threshold=1024 * GIB) == W.Movers([], [], 0, 0)
    # 460 GiB threshold: mb/ck (+500) qualifies but its 450-GiB child does not, so the parent is reported
    assert W.movers(A, B, threshold=460 * GIB).up == [W.Mover("mb/ck", 500 * GIB, "grew", "ann-a")]


def test_table_totals_sum_every_slice():
    assert (A.bytes("mb/old"), A.bytes("mb/ck"), A.bytes("mb/nope")) == (300 * GIB, 1000 * GIB, 0)
    assert A.children("mb/ck") == ["mb/ck/run1", "mb/ck/run2"]


def test_owner_is_strict_majority_slice():
    assert A.owner("mb/old") == "bob-b"
    assert B.owner("mb/data") is None
    assert B.owner("mb/missing") is None


def test_short_name():
    assert [W.short_name(u) for u in ("calvin-xu", "ahmed-ahmed", None)] == ["Calvin", "Ahmed", "unowned"]


PRIOR_META = {"total_objects": 100, "total_bytes": 3_000_000_000_000_000, "class_bytes": {"1": 3_000_000_000_000_000}}
META = {"total_objects": 90, "total_bytes": 2_900_000_000_000_000, "class_bytes": {"1": 2_900_000_000_000_000}}
UNDO = int(dt.datetime(2026, 9, 18, 20, 18, tzinfo=dt.timezone.utc).timestamp())


def test_totals_from_meta():
    assert W.totals_from_meta(PRIOR_META, META) == W.Totals(
        objects=90, bytes=2_900_000_000_000_000, cost=54017,
        d_objects=-10, d_bytes=-100_000_000_000_000, d_cost=-1862,
    )


def test_swept_from_runs_windows_real_finished_runs():
    runs = [
        {"mode": "real", "finished_ts": 50, "deleted_objects": 1000, "deleted_bytes": 40_000_000_000_000, "undo_deadline": UNDO},
        {"mode": "real", "finished_ts": 60, "deleted_objects": 234, "deleted_bytes": 10_000_000_000_000, "undo_deadline": UNDO - 3600},
        {"mode": "dry", "finished_ts": 55, "deleted_objects": 5, "deleted_bytes": 5},          # dry: excluded
        {"mode": "real", "finished_ts": 10, "deleted_objects": 7, "deleted_bytes": 7},          # before the window
        {"mode": "real", "finished_ts": None, "deleted_objects": 9, "deleted_bytes": 9},        # unfinished
        {"mode": "real", "finished_ts": 58, "deleted_objects": 0, "deleted_bytes": 0},          # aborted: nothing deleted
    ]
    assert W.swept_from_runs(runs, since=10, until=60) == W.Swept(objects=1234, bytes=50_000_000_000_000, runs=2, undo_deadline=UNDO)


TOTALS = W.totals_from_meta(PRIOR_META, META)
SWEPT = W.Swept(objects=1234, bytes=50_000_000_000_000, runs=2, undo_deadline=UNDO)

EXPECTED = """**Weekly storage report** (UTC 2026-09-14) · [gcs.oa.dev ↗](https://gcs.oa.dev/?d=260914-7d#diff)
- totals: 90 objects · 2,900.0 TB (−100.0 TB, −3.3% vs 09-07) · $54,017/mo (−$1,862)
- swept this week: 1,234 objects · 50.0 TB in 2 runs · undo until 09-18

_Changes since 2026-09-07 (prefixes that moved ≥ 100 GiB):_

**Biggest increases:**
- `mb/ck/run1` +0.4 TiB (grew · Ann)
- `mb/new` +0.2 TiB (new · Ann)

**Biggest decreases:**
- `mb/scratch` −0.4 TiB (swept · Bob)
- `mb/old` −0.3 TiB (gone · Bob)
- `mb/data` −0.1 TiB (shrank · unowned)"""


def test_compose():
    assert W.compose(TOTALS, SWEPT, MOVERS, date="2026-09-14", prior="2026-09-07") == EXPECTED


def test_compose_without_sweeps_or_movers():
    text = W.compose(TOTALS, W.Swept(0, 0, 0, None), W.Movers([], [], 0, 0), date="2026-09-14", prior="2026-09-07")
    assert text.split("\n") == [
        "**Weekly storage report** (UTC 2026-09-14) · [gcs.oa.dev ↗](https://gcs.oa.dev/?d=260914-7d#diff)",
        "- totals: 90 objects · 2,900.0 TB (−100.0 TB, −3.3% vs 09-07) · $54,017/mo (−$1,862)",
        "",
        "_Changes since 2026-09-07 (prefixes that moved ≥ 100 GiB):_",
        "",
        "**Biggest increases:**",
        "- _(none)_",
        "",
        "**Biggest decreases:**",
        "- _(none)_",
    ]


def test_compose_fits_discord_limit_by_dropping_tail_movers():
    long = [W.Mover(f"mb/{'x' * 60}/{i:02d}", (30 - i) * GIB * 10, "grew", "ann-a") for i in range(30)]
    mv = W.Movers(up=long, down=list(long), more_up=0, more_down=0)
    text = W.compose(TOTALS, None, mv, date="2026-09-14", prior="2026-09-07")
    assert len(text) <= W.MESSAGE_LIMIT
    lines = text.split("\n")
    inc = lines.index("**Biggest increases:**")
    dec = lines.index("**Biggest decreases:**")
    ups = lines[inc + 1 : dec - 1]
    downs = lines[dec + 1 :]
    # equal-length lists drop from the decreases first; the survivors are the head of each list, in order
    assert ups == [W._line(m, W.short_name) for m in long[: len(ups) - 1]] + [f"- _(+{30 - (len(ups) - 1)} more in the report)_"]
    assert downs == [W._line(m, W.short_name) for m in long[: len(downs) - 1]] + [f"- _(+{30 - (len(downs) - 1)} more in the report)_"]
    assert len(ups) - len(downs) in (0, 1)
    assert len(ups) + len(downs) < 60


def test_read_table_takes_both_index_generations(tmp_path):
    """`read_table` builds the same `Table` from a pre-store index (dir rows,
    `b`/`o`) and a store sort (`kind`, `size`, `n_files`, object rows) —
    dir rows to MAX_DEPTH, objects left out."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [("mb", 1, None, 50 * GIB, 5), ("mb/x", 2, "ann-a", 30 * GIB, 3), ("mb/x", 2, None, 20 * GIB, 2), ("mb/a/b/c/d/e", 6, "ann-a", GIB, 1)]
    legacy = tmp_path / "legacy.parquet"
    pq.write_table(pa.table({
        "path": [r[0] for r in rows], "depth": [r[1] for r in rows], "usr": pa.array([r[2] for r in rows], pa.string()),
        "b": [r[3] for r in rows], "o": [r[4] for r in rows],
    }), legacy)
    store = tmp_path / "store.parquet"
    objs = [("mb/x/o1.bin", 3, "ann-a", 30 * GIB, 1), ("mb/o2.bin", 2, None, 20 * GIB, 1)]
    pq.write_table(pa.table({
        "path": [r[0] for r in rows + objs], "depth": [r[1] for r in rows + objs], "usr": pa.array([r[2] for r in rows + objs], pa.string()),
        "kind": ["dir"] * len(rows) + ["file"] * len(objs),
        "size": [r[3] for r in rows + objs], "n_files": [r[4] for r in rows + objs],
    }), store)
    for f in (legacy, store):
        t = W.read_table(str(f))
        assert (t.total, t.depth, t.slices) == (
            {"mb": 50 * GIB, "mb/x": 50 * GIB},
            {"mb": 1, "mb/x": 2},
            {"mb/x": {"ann-a": 30 * GIB}},
        )


def test_prior_scan_skips_to_the_newest_scan_at_least_a_week_back():
    dates = ["2026-09-01", "2026-09-05", "2026-09-06", "2026-09-07", "2026-09-13", "2026-09-14"]
    assert W.prior_scan(dates, "2026-09-14") == "2026-09-07"
    assert W.prior_scan(dates, "2026-09-13") == "2026-09-06"   # 9/6 is 7 days back exactly
    assert W.prior_scan(["2026-09-14"], "2026-09-14") is None
