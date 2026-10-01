"""`_d1_query` transient-error retry (the 2026-09-01 [team]-variant 401)."""

from __future__ import annotations

import io
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from dt_cloud import index_footer
from dt_cloud.index_footer import D1_RETRIES, _d1_query


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.cloudflare.com/...",
        code=code,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(b'{"success":false,"errors":[{"code":10000,"message":"Authentication error"}]}'),
    )


class _Resp:
    def read(self) -> bytes:
        return b'{"success": true, "result": []}'


def test_d1_query_retries_transient_401(monkeypatch):
    """Two spurious 401s then success: exactly 3 attempts, no exception."""
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) <= 2:
            raise _http_error(401)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _d1_query("SELECT 1", acct="acct", tok="tok", db_id="db")
    assert calls == [
        "https://api.cloudflare.com/client/v4/accounts/acct/d1/database/db/query",
    ] * 3


def test_d1_query_persistent_401_raises_after_all_retries(monkeypatch):
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[int] = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(401)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        _d1_query("SELECT 1", acct="acct", tok="tok", db_id="db")
    assert str(ei.value) == (
        'D1 query failed (401): {"success":false,"errors":'
        '[{"code":10000,"message":"Authentication error"}]}'
    )
    assert calls == [1] * (D1_RETRIES + 1)


def test_d1_query_non_retryable_status_fails_fast(monkeypatch):
    """A 400 (bad SQL / bad scope shape) is not transient — one attempt only."""
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[int] = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(400)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        _d1_query("SELECT 1", acct="acct", tok="tok", db_id="db")
    assert calls == [1]


# --- compact `rg_json` (2026-09-06) ------------------------------------------

import json

import pyarrow as pa
import pyarrow.parquet as pq

from dt_cloud.index_footer import _group_rows, _schema_json


def _write_index(path) -> "pq.FileMetaData":
    """A two-group path-index-shaped parquet (dictionary + plain columns)."""
    t = pa.table({
        "path": pa.array([f"marin-b/d{i}" for i in range(6)]),
        "depth": pa.array([2] * 6, pa.int64()),
        "usr": pa.array([None, "u1", "u1", None, "u2", "u2"]),
        "b": pa.array([10, 20, 30, 40, 50, 60], pa.int64()),
    })
    pq.write_table(t, path, row_group_size=4, compression="snappy", use_dictionary=["depth", "usr"])
    return pq.ParquetFile(path).metadata


def test_group_rows_compact_form(tmp_path):
    md = _write_index(tmp_path / "i.parquet")
    rows = _group_rows(md)
    assert [(r["rg"], r["row_start"], r["row_end"], r["d_min"], r["d_max"], r["p_min"], r["p_max"], r["b_min"], r["b_max"], r["u_min"], r["u_max"]) for r in rows] == [
        (0, 0, 4, 2, 2, "marin-b/d0", "marin-b/d3", 10, 40, "u1", "u1"),
        (1, 4, 6, 2, 2, "marin-b/d4", "marin-b/d5", 50, 60, "u2", "u2"),
    ]
    assert set(rows[0]) == {"rg", "d_min", "d_max", "p_min", "p_max", "b_max", "u_min", "u_max", "row_start", "row_end", "rg_json", "b_min"}
    for r in rows:
        rg = md.row_group(r["rg"])
        expected = [
            rg.num_rows,
            "SNAPPY",
            [[c.data_page_offset, c.total_compressed_size, c.dictionary_page_offset or 0] for c in (rg.column(i) for i in range(rg.num_columns))],
        ]
        assert json.loads(r["rg_json"]) == expected
        assert r["rg_json"] == json.dumps(expected, separators=(",", ":"))
    # Dictionary columns carry a dictionary page offset; plain ones store 0.
    cols = json.loads(rows[0]["rg_json"])[2]
    assert [bool(c[2]) for c in cols] == [False, True, True, False]
    assert [el["name"] for el in _schema_json(md)["schema"][1:]] == ["path", "depth", "usr", "b"]
    # A dir-only index (wire names, no `tier` metadata) is generation 1.
    assert _schema_json(md)["version"] == 1


def test_store_sort_is_version_2_and_sizes_resolve_to_size(tmp_path):
    """A sort the engine cut (`tier` in its metadata; `size`, `kind` columns)
    syncs as `index_schema.version` 2 — how a reader learns the generation has
    objects and a `bysize` sibling — with the group stats off `size`."""
    from dt_cloud.index_footer import INDEX_VERSION_STORE, extract

    t = pa.table({
        "path": ["b", "b/x.bin", "b/y"], "size": pa.array([30, 20, 10], pa.int64()),
        "depth": pa.array([1, 2, 2], pa.int64()), "kind": ["dir", "file", "dir"],
    })
    t = t.replace_schema_metadata({b"tier": b"path", b"sort": b"depth,path"})
    pq.write_table(t, tmp_path / "path-index.parquet", compression="snappy")
    schema, groups = extract(str(tmp_path / "path-index.parquet"))
    assert schema["version"] == INDEX_VERSION_STORE == 2
    assert [el["name"] for el in schema["schema"][1:]] == ["path", "size", "depth", "kind"]
    assert "floor_bytes" not in schema
    assert [(g["b_min"], g["b_max"], g["u_min"], g["u_max"]) for g in groups] == [(10, 30, None, None)]


def test_sync_d1_packs_inserts_greedily_under_the_byte_limit(tmp_path, monkeypatch):
    from dt_cloud.index_footer import sync_d1

    md = _write_index(tmp_path / "i.parquet")
    rows = [dict(r) for r in _group_rows(md) * 6]  # 12 group rows (copies; packing only cares about size)
    for i, r in enumerate(rows):
        r["rg"] = i
    monkeypatch.setattr(index_footer, "extract", lambda _p: ({"version": 1, "schema": []}, rows))
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    blobs: list[tuple] = []
    monkeypatch.setattr(index_footer, "write_groups_blob", lambda path, schema, rs: blobs.append(("json", path, schema, rs)) or (path, 0))
    monkeypatch.setattr(index_footer, "write_groups_parquet", lambda path, schema, rs: blobs.append(("parquet", path, schema, rs)) or (path, 0))
    sent: list[str] = []
    monkeypatch.setattr(index_footer, "_d1_query", lambda sql, acct, tok, db_id: sent.append(sql) or [])
    limit = 1000
    n = sync_d1("2026-09-01", "x.parquet", variant="path", gen="20260901T070000Z", key="listing/2026-09-01/index/20260901T070000Z", insert_bytes=limit)
    assert n == 12
    # The cold footer + blob beside the parquet are written before any D1 row: the durable copies.
    assert blobs == [("parquet", "x.parquet", {"version": 1, "schema": []}, rows), ("json", "x.parquet", {"version": 1, "schema": []}, rows)]
    # Generation protocol: sweep unreachable gens, land every group under this
    # gen, then flip the pointer — nothing is deleted before the flip.
    assert sent[0] == (
        "DELETE FROM index_row_groups WHERE date='2026-09-01' AND variant='path' AND gen <> '20260901T070000Z' "
        "AND gen <> COALESCE((SELECT gen FROM index_schema WHERE date='2026-09-01' AND variant='path'), '');"
    )
    assert sent[-1] == (
        "INSERT OR REPLACE INTO index_schema (date, variant, version, schema_json, floor_bytes, gen, dir) VALUES "
        "('2026-09-01', 'path', 1, '[]', NULL, '20260901T070000Z', 'listing/2026-09-01/index/20260901T070000Z');"
    )
    inserts = sent[1:-1]
    head = "INSERT OR REPLACE INTO index_row_groups (date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json) VALUES "
    tuples = [stmt[len(head):-1].split("),(") for stmt in inserts]
    assert all(stmt.startswith(head) and stmt.endswith(";") and len(stmt) <= limit for stmt in inserts)
    assert [len(t) for t in tuples] == [5, 5, 2]  # 12 rows, five ~165-byte tuples per 1000-byte statement
    # Greedy: no statement could have taken the next one's first tuple.
    for stmt, nxt in zip(inserts, inserts[1:]):
        first = nxt[len(head):].split("),(")[0] + ")"
        assert len(stmt) + 1 + len(first) > limit
    assert [t.split(", ")[3] for stmt in tuples for t in stmt] == [str(i) for i in range(12)]


def test_gc_d1_deletes_only_generations_no_pointer_names(monkeypatch):
    """The gc statement against a real SQLite: rows of the pointer's gen stay,
    every other gen of that date goes, other dates untouched."""
    import re
    import sqlite3

    from dt_cloud.index_footer import gc_d1

    con = sqlite3.connect(":memory:")
    # The schema from the cw lineage, whichever shape it has: one squashed
    # `0001_init.sql` (creates every table itself), or the migration that
    # introduced `index_row_groups` on top of an earlier `index_schema`.
    mig = Path(__file__).parents[2] / "site/migrations/cw"
    (ddl_path,) = [f for f in sorted(mig.glob("*.sql")) if "CREATE TABLE index_row_groups" in f.read_text()]
    ddl = ddl_path.read_text()
    if "CREATE TABLE index_schema" not in ddl:
        con.executescript("CREATE TABLE index_schema (date TEXT, variant TEXT, version INTEGER, schema_json TEXT, floor_bytes INTEGER, PRIMARY KEY (date, variant));")
    con.executescript(ddl)
    row = "(?, ?, ?, ?, 1, 1, 'a', 'b', 1, NULL, NULL, 0, 1, '[]')"
    con.executemany(f"INSERT INTO index_row_groups VALUES {row}", [
        ("2026-09-01", "path", "g1", 0), ("2026-09-01", "path", "g1", 1),
        ("2026-09-01", "path", "g2", 0),  # the pointer's gen
        ("2026-09-01", "user", "g1", 0),  # user variant still points at g1
        ("2026-09-01", "user", "orphan", 0),
        ("2026-09-02", "path", "g1", 0),  # another date: a dangling gen, but not asked
    ])
    con.executemany("INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES (?, ?, 1, '[]', ?, ?)", [
        ("2026-09-01", "path", "g2", "listing/2026-09-01/index/g2"),
        ("2026-09-01", "user", "g1", "listing/2026-09-01"),
    ])
    sent: list[str] = []

    def fake_query(sql, acct, tok, db_id):
        sent.append(sql)
        cur = con.execute(sql)
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

    monkeypatch.setattr(index_footer, "_d1_query", fake_query)
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    assert gc_d1("2026-09-01") == 3
    assert re.sub(r"\s+", " ", sent[0]) == (
        "DELETE FROM index_row_groups WHERE date = '2026-09-01' AND gen <> COALESCE("
        "(SELECT s.gen FROM index_schema s WHERE s.date = index_row_groups.date AND s.variant = index_row_groups.variant), '') RETURNING 1 AS n;"
    )
    assert con.execute("SELECT date, variant, gen, rg FROM index_row_groups ORDER BY 1, 2, 3, 4").fetchall() == [
        ("2026-09-01", "path", "g2", 0),
        ("2026-09-01", "user", "g1", 0),
        ("2026-09-02", "path", "g1", 0),
    ]


def test_retire_d1_drops_sort_groups_of_scans_past_the_retention_window(monkeypatch):
    """Newest `retain` scans keep everything; older scans lose only the store
    sorts' row groups (`SORT_VARIANTS`: path, bysize and the by-user copies —
    a pre-store scan simply has no bysize rows to drop); the age pyramid stays;
    pointers are untouched."""
    import sqlite3

    from dt_cloud.index_footer import SORT_VARIANTS, retire_d1

    assert SORT_VARIANTS == ("path", "bysize", "user", "bysize-user")
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE index_schema (date TEXT, variant TEXT, version INTEGER, schema_json TEXT, floor_bytes INTEGER, gen TEXT, dir TEXT, PRIMARY KEY (date, variant));")
    con.executescript("""
      CREATE TABLE index_row_groups (date TEXT, variant TEXT, gen TEXT, rg INTEGER, PRIMARY KEY (date, variant, gen, rg));
    """)
    for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
        for v in ("path", "user", "age-pyramid-1d") + (("bysize",) if d != "2026-09-01" else ()):
            con.execute("INSERT INTO index_schema VALUES (?, ?, 1, '[]', NULL, 'legacy', ?)", (d, v, f"listing/{d}"))
            con.executemany("INSERT INTO index_row_groups VALUES (?, ?, 'legacy', ?)", [(d, v, i) for i in range(2)])

    def fake_query(sql, acct, tok, db_id):
        cur = con.execute(sql)
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()] if cur.description else []

    monkeypatch.setattr(index_footer, "_d1_query", fake_query)
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    checked: list[str] = []
    every = lambda p: checked.append(p) or True  # noqa: E731 — every cold footer exists
    assert retire_d1(2, has_cold=every) == ([("2026-09-01", "path", 2), ("2026-09-01", "user", 2)], [])
    assert checked == [
        "oa-gcs-usage-dvx/listing/2026-09-01/path-index.groups.parquet",
        "oa-gcs-usage-dvx/listing/2026-09-01/path-index-by-user.groups.parquet",
    ]
    assert con.execute("SELECT date, variant, count(*) FROM index_row_groups GROUP BY 1, 2 ORDER BY 1, 2").fetchall() == [
        ("2026-09-01", "age-pyramid-1d", 2),
        ("2026-09-02", "age-pyramid-1d", 2), ("2026-09-02", "bysize", 2), ("2026-09-02", "path", 2), ("2026-09-02", "user", 2),
        ("2026-09-03", "age-pyramid-1d", 2), ("2026-09-03", "bysize", 2), ("2026-09-03", "path", 2), ("2026-09-03", "user", 2),
    ]
    assert con.execute("SELECT count(*) FROM index_schema").fetchone() == (11,)
    checked.clear()
    assert retire_d1(2, has_cold=every) == ([], [])  # idempotent
    assert checked == []  # nothing left in D1 to check
    # No cold footer for the `bysize` sort: it stays in D1, the rest go.
    cold = lambda p: "bysize" not in p  # noqa: E731
    assert retire_d1(1, base="r2://bk", has_cold=cold) == (
        [("2026-09-02", "path", 2), ("2026-09-02", "user", 2)],
        [("2026-09-02", "bysize", "r2://bk/listing/2026-09-02/path-index-bysize.groups.parquet")],
    )
    assert con.execute("SELECT variant, count(*) FROM index_row_groups WHERE date = '2026-09-02' GROUP BY 1 ORDER BY 1").fetchall() == [
        ("age-pyramid-1d", 2), ("bysize", 2),
    ]


def test_retire_d1_refuses_without_a_cold_footer_on_disk(tmp_path, monkeypatch):
    """The default check looks for the real file under `base`: none → kept
    (and reported), written → retired."""
    import sqlite3

    from dt_cloud.index_footer import retire_d1, write_groups_parquet

    con = sqlite3.connect(":memory:")
    con.executescript("""
      CREATE TABLE index_schema (date TEXT, variant TEXT, version INTEGER, schema_json TEXT, floor_bytes INTEGER, gen TEXT, dir TEXT, PRIMARY KEY (date, variant));
      CREATE TABLE index_row_groups (date TEXT, variant TEXT, gen TEXT, rg INTEGER, PRIMARY KEY (date, variant, gen, rg));
    """)
    for d in ("2026-09-01", "2026-09-02"):
        con.execute("INSERT INTO index_schema VALUES (?, 'path', 2, '[]', NULL, 'g', ?)", (d, f"listing/{d}/index/g"))
        con.executemany("INSERT INTO index_row_groups VALUES (?, 'path', 'g', ?)", [(d, i) for i in range(3)])

    def fake_query(sql, acct, tok, db_id):
        cur = con.execute(sql)
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()] if cur.description else []

    monkeypatch.setattr(index_footer, "_d1_query", fake_query)
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    gdir = tmp_path / "listing/2026-09-01/index/g"
    want = str(gdir / "path-index.groups.parquet")
    assert retire_d1(1, base=str(tmp_path)) == ([], [("2026-09-01", "path", want)])
    assert con.execute("SELECT count(*) FROM index_row_groups").fetchone() == (6,)
    gdir.mkdir(parents=True)
    md = _write_index(tmp_path / "i.parquet")
    write_groups_parquet(str(gdir / "path-index.parquet"), _schema_json(md), _group_rows(md))
    assert retire_d1(1, base=str(tmp_path)) == ([("2026-09-01", "path", 3)], [])
    assert con.execute("SELECT date, count(*) FROM index_row_groups GROUP BY 1").fetchall() == [("2026-09-02", 3)]


def test_groups_blob_is_the_synced_rows_as_one_document(tmp_path):
    from dt_cloud.index_footer import groups_blob_path, write_groups_blob

    md = _write_index(tmp_path / "i.parquet")
    schema = {**_schema_json(md), "floor_bytes": 4096}
    rows = _group_rows(md)
    out, n = write_groups_blob(str(tmp_path / "i.parquet"), schema, rows)
    assert out == str(tmp_path / "i.groups.json") == groups_blob_path(str(tmp_path / "i.parquet"))
    text = (tmp_path / "i.groups.json").read_text()
    assert n == len(text)
    assert json.loads(text) == {
        "v": 1,
        "version": schema["version"],
        "schema": schema["schema"],
        "floor_bytes": 4096,
        "groups": [
            [r["rg"], r["d_min"], r["d_max"], r["p_min"], r["p_max"], r["b_max"], r["u_min"], r["u_max"], r["row_start"], r["row_end"], r["rg_json"], r["b_min"]]
            for r in rows
        ],
    }
    assert [(g[0], g[11]) for g in json.loads(text)["groups"]] == [(0, 10), (1, 50)]  # `b_min` appended 12th


def test_groups_parquet_is_the_synced_rows_typed_in_small_stat_groups(tmp_path):
    """The cold footer tier holds exactly the rows `sync_d1` sends D1 (and the
    blob holds, `b_min` included), in rg order, in `row_group_rows`-row
    groups with min/max stats on every pruning column — none on `rg_json`."""
    from disk_tree.find.groups import FOOTER_COLS, FOOTER_STAT_COLS, read_groups_parquet

    from dt_cloud.index_footer import groups_parquet_path, write_groups_parquet

    t = pa.table({
        "path": pa.array([f"b/d{i:02d}" for i in range(10)]),
        "depth": pa.array([2] * 10, pa.int64()),
        "usr": pa.array(["u1"] * 4 + [None] * 2 + ["u2"] * 4),
        "size": pa.array(range(100, 110), pa.int64()),
    })
    pq.write_table(t.replace_schema_metadata({b"tier": b"path"}), tmp_path / "path-index.parquet", row_group_size=2, compression="zstd")
    schema, rows = index_footer.extract(str(tmp_path / "path-index.parquet"))
    out, n = write_groups_parquet(str(tmp_path / "path-index.parquet"), schema, rows, row_group_rows=2)
    assert out == groups_parquet_path(str(tmp_path / "path-index.parquet")) == str(tmp_path / "path-index.groups.parquet")
    assert n == (tmp_path / "path-index.groups.parquet").stat().st_size
    back_schema, back = read_groups_parquet(out)
    assert back_schema == schema
    assert back == [{c: r[c] for c in FOOTER_COLS} for r in rows]
    assert [(r["rg"], r["u_min"], r["u_max"], r["b_min"], r["b_max"], r["row_start"], r["row_end"]) for r in back] == [
        (0, "u1", "u1", 100, 101, 0, 2), (1, "u1", "u1", 102, 103, 2, 4), (2, None, None, 104, 105, 4, 6),
        (3, "u2", "u2", 106, 107, 6, 8), (4, "u2", "u2", 108, 109, 8, 10),
    ]
    md = pq.read_metadata(out)
    assert [md.row_group(g).num_rows for g in range(md.num_row_groups)] == [2, 2, 1]
    assert md.schema.names == list(FOOTER_COLS)
    assert (md.metadata[b"groups_v"], md.metadata[b"version"], b"ARROW:schema" in md.metadata) == (b"1", b"2", False)

    def stats(g: int) -> dict:
        rg = md.row_group(g)
        got = {}
        for c in range(rg.num_columns):
            cc = rg.column(c)
            assert cc.compression == "ZSTD"
            s = cc.statistics
            got[cc.path_in_schema] = (s.min, s.max) if s is not None and s.has_min_max else None
        return got

    # Footer group 1 = tier groups 2 (usr all NULL) and 3 (u2): the NULLs drop out of the u stats.
    assert stats(1) == {
        "rg": None, "d_min": (2, 2), "d_max": (2, 2), "p_min": ("b/d04", "b/d06"), "p_max": ("b/d05", "b/d07"),
        "b_min": (104, 106), "b_max": (105, 107), "u_min": ("u2", "u2"), "u_max": ("u2", "u2"),
        "row_start": None, "row_end": None, "rg_json": None,
    }
    assert {c for c, v in stats(0).items() if v is not None} == set(FOOTER_STAT_COLS)


def test_index_blob_backfills_the_cold_footer_from_json(tmp_path, monkeypatch):
    """`index-blob -J`: the `.groups.parquet` from the `.groups.json` already
    beside a tier, never reading the tier's footer — byte-identical to the
    one written from the footer."""
    from click.testing import CliRunner
    from disk_tree.find.groups import read_groups_parquet

    from dt_cloud import cli as C
    from dt_cloud.cli import main
    from dt_cloud.index_footer import write_groups_blob, write_groups_parquet

    logged: list[str] = []  # `err` binds stderr at import: record it instead
    monkeypatch.setattr(C, "err", lambda *a: logged.append(" ".join(map(str, a))))

    d = tmp_path / "g"
    d.mkdir()
    md = _write_index(d / "path-index.parquet")
    schema, rows = _schema_json(md), _group_rows(md)
    write_groups_blob(str(d / "path-index.parquet"), schema, rows)
    ref, _ = write_groups_parquet(str(tmp_path / "ref.parquet"), schema, rows)
    # The tier's own footer must not be read: make it unreadable.
    (d / "path-index.parquet").write_bytes(b"not a parquet")
    res = CliRunner().invoke(main, ["index-blob", "-J", "-v", "path", "-v", "bysize", "-d", str(d), "2026-09-01"])
    assert res.exit_code == 0, res.output
    size = (d / "path-index.groups.parquet").stat().st_size
    assert logged == [
        f"index-blob: 2026-09-01 [path] 2 groups → {d}/path-index.groups.parquet ({size:,} B)",
        f"index-blob: 2026-09-01 [bysize] no tier at {d}/path-index-bysize.parquet; skipped",
    ]
    assert (d / "path-index.groups.parquet").read_bytes() == (tmp_path / "ref.groups.parquet").read_bytes()
    assert ref == str(tmp_path / "ref.groups.parquet")
    assert read_groups_parquet(str(d / "path-index.groups.parquet")) == (schema, rows)


def test_d1_query_refuses_without_a_database(monkeypatch):
    """No default D1: a deployment that forgot `$D1_DB_ID` fails instead of
    writing into another deployment's database (it once defaulted to gcs prod)."""
    calls: list[object] = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a) or None)
    with pytest.raises(RuntimeError, match=r"^no D1 database: set \$D1_DB_ID"):
        _d1_query("SELECT 1", acct="acct", tok="tok", db_id="")
    assert calls == []
