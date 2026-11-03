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
from dt_cloud.level_sim import ROW_BYTES, Config, chunk_span, configs, fit, level_sim_cmd, merged, wave, wave_ms
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
