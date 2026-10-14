from types import SimpleNamespace

import duckdb
import pytest

from dt_cloud import static_anchors as an


@pytest.mark.parametrize("batch_keys, queries", [(1, 4), (256, 2)])
def test_prior_probes_are_exact_and_batched_by_shard(tmp_path, batch_keys, queries):
    con = duckdb.connect()
    con.execute("CREATE TABLE source(s VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf TIMESTAMP, vt TIMESTAMP, size BIGINT, n_files BIGINT)")
    rows = [
        ("foo", "b/a/x", 1), ("foo", "b/a/deep/y", 2),
        ("foo", "b/ab/not-covered", 3), ("foo", "b/z/é", 4),
        ("bar", "b/elsewhere/x", 5), ("bar", "b/a/x", 6),
        ("other", "b/a/x", 7),
    ]
    con.executemany("INSERT INTO source VALUES (?, 3, ?, NULL, TIMESTAMP '2026-10-01', TIMESTAMP '2027-01-01', ?, 1)", rows)
    file = str(tmp_path / "source.parquet")
    con.execute(f"COPY source TO {an.q(file)} (FORMAT PARQUET)")
    con.execute("CREATE TABLE near(k VARCHAR, dir VARCHAR)")
    con.executemany("INSERT INTO near VALUES (?, ?)", [
        ("foo", "b/a"), ("foo", "b/a/deep"), ("foo", "b/a"),
        ("foo", "b/z"), ("bar", ""), ("bar", "b/a"), ("missing", "b/a"),
    ])
    con.execute(f"CREATE TABLE prows({an.AR_COLS})")
    tier = SimpleNamespace(shard_file=lambda mode, key: file if key != "missing" else None)
    assert an.probe_prior_rows(con, [tier, tier], "end", batch_keys=batch_keys) == queries
    assert con.execute("SELECT k, path, size FROM prows ORDER BY k, path, size").fetchall() == [
        ("bar", "b/a/x", 6), ("bar", "b/a/x", 6),
        ("bar", "b/elsewhere/x", 5), ("bar", "b/elsewhere/x", 5),
        ("foo", "b/a/deep/y", 2), ("foo", "b/a/deep/y", 2),
        ("foo", "b/a/x", 1), ("foo", "b/a/x", 1),
        ("foo", "b/z/é", 4), ("foo", "b/z/é", 4),
    ]
    assert con.execute("SELECT k, lo, hi FROM probe_ranges ORDER BY k, lo").fetchall() == [
        ("bar", "", None), ("foo", "b/a/", "b/a0"),
        ("foo", "b/z/", "b/z0"), ("missing", "b/a/", "b/a0"),
    ]


def test_probe_batch_size_must_be_positive():
    with pytest.raises(ValueError, match="^batch_keys must be positive$"):
        an.probe_prior_rows(None, [], "end", batch_keys=0)
