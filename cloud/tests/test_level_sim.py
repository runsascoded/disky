"""`interval-store level-sim`: the coalescing primitives, and one read priced over a small real `level` file: 3 groups of 8
rows in pages of 2, keyed `(depth, path, ts, name)`.

    rg0: depth 1, path `a`, ts 10..17    rg1: depth 1, path `b`, ts 10..17    rg2: depth 2, path `a/x`, ts 10..17
"""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from dt_cloud.interval_level import LEVEL_SCHEMA
from dt_cloud.level_sim import ROW_BYTES, Config, FirstTouch, PageIx, SmallGroup, chunk_span, configs, fit, level_sim_cmd, merged, small_groups, wave, wave_ms
from dt_cloud.parquet_pages import chunk_indexes, page_locations


def test_merged_is_plan_runs():
    spans = [(100, 110), (0, 10), (15, 20), (2000, 2100)]
    assert merged(spans, 5, 1 << 62) == [(0, 20), (100, 110), (2000, 2100)]
    assert merged(spans, 100, 1 << 62) == [(0, 110), (2000, 2100)]
    # The cap stops a run; a span past it is still a run of its own.
    assert merged(spans, 100, 20) == [(0, 20), (100, 110), (2000, 2100)]
    assert merged([(0, 50)], 0, 10) == [(0, 50)]


def test_a_wave_is_list_scheduled_on_six_slots():
    mib = 1 << 20
    assert wave_ms([0] * 6, 100, 10) == 100
    assert wave_ms([0] * 7, 100, 10) == 200
    # One 10 MiB get at 10 MiB/s, five small beside it: the 7th takes the first free slot, not the big one's.
    assert wave_ms([10 * mib] + [0] * 6, 100, 10) == 1100
    assert wave_ms([0] * 6 + [10 * mib], 100, 10) == 1200


def test_fit_closes_a_gap_only_when_it_saves_a_round_within_the_budget():
    k = 1 << 10
    m = 1 << 20
    # 12 spans, 200 KiB apart: 2 rounds of 6. Six closed gaps (1.2 MiB, ~42 ms) leave 6 runs, one round.
    spans = [(i * 201 * k, i * 201 * k + k) for i in range(12)]
    assert fit([spans], 1 << 40, 195, 28.6) == [[(spans[0][0], spans[6][1]), *spans[7:]]]
    # 1 MiB apart: closing six costs 6 MiB (~210 ms) on one get, more than the round: as they are, budget or not.
    far = [(i * (m + k), i * (m + k) + k) for i in range(12)]
    assert fit([far], 1 << 40, 195, 28.6) == [far]
    # Within 1 MiB of bytes those six gaps don't fit either.
    assert fit([spans], m, 195, 28.6) == [spans]
    # Under the floor a gap always closes, and a wave within one round's slots gains nothing from any other.
    assert fit([[(0, k), (2 * k, 3 * k), (m, m + k)]], 1 << 40, 195, 28.6) == [[(0, 3 * k), (m, m + k)]]


def test_configs_cross_gaps_and_caps_then_budgets():
    assert [c.label for c in configs("512K,2M", "2M,none", "16M")] == ["g512K/c2M", "g2M/c2M", "g512K/c∞", "g2M/c∞", "fit:16M"]
    assert configs("1M", "4M", "")[0] == Config("g1M/c4M", 1 << 20, 4 << 20)


def test_wave_counts_rounds_at_six():
    got = wave([[(i * 10 ** 9, i * 10 ** 9 + 10) for i in range(7)]], Config("x"), 100, 10)
    assert (got["gets"], got["rounds"], got["bytes"], got["max_get"]) == (7, 2, 70, 10)


def _level_file(store: Path) -> tuple[Path, list[list[dict]]]:
    served = store / "served"
    served.mkdir(parents=True)
    out = served / "level.parquet"

    def group(depth: int, path: str) -> pa.Table:
        n = 8
        return pa.table({
            "depth": [depth] * n, "path": [path] * n, "ts": list(range(10, 18)), "name": [f"c{i}" for i in range(n)], "op": [1] * n, "kind": ["file"] * n, "kind_prev": ["file"] * n,
            "size": [1] * n, "n_files": [1] * n, "size_prev": [0] * n, "n_files_prev": [1] * n, "churn_cum": [1] * n, "churn_prev": [0] * n,
        }, schema=LEVEL_SCHEMA)

    keys = [(1, "a"), (1, "b"), (2, "a/x")]
    with pq.ParquetWriter(out, LEVEL_SCHEMA, use_dictionary=False, compression="none", data_page_size=1, write_batch_size=2, write_page_index=True) as w:
        for d, p in keys:
            w.write_table(group(d, p), row_group_size=8)
    md = pq.read_metadata(out)
    rows, cmeta = [], []
    for g, (ixs, (d, p)) in enumerate(zip(chunk_indexes(out), keys)):
        rg = md.row_group(g)
        cols = [[rg.column(c).data_page_offset, rg.column(c).total_compressed_size, rg.column(c).dictionary_page_offset or 0, *ix] for c, ix in enumerate(ixs)]
        cmeta.append(cols)
        rows.append({"rg": g, "d_min": d, "d_max": d, "p_min": p, "p_max": p, "vf_min": 10, "vf_max": 17, "vt_max": 17, "rg_json": json.dumps([8, "UNCOMPRESSED", cols])})
    pq.write_table(pa.Table.from_pylist(rows), served / "level.groups.parquet")
    (store / "scans.json").write_text(json.dumps({"scans": [{"id": f"s{t}", "ts": t} for t in range(10, 18)]}))
    return out, cmeta


def test_a_read_priced_over_a_real_file(tmp_path):
    store = tmp_path / "gen"
    out, cmeta = _level_file(store)
    reqs = tmp_path / "reqs.jsonl"
    # `a` over (s11, s13]: rows with ts 12, 13 of rg0: row indexes 2, 3, one page of 2 in every column.
    reqs.write_text(json.dumps({"tag": 7, "from": "s11", "to": "s13", "dirs": ["a"]}) + "\n")
    res = tmp_path / "out.jsonl"
    r = CliRunner().invoke(level_sim_cmd, ["-c", str(tmp_path / "c"), "-g", "0,1G", "-C", "none", "-b", "1G", "-o", str(res), str(store), str(reqs)], catch_exceptions=False)
    assert r.exit_code == 0
    got = json.loads(res.read_text())
    ixs = chunk_indexes(out)[0]
    locs = [page_locations(out, ix) for ix in ixs]
    assert [len(x) for x in locs] == [4] * 13
    # Page 1 (rows 2-3) of each of the 13 columns.
    page_bytes = [(x[1].offset, x[1].offset + x[1].compressed_page_size) for x in locs]
    needed = sum(e - s for s, e in merged(page_bytes, 0, 1 << 62))
    index_spans = [(ix.column_index_offset, ix.column_index_offset + ix.column_index_length) for ix in ixs[:3]] + [(ix.offset_index_offset, ix.offset_index_offset + ix.offset_index_length) for ix in ixs]
    index_needed = sum(e - s for s, e in merged(index_spans, 0, 1 << 62))
    assert {k: got[k] for k in ("tag", "dirs", "groups", "pages", "rows", "needed_bytes", "index_needed_bytes", "decoded_bytes")} == {
        "tag": 7, "dirs": 1, "groups": 1, "pages": 1, "rows": 2, "needed_bytes": needed, "index_needed_bytes": index_needed, "decoded_bytes": 2 * ROW_BYTES,
    }
    assert sorted(got["cfg"]) == sorted(["g0K/c∞", "g1024M/c∞", "fit:1G"])
    # A gap covering everything: one get a wave.
    assert (got["cfg"]["g1024M/c∞"]["index"]["gets"], got["cfg"]["g1024M/c∞"]["data"]["gets"]) == (1, 1)
    assert got["cfg"]["g1024M/c∞"]["data"]["bytes"] == max(e for _, e in page_bytes) - min(s for s, _ in page_bytes)
    # No index wave: the group's chunks whole, every row of it.
    assert got["whole"]["rows"] == 8
    s, e = chunk_span(dict(zip(range(13), cmeta[0])))
    assert (got["whole"]["data"]["gets"], got["whole"]["data"]["bytes"]) == (1, e - s)


def test_small_groups_are_a_real_groups_pages_stored_contiguously():
    # Two columns' worth of pages isn't a level file's 13: every column carries the same 4 pages of 2 rows, 10 bytes each; `name` has
    # no dictionary, the rest a dictionary page of 3 bytes before their first page.
    cols = ["depth", "path", "ts", "name", "op", "kind", "kind_prev", "size", "n_files", "size_prev", "n_files_prev", "churn_cum", "churn_prev"]
    chunks = {c: [1000 * (k + 1) + 3, 43, 1000 * (k + 1), 0, 0, 0, 0] for k, c in enumerate(cols)}
    locs = {c: [(1000 * (k + 1) + 3 + 10 * i, 10, 2 * i) for i in range(4)] for k, c in enumerate(cols)}
    ix = PageIx(locs, [(1, 1)] * 4, [("a", "a"), ("a", "b"), ("b", "b"), ("c", "c")], [(10, 11), (12, 13), (14, 15), (16, 17)])
    each = 3 + 10
    assert small_groups(ix, chunks, 8, 2) == [
        SmallGroup(2, 1000 + 13 * each * i, 1000 + 13 * each * (i + 1), 1, 1, *p, *t, (each,) * 13)
        for i, (p, t) in enumerate(zip(ix.path, ix.ts))
    ]
    two = 3 + 20
    assert small_groups(ix, chunks, 8, 4) == [
        SmallGroup(4, 1000, 1000 + 13 * two, 1, 1, "a", "b", 10, 13, (two,) * 13),
        SmallGroup(4, 1000 + 13 * two, 1000 + 26 * two, 1, 1, "b", "c", 14, 17, (two,) * 13),
    ]
    # A size the pages don't divide can't be read from them.
    try:
        small_groups(ix, chunks, 8, 3)
    except ValueError as e:
        assert str(e) == "pages at rows [0, 2] don't divide 3-row groups"
    else:
        raise AssertionError("3-row groups from 2-row pages")


def test_first_touch_holds_from_the_next_round_and_joins_within_one():
    s = FirstTouch()
    assert s.split((0, 0), ["a", "b"]) == (["a", "b"], [])
    s.read(["a", "b"], 200.0)
    # Beside it: another batch joins, waiting for it.
    assert s.split((0, 0), ["b", "c"]) == (["c"], ["b"])
    assert s.wait(["b"]) == 200.0
    s.read(["c"], 100.0)
    # The next round: everything read is held.
    assert s.split((0, 1), ["a", "b", "c", "d"]) == (["d"], [])
    assert s.wait([]) == 0.0


def test_the_mode_table_prices_a_read_of_a_real_file(tmp_path):
    store = tmp_path / "gen"
    out, _ = _level_file(store)
    reqs = tmp_path / "reqs.jsonl"
    reqs.write_text("".join(json.dumps({"case": 0, "level": lv, "batch": 0, "round": 0, "from": "s11", "to": "s13", "dirs": ["a"]}) + "\n" for lv in (0, 1)))
    table = tmp_path / "modes.jsonl"
    r = CliRunner().invoke(level_sim_cmd, ["-c", str(tmp_path / "c"), "-g", "512K", "-C", "2M", "-T", "100", "-R", "1000000", "-s", "2,4", "-F", "2", "-O", str(table), "-o", str(tmp_path / "o.jsonl"), str(store), str(reqs)], catch_exceptions=False)
    assert r.exit_code == 0
    got = {m["mode"]: {k: v for k, v in m.items() if k in ("gets", "rounds", "rows", "lookups", "first_p50", "full_p50")} for m in map(json.loads, table.read_text().splitlines())}
    # Rounded up to the slots' ms: the throughput is effectively free here, a wave of one get is 100 ms.
    got = {k: {**v, "first_p50": round(v["first_p50"]), "full_p50": round(v["full_p50"])} for k, v in got.items()}
    # The fixture's sidecar is one footer group; the index and the data are one run each. Two levels, each the same read.
    assert got == {
        # Each level: index + data. The footer group, the first level's.
        "8K pages, today": {"gets": 1 + 2 + 2, "rounds": 1 + 2 + 2, "rows": 4, "lookups": 1, "first_p50": 305, "full_p50": 505},
        # Index and footer group once, a colo lookup each (5 ms) ahead of them.
        "8K pages, held, cold": {"gets": 1 + 1 + 2, "rounds": 1 + 1 + 2, "rows": 4, "lookups": 2, "first_p50": 305, "full_p50": 405},
        "8K pages, held, colo warm": {"gets": 2, "rounds": 2, "rows": 4, "lookups": 2, "first_p50": 105, "full_p50": 205},
        "8K pages, held, warm": {"gets": 2, "rounds": 2, "rows": 4, "lookups": 0, "first_p50": 100, "full_p50": 200},
        # 2 rows: page 1 alone; a footer group of 2 rows holds its ordinal.
        "2 rows/F2, cold": {"gets": 1 + 2, "rounds": 1 + 2, "rows": 4, "lookups": 1, "first_p50": 205, "full_p50": 305},
        "2 rows/F2, colo warm": {"gets": 2, "rounds": 2, "rows": 4, "lookups": 1, "first_p50": 105, "full_p50": 205},
        "2 rows, warm": {"gets": 2, "rounds": 2, "rows": 4, "lookups": 0, "first_p50": 100, "full_p50": 200},
        # 4 rows: pages 0 and 1 are one group: 4 rows a read.
        "4 rows/F2, cold": {"gets": 1 + 2, "rounds": 1 + 2, "rows": 8, "lookups": 1, "first_p50": 205, "full_p50": 305},
        "4 rows/F2, colo warm": {"gets": 2, "rounds": 2, "rows": 8, "lookups": 1, "first_p50": 105, "full_p50": 205},
        "4 rows, warm": {"gets": 2, "rounds": 2, "rows": 8, "lookups": 0, "first_p50": 100, "full_p50": 200},
    }
