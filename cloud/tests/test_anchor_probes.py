from types import SimpleNamespace

import duckdb
import pyarrow.parquet as pq
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


def test_probe_join_state_is_bounded_by_the_current_key_batch(tmp_path):
    import json

    con = duckdb.connect()
    con.execute("CREATE TABLE source AS SELECT CASE WHEN i < 400 THEN 'foo' ELSE 'bar' END AS s, 3::UTINYINT AS depth, 'b/a/' || i AS path, NULL::VARCHAR AS usr, TIMESTAMP '2026-10-01' AS vf, TIMESTAMP '2027-01-01' AS vt, 1::BIGINT AS size, 1::BIGINT AS n_files FROM range(800) AS r(i)")
    file = str(tmp_path / 'source.parquet')
    con.execute(f'COPY source TO {an.q(file)} (FORMAT PARQUET)')
    con.execute("CREATE TABLE near AS SELECT 'unused' || i AS k, 'b/a' AS dir FROM range(20) AS r(i) UNION ALL SELECT 'foo', 'b/a' UNION ALL SELECT 'bar', 'b/a'")
    con.execute(f'CREATE TABLE prows({an.AR_COLS})')
    profile = tmp_path / 'profile.json'
    con.execute("SET enable_profiling='json'")
    con.execute(f'SET profiling_output={an.q(str(profile))}')
    right_rows = []

    def joins(node):
        if node.get('operator_name') == 'ASOF_JOIN':
            yield node
        for child in node.get('children', []):
            yield from joins(child)

    class Observed:
        def register(self, name, table):
            return con.register(name, table)

        def unregister(self, name):
            return con.unregister(name)

        def execute(self, sql):
            result = con.execute(sql)
            if sql.startswith('INSERT INTO prows'):
                right_rows.extend(node['children'][1]['operator_cardinality'] for node in joins(json.loads(profile.read_text())))
            return result

    tier = SimpleNamespace(shard_file=lambda mode, key: file if key in ('foo', 'bar') else None)
    assert an.probe_prior_rows(Observed(), [tier], 'end', batch_keys=1) == 2
    assert right_rows == [1, 1]
    assert con.execute('SELECT k, count(*) FROM prows GROUP BY k ORDER BY k').fetchall() == [('bar', 400), ('foo', 400)]


def test_candidate_bounds_include_unknown_statistics_and_exact_edges():
    con = duckdb.connect()
    con.execute('CREATE TABLE cand(k VARCHAR, lvl INTEGER, dir VARCHAR, n BIGINT)')
    con.executemany('INSERT INTO cand VALUES (?, ?, ?, ?)', [
        ('foo', 0, '', 1), ('foo', 1, 'a', 1), ('foo', 1, 'b', 1),
        ('foo', 1, 'c', 1), ('missing', 0, '', 1),
    ])
    con.execute('CREATE TABLE ktk(k VARCHAR, p_min VARCHAR, p_max VARCHAR, rows BIGINT)')
    con.executemany('INSERT INTO ktk VALUES (?, ?, ?, ?)', [
        ('foo', None, None, 2), ('foo', 'a/a', 'a/m', 3),
        ('foo', 'a/z', 'b/c', 5), ('foo', 'b/a', 'b/z', 7),
        ('foo', 'b/z', None, 11), ('foo', None, 'a/a', 13),
        ('foo', 'b/a', 'b/z', 17),
        ('other', None, None, 999),
    ])
    an.near_candidates(con, 0)
    assert con.execute('SELECT * FROM near ORDER BY k, lvl, dir').fetchall() == [
        ('foo', 0, '', 1, 58), ('foo', 1, 'a', 1, 23),
        ('foo', 1, 'b', 1, 42), ('foo', 1, 'c', 1, 13),
        ('missing', 0, '', 1, 0),
    ]
    an.near_candidates(con, 24)
    assert con.execute('SELECT * FROM near ORDER BY k, lvl, dir').fetchall() == [
        ('foo', 0, '', 1, 58), ('foo', 1, 'b', 1, 42),
    ]


def test_run_tiers_bound_by_their_pure_groups_and_the_keys_other_rows():
    """The base by its groups; the run tiers by their pure groups (only the key's rows) meeting the range, plus the key's
    rows outside them (its count less the pure groups'), at most its count. A mixed run group spans other keys' paths,
    so it would meet every `dir` with all its rows: `foo` has only 3 rows in the runs, all in mixed groups; `big` 5,000,
    4,300 in pure groups under `b/` and `c/`, so 700 elsewhere."""
    con = duckdb.connect()
    con.execute('CREATE TABLE cand(k VARCHAR, lvl INTEGER, dir VARCHAR, n BIGINT)')
    con.executemany('INSERT INTO cand VALUES (?, ?, ?, ?)', [
        ('foo', 0, '', 1), ('foo', 1, 'a', 1), ('foo', 1, 'b', 1), ('big', 1, 'b', 1), ('big', 1, 'c', 1), ('big', 1, 'd', 1)])
    con.execute('CREATE TABLE ktk(k VARCHAR, t INTEGER, s_min VARCHAR, s_max VARCHAR, p_min VARCHAR, p_max VARCHAR, rows BIGINT)')
    con.executemany('INSERT INTO ktk VALUES (?, ?, ?, ?, ?, ?, ?)', [
        ('foo', 0, 'foo', 'foo', 'a/a', 'a/m', 200),   # the base: only foo's rows, under a/
        ('foo', 1, 'a', 'zz', 'a', 'z/z', 8000),       # a run: a group of many keys
        ('foo', 2, 'a', 'zz', 'a', 'z/z', 8000),       # another
        ('big', 1, 'a', 'zz', 'a', 'z/z', 8000),       # the first, as big's
        ('big', 1, 'big', 'big', 'b/a', 'b/z', 300),   # run groups of big's own rows
        ('big', 2, 'big', 'big', 'c/a', 'c/z', 4000),
    ])
    con.execute('CREATE TABLE rc(k VARCHAR, n BIGINT)')
    con.executemany('INSERT INTO rc VALUES (?, ?)', [('foo', 3), ('big', 5000)])
    an.near_candidates(con, 100, "rc")
    assert con.execute('SELECT * FROM near ORDER BY k, lvl, dir').fetchall() == [
        ('big', 1, 'b', 1, 1000), ('big', 1, 'c', 1, 4700), ('big', 1, 'd', 1, 700),
        ('foo', 0, '', 1, 203), ('foo', 1, 'a', 1, 203),
    ]
    an.near_candidates(con, 100, None)
    assert con.execute('SELECT k, dir, bound FROM near ORDER BY k, lvl, dir').fetchall() == [
        ('big', 'b', 8300), ('big', 'c', 12000), ('big', 'd', 8000), ('foo', '', 16200), ('foo', 'a', 16200), ('foo', 'b', 16000),
    ]


def test_candidate_bounds_scale_with_many_directories_of_one_key():
    con = duckdb.connect()
    con.execute("CREATE TABLE cand AS SELECT 'key' AS k, 1 AS lvl, printf('b/%06d', i) AS dir, 1::BIGINT AS n FROM range(20000) AS r(i)")
    con.execute("CREATE TABLE ktk AS SELECT k, dir || '/a' AS p_min, dir || '/z' AS p_max, 10::BIGINT AS rows FROM cand")
    an.near_candidates(con, 10)
    assert con.execute('SELECT count(*), min(bound), max(bound), sum(bound) FROM near').fetchone() == (20000, 10, 10, 200000)


def test_tier_reads_each_immutable_shard_plan_once(monkeypatch, tmp_path):
    import json
    from pathlib import Path

    plan = tmp_path / 'shards.json'
    plan.write_text(json.dumps({'shards': [{'i': 7, 'lo': 'aaa', 'hi': 'mmm'}, {'i': 19, 'lo': 'mmm', 'hi': None}]}) + '\n')
    read_text = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    tier = an.Tier(tmp_path)
    keys = ['000', 'aaa', 'aaaz', 'lll', 'mmm', 'zzz', 'é']
    expected = [None, *[str(tmp_path / 'sx/s0007.parquet')] * 3, *[str(tmp_path / 'sx/s0019.parquet')] * 3]
    assert [tier.shard_file('end', key) for key in keys] == expected
    assert [tier.shard_file('end', key) for key in keys] == expected
    assert reads == [plan]


def test_prior_dir_counts_match_the_ancestor_expansion():
    """`pd` by running counts equals counting each distinct version at each of its ancestors (`_level_dirs`), including
    a version listed by two tiers, a sibling sharing a prefix (`a/b` vs `a/bc`), and a near with no versions under it."""
    con = duckdb.connect()
    con.execute("CREATE TABLE prows (k VARCHAR, depth INTEGER, path VARCHAR, usr VARCHAR, vf BIGINT)")
    con.execute("""INSERT INTO prows VALUES
        ('q', 3, 'a/b/x', '', 1), ('q', 3, 'a/b/x', '', 1), ('q', 3, 'a/b/x', '', 2), ('q', 3, 'a/b/y', 'u', 1),
        ('q', 3, 'a/bc/z', '', 1), ('q', 2, 'a/w', '', 1), ('q', 3, 'c/d/e', '', 1), ('r', 2, 'a/b', '', 1)""")
    con.execute("CREATE TABLE near (k VARCHAR, lvl INTEGER, dir VARCHAR, n BIGINT)")
    con.execute("""INSERT INTO near VALUES ('q', 0, '', 1), ('q', 1, 'a', 1), ('q', 2, 'a/b', 1), ('q', 2, 'a/bc', 1),
        ('q', 1, 'c', 1), ('q', 1, 'z', 1), ('r', 1, 'a', 1), ('s', 0, '', 1)""")
    an.prior_dir_counts(con)
    got = con.execute("SELECT k, lvl, dir, n FROM pd ORDER BY k, lvl, dir").fetchall()
    con.execute("CREATE TABLE pvx AS SELECT DISTINCT k, depth, path, usr, vf FROM prows")
    want = dict(((k, lvl, d), n) for k, lvl, d, n in con.execute(f"""SELECT x.k, x.lvl, x.dir, count(*) FROM ({an._level_dirs('pvx')}) AS x
        SEMI JOIN near USING (k, lvl, dir) GROUP BY ALL""").fetchall())
    assert got == [
        ("q", 0, "", 6), ("q", 1, "a", 5), ("q", 1, "c", 1), ("q", 1, "z", 0), ("q", 2, "a/b", 3), ("q", 2, "a/bc", 1),
        ("r", 1, "a", 1), ("s", 0, "", 0),
    ]
    assert [(k, lvl, d, want.get((k, lvl, d), 0)) for k, lvl, d, _ in got] == got



@pytest.mark.parametrize("batch_keys, queries", [(1, 8), (2, 4), (256, 2)])
def test_start_probes_read_every_row_under_each_near_prefix_in_key_batches(tmp_path, batch_keys, queries):
    """Each near key gets every prior row whose name starts with it (nested keys `abc` / `abcd` both), once per tier,
    whatever the batching; a row between two batched keys that starts with neither is read (the batch's `s` range) but
    never joined."""
    con = duckdb.connect()
    con.execute("CREATE TABLE source(s VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf TIMESTAMP, vt TIMESTAMP, size BIGINT, n_files BIGINT)")
    rows = [("abc", "b/1", 1), ("abcd", "b/2", 2), ("abcde", "b/3", 3), ("abd", "b/4", 4), ("abx", "b/5", 5), ("xyz", "b/6", 6), ("xyzé", "b/7", 7)]
    con.executemany("INSERT INTO source VALUES (?, 3, ?, NULL, TIMESTAMP '2026-10-01', TIMESTAMP '2027-01-01', ?, 1)", rows)
    file = str(tmp_path / "source.parquet")
    con.execute(f"COPY source TO {an.q(file)} (FORMAT PARQUET)")
    con.execute(f"CREATE TABLE prows({an.AR_COLS})")
    tier = SimpleNamespace(shard_file=lambda mode, key: file if key != "missing" else None)
    assert an.probe_start_rows(con, [tier, tier], ["abc", "abcd", "abx", "missing", "xyz"], batch_keys=batch_keys) == queries
    assert con.execute("SELECT k, size, count(*) FROM prows GROUP BY ALL ORDER BY k, size").fetchall() == [
        ("abc", 1, 2), ("abc", 2, 2), ("abc", 3, 2),
        ("abcd", 2, 2), ("abcd", 3, 2),
        ("abx", 5, 2),
        ("xyz", 6, 2), ("xyz", 7, 2),
    ]


def test_start_probe_batch_size_must_be_positive():
    with pytest.raises(ValueError, match="^batch_keys must be positive$"):
        an.probe_start_rows(None, [], [], batch_keys=0)


def test_probe_batches_split_where_the_rows_between_keys_pass_the_gap(tmp_path):
    """Keys `k00`..`k99`, 100 rows each, one 100-row group per key: near keys within `gap_rows` of each other share a
    query's `s` range, farther ones get their own, so a batch never reads the shard between sparse keys."""
    con = duckdb.connect()
    con.execute("""CREATE TABLE source AS SELECT printf('k%02d', i // 100) AS s, 3::UTINYINT AS depth, printf('b/a/%04d', i) AS path,
        NULL::VARCHAR AS usr, TIMESTAMP '2026-10-01' AS vf, TIMESTAMP '2027-01-01' AS vt, 1::BIGINT AS size, 1::BIGINT AS n_files
        FROM range(10000) AS r(i) ORDER BY s, path""")
    file = str(tmp_path / "source.parquet")
    pq.write_table(con.execute("SELECT * FROM source").to_arrow_table(), file, row_group_size=100)  # DuckDB's COPY rounds groups up to its vectors
    keys = ["k00", "k01", "k03", "k10", "k11", "k50", "k99"]
    assert an.key_clusters(con, file, keys, gap_rows=100) == [["k00", "k01", "k03"], ["k10", "k11"], ["k50"], ["k99"]]
    assert an.key_clusters(con, file, keys, gap_rows=0) == [["k00", "k01"], ["k03"], ["k10", "k11"], ["k50"], ["k99"]]
    assert an.key_clusters(con, file, keys, gap_rows=100, batch_keys=2) == [["k00", "k01"], ["k03"], ["k10", "k11"], ["k50"], ["k99"]]
    assert an.key_clusters(con, file, keys, gap_rows=10_000) == [keys]
    con.execute("CREATE TABLE near AS SELECT unnest(?) AS k, 'b/a' AS dir", [keys])
    con.execute(f"CREATE TABLE prows({an.AR_COLS})")
    tier = SimpleNamespace(shard_file=lambda mode, key: file)
    assert an.probe_prior_rows(con, [tier], "end") == 1
    assert con.execute("SELECT k, count(*) FROM prows GROUP BY k ORDER BY k").fetchall() == [(k, 100) for k in keys]


def test_probe_batches_read_only_the_row_groups_meeting_their_keys(tmp_path):
    """Keys `k00`..`k99`, 100 rows each, one 100-row group per key (`k50`'s split over two): a batch reads its keys'
    groups, never those between, up to `max_rows` (a key's groups are never split) and `batch_keys`; keys in no group are
    dropped."""
    con = duckdb.connect()
    con.execute("""CREATE TABLE source AS SELECT printf('k%02d', i // 100) AS s, 3::UTINYINT AS depth, printf('b/a/%04d', i) AS path,
        NULL::VARCHAR AS usr, TIMESTAMP '2026-10-01' AS vf, TIMESTAMP '2027-01-01' AS vt, 1::BIGINT AS size, 1::BIGINT AS n_files
        FROM range(10000) AS r(i) ORDER BY s, path""")
    file = str(tmp_path / "source.parquet")
    table = con.execute("SELECT * FROM source").to_arrow_table()
    with pq.ParquetWriter(file, table.schema) as w:
        for lo, hi in [(i, i + 100) for i in range(0, 5000, 100)] + [(5000, 5050), (5050, 5100)] + [(i, i + 100) for i in range(5100, 10000, 100)]:
            w.write_table(table.slice(lo, hi - lo))
    keys = ["k00", "k01", "k03", "k10", "k11", "k50", "k99", "zz"]
    assert an.key_group_batches(file, keys) == [([0, 1, 3, 10, 11, 50, 51, 100], keys[:-1])]
    assert an.key_group_batches(file, keys, max_rows=250) == [
        ([0, 1], ["k00", "k01"]), ([3, 10], ["k03", "k10"]), ([11, 50, 51], ["k11", "k50"]), ([100], ["k99"])]
    assert an.key_group_batches(file, ["k49", "k50"], max_rows=50) == [([49], ["k49"]), ([50, 51], ["k50"])]
    assert an.key_group_batches(file, keys, batch_keys=3) == [([0, 1, 3], ["k00", "k01", "k03"]), ([10, 11, 50, 51], ["k10", "k11", "k50"]), ([100], ["k99"])]
    con.execute("CREATE TABLE near AS SELECT unnest(?) AS k, 'b/a' AS dir", [keys[:-1]])
    con.execute(f"CREATE TABLE prows({an.AR_COLS})")
    tier = SimpleNamespace(shard_file=lambda mode, key: file)
    assert an.probe_prior_rows(con, [tier], "end", batch_keys=3) == 3
    assert con.execute("SELECT k, count(*) FROM prows GROUP BY k ORDER BY k").fetchall() == [(k, 100) for k in keys[:-1]]


@pytest.mark.parametrize("runs", [False, True])
def test_level_pruning_keeps_exactly_the_unpruned_near(runs):
    """`near_candidates` bounds a level's candidates only under parents that can pass R (or weren't candidates: already
    heavy); `n + bound` shrinks parent → child, so that prunes nothing that would pass. Random directory trees per key
    (`cand` with the ancestors a run's rows give, some missing as already heavy), random groups (pure and mixed, null
    statistics): equal to bounding every candidate."""
    import random

    rng = random.Random(7)
    con = duckdb.connect()
    con.execute("CREATE TABLE cand(k VARCHAR, lvl INTEGER, dir VARCHAR, n BIGINT)")
    con.execute("CREATE TABLE ktk(k VARCHAR, t INTEGER, s_min VARCHAR, s_max VARCHAR, p_min VARCHAR, p_max VARCHAR, rows BIGINT)")
    con.execute("CREATE TABLE rc(k VARCHAR, n BIGINT)")
    for key in ("k1", "k2", "k3"):
        # the run's rows (paths), and each of their directories with its rows under it, as `rd` counts them
        paths = ["/".join(rng.choice("abc") for _ in range(rng.randint(2, 5))) for _ in range(300)]
        under: dict[str, int] = {}
        for p in paths:
            parts = p.split("/")
            for i in range(len(parts)):
                d = "/".join(parts[:i])
                under[d] = under.get(d, 0) + 1
        dirs = set(under)
        cand = [(key, 0 if d == "" else d.count("/") + 1, d, under[d]) for d in sorted(dirs) if rng.random() > 0.1]
        con.executemany("INSERT INTO cand VALUES (?, ?, ?, ?)", cand)
        for _ in range(30):
            lo, hi = sorted(rng.choice(sorted(dirs - {""})) + "/" + rng.choice("xyz") for _ in range(2))
            lo, hi = (None if rng.random() < 0.05 else lo), (None if rng.random() < 0.05 else hi)
            s_min, s_max = (key, key) if rng.random() < 0.5 else ("k0", "k9")  # pure, or mixed
            con.execute("INSERT INTO ktk VALUES (?, ?, ?, ?, ?, ?, ?)", (key, rng.choice([0, 1, 2]), s_min, s_max, lo, hi, rng.randint(1, 60)))
        con.execute("INSERT INTO rc VALUES (?, ?)", (key, rng.randint(0, 100)))
    rc = "rc" if runs else None
    for R in (0, 100, 300, 600):
        an.near_candidates(con, R, rc)
        got = con.execute("SELECT * FROM near ORDER BY ALL").fetchall()
        # unpruned: every candidate bounded
        if runs:
            an.group_prefix(con, "SELECT * FROM ktk WHERE t = 0", "gb")
            an.group_prefix(con, "SELECT * FROM ktk WHERE t > 0 AND s_min = s_max", "gr")
            an.group_bound(con, "gb", "cand", "wb")
            an.group_bound(con, "gr", "cand", "wr")
            runs = "least(r.bound + greatest(coalesce(rc.n, 0) - coalesce(pt.n, 0), 0), coalesce(rc.n, 0))"
            want = con.execute(f"""SELECT b.k, b.lvl, b.dir, b.n, b.bound + {runs} FROM wb AS b JOIN wr AS r USING (k, lvl, dir)
                LEFT JOIN rc USING (k) LEFT JOIN gr_t AS pt USING (k) WHERE b.n + b.bound + {runs} > {R} ORDER BY ALL""").fetchall()
        else:
            an.group_prefix(con, "SELECT * FROM ktk", "g")
            an.group_bound(con, "g", "cand", "w")
            want = con.execute(f"SELECT * FROM w WHERE n + bound > {R} ORDER BY ALL").fetchall()
        assert got == want
        assert got or R >= 300  # a vacuous comparison would prove nothing


def test_a_heavy_keys_probe_reads_only_the_groups_meeting_its_ranges(tmp_path):
    """Key `k`'s 2,000 rows in 200 groups of 10 (group g: `d/g<g>/x<j>`), probe ranges under `d/g010`, `d/g011` and
    `d/g150`: the spans `island_spans` reads are those groups' (10 and 11 joined), the probe keeps exactly their rows,
    and a probe range nothing meets reads nothing."""
    con = duckdb.connect()
    con.execute("""CREATE TABLE source AS SELECT 'k' AS s, 3::UTINYINT AS depth, printf('d/g%03d/x%d', i // 10, i % 10) AS path,
        NULL::VARCHAR AS usr, TIMESTAMP '2026-10-01' AS vf, TIMESTAMP '2027-01-01' AS vt, 1::BIGINT AS size, 1::BIGINT AS n_files
        FROM range(2000) AS r(i) ORDER BY path""")
    file = str(tmp_path / "source.parquet")
    pq.write_table(con.execute("SELECT * FROM source").to_arrow_table(), file, row_group_size=10)
    con.execute("CREATE TABLE near AS SELECT 'k' AS k, unnest(['d/g010', 'd/g011', 'd/g150']) AS dir")
    con.execute(f"CREATE TABLE prows({an.AR_COLS})")
    tier = SimpleNamespace(shard_file=lambda mode, key: file)
    assert an.probe_prior_rows(con, [tier], "end") == 1
    assert an.island_spans(con, "k", an.group_bounds(file)) == [("d/g010/x0", "d/g011/x9", 2), ("d/g150/x0", "d/g150/x9", 1)]
    assert con.execute("SELECT path FROM prows ORDER BY path").fetchall() == [
        (f"d/g{g:03d}/x{j}",) for g in (10, 11, 150) for j in range(10)]
    con.execute("DELETE FROM prows; DELETE FROM near; INSERT INTO near VALUES ('k', 'e/none')")
    assert an.probe_prior_rows(con, [tier], "end") == 0
    assert con.execute("SELECT count(*) FROM prows").fetchone()[0] == 0


def test_a_heavy_keys_groups_shared_with_its_neighbors_read_once(tmp_path):
    """Key `k`'s 1,992 rows (`d/g<g>/x<j>`) between neighbors `a` (5 rows under `z/`) and `m` (5 under `a/`), 10 rows a
    group: `k`'s first and last groups are shared, their path statistics spanning the neighbors' paths. Their rows of `k`
    are bounded by the next / previous group of `k` alone (`key_paths`), so a probe under `d/g000` and `d/g199` reads
    two disjoint spans and keeps each row once."""
    con = duckdb.connect()
    con.execute("""CREATE TABLE source AS SELECT * FROM (
            SELECT 'a' AS s, printf('z/%d', i) AS path FROM range(5) AS r(i)
            UNION ALL SELECT 'k', printf('d/g%03d/x%d', i // 10, i % 10) FROM range(5, 1997) AS r(i)
            UNION ALL SELECT 'm', printf('a/%d', i) FROM range(5) AS r(i))
        ORDER BY s, path""")
    con.execute("""CREATE TABLE src AS SELECT s, 3::UTINYINT AS depth, path, NULL::VARCHAR AS usr, TIMESTAMP '2026-10-01' AS vf,
        TIMESTAMP '2027-01-01' AS vt, 1::BIGINT AS size, 1::BIGINT AS n_files FROM source ORDER BY s, path""")
    file = str(tmp_path / "source.parquet")
    pq.write_table(con.execute("SELECT * FROM src").to_arrow_table(), file, row_group_size=10)
    bounds = an.group_bounds(file)
    groups = [g for g, b in enumerate(bounds) if b[0] <= "k" <= b[1]]
    assert (bounds[groups[0]][:2], bounds[groups[-1]][:2]) == (("a", "k"), ("k", "m"))
    assert an.key_paths("k", [bounds[g] for g in groups])[0] == ("", "d/g001/x0")
    assert an.key_paths("k", [bounds[g] for g in groups])[-1] == ("d/g198/x9", None)
    con.execute("CREATE TABLE near AS SELECT 'k' AS k, unnest(['d/g000', 'd/g199']) AS dir")
    con.execute(f"CREATE TABLE prows({an.AR_COLS})")
    tier = SimpleNamespace(shard_file=lambda mode, key: file)
    assert an.probe_prior_rows(con, [tier], "end") == 1
    assert con.execute("SELECT path FROM prows ORDER BY path").fetchall() == [
        *((f"d/g000/x{j}",) for j in range(5, 10)), *((f"d/g199/x{j}",) for j in range(7))]
