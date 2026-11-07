"""Provider identities use one opaque optional field through listing and import."""
from pathlib import Path

import pytest

from disk_tree.find.bulk import entries_to_frame
from test_bulk_s3 import _LM, _stubbed_lister


def test_canonical_nullable_token_keeps_an_existing_native_generation() -> None:
    import duckdb
    from disk_tree.object_version import version_sql

    expression = version_sql({"version", "generation"})
    assert duckdb.connect().execute(f"SELECT {expression} FROM (SELECT NULL::VARCHAR AS version, 9007199254740993::BIGINT AS generation)").fetchall() == [("9007199254740993",)]


@pytest.mark.parametrize("paged", [False, True])
def test_s3_keeps_list_and_boundary_head_etags(paged: bool) -> None:
    lister, stubber = _stubbed_lister()
    stubber.add_response("head_object", {
        "ContentLength": 7, "LastModified": _LM, "ETag": '"boundary"',
    }, {"Bucket": "b1", "Key": "a"})
    stubber.add_response("list_objects_v2", {
        "Contents": [{"Key": "b", "Size": 9, "LastModified": _LM, "ETag": '"multipart-3"'}],
        "IsTruncated": False,
    }, {"Bucket": "b1", "StartAfter": "a", **({} if paged else {"Prefix": ""})})
    with stubber:
        if paged:
            rows = [r for page in lister.stream_pages("b1", None, "a", None) for r in page]
        else:
            rows = list(lister.stream_prefix("b1", "", "a", None))
        stubber.assert_no_pending_responses()
    assert [(r.name, getattr(r, "version", None)) for r in rows] == [
        ("a", '"boundary"'), ("b", '"multipart-3"'),
    ]


def test_generic_version_accepts_etags_and_exact_gcs_generations() -> None:
    frame = entries_to_frame("b", [
        ("legacy", 1, None, None),
        ("gcs", 2, None, None, 9007199254740993),
        ("s3", 3, None, None, None, None, '"opaque-17"'),
    ])
    assert frame[["name", "generation", "version"]].astype(object).where(frame.notna(), None).to_dict("records") == [
        {"name": "legacy", "generation": None, "version": None},
        {"name": "gcs", "generation": 9007199254740993, "version": "9007199254740993"},
        {"name": "s3", "generation": None, "version": '"opaque-17"'},
    ]


@pytest.mark.parametrize("engine", ["pandas", "duckdb", "stream"])
def test_import_engines_preserve_tokens_only_on_files(tmp_path: Path, engine: str) -> None:
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
    from disk_tree.find.aggregate_stream import aggregate_stream
    from disk_tree.find.import_listing import import_listing
    from disk_tree.listing import prepare_listing

    raw, out = tmp_path / "raw.parquet", tmp_path / "l2.parquet"
    pq.write_table(pa.Table.from_pandas(entries_to_frame("b", [
        ("d/a", 7, "2026-10-01T00:00:00Z", "STANDARD", None, None, '"a-3"'),
        ("d/b", 9, "2026-10-01T00:00:00Z", "STANDARD", 9007199254740993),
    ]), preserve_index=False), raw)
    listings = (str(raw),)
    con = duckdb.connect()
    if engine == "pandas":
        frame = import_listing(listings, "b", scheme="s3", con=con).df
        table = pa.Table.from_pandas(frame, preserve_index=False)
    else:
        if engine == "duckdb":
            aggregate_listing_to_parquet(prepare_listing(con, listings), "b", "s3", str(out), con=con)
        else:
            aggregate_stream(listings, "b", "s3", str(out), con=con)
        table = pq.read_table(out)
    assert [{k: row[k] for k in ("path", "kind", "version")} for row in table.to_pylist()] == [
        {"path": ".", "kind": "dir", "version": None},
        {"path": "d", "kind": "dir", "version": None},
        {"path": "d/a", "kind": "file", "version": '"a-3"'},
        {"path": "d/b", "kind": "file", "version": "9007199254740993"},
    ]


@pytest.mark.parametrize("temp_dir", [None, "spill"])
def test_stream_version_attach_takes_the_import_memory_limit_and_spills(tmp_path: Path, monkeypatch, temp_dir: str | None) -> None:
    """The stream import's version attach is a hash GROUP BY over every listed name: it runs under the import's `-M` and
    spills to `-T` (default: inside the parts dir, so a local-SSD staging disk takes it), never a fixed small cap (the
    02a import died at 512MB, 2026-10-22)."""
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    from disk_tree.find.aggregate_stream import aggregate_stream

    raw, out = tmp_path / "raw.parquet", tmp_path / "l2.parquet"
    pq.write_table(pa.Table.from_pandas(entries_to_frame("b", [
        ("d/a", 7, "2026-10-01T00:00:00Z", "STANDARD", None, None, '"a-3"'),
    ]), preserve_index=False), raw)
    configs = []
    connect = duckdb.connect

    def spy(*args, **kw):
        configs.append(kw.get("config"))
        return connect(*args, **kw)

    monkeypatch.setattr(duckdb, "connect", spy)
    spill = str(tmp_path / temp_dir) if temp_dir else None
    aggregate_stream((str(raw),), "b", "s3", str(out), memory_limit="3GB", temp_dir=spill)
    assert configs == [{"memory_limit": "3GB", "temp_directory": spill or f"{out}.parts/version-spill"}]
    assert pq.read_table(out, columns=["path", "version"]).to_pylist()[-1] == {"path": "d/a", "version": '"a-3"'}


def test_version_attach_streams_rows_beside_the_ordered_identities(tmp_path: Path, monkeypatch) -> None:
    """A layer-2 of 11 rows in 4-row groups, attached at 3-row groups (`BLOB_ROW_GROUP_SIZE`) on 4 threads: every row in
    its place with its columns and metadata, files carrying their listed identity (one unlisted: NULL), dirs none (a dir
    named in the listing included), the output cut into 3-row groups."""
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    import disk_tree.storage.base as base
    from disk_tree.object_version import attach_parquet

    monkeypatch.setattr(base, "BLOB_ROW_GROUP_SIZE", 3)
    l2, listing = tmp_path / "l2.parquet", tmp_path / "listing.parquet"
    paths = [(".", "dir"), ("d", "dir"), *((f"d/f{i:02d}", "file") for i in range(9))]
    table = pa.Table.from_pylist([{"path": p, "kind": k, "size": i} for i, (p, k) in enumerate(paths)]).replace_schema_metadata({b"fmt": b"v2"})
    pq.write_table(table, l2, row_group_size=4)
    pq.write_table(pa.Table.from_pylist([{"bucket": "b", "name": f"d/f{i:02d}", "version": f"v{i}"} for i in range(9) if i != 4]
                                        + [{"bucket": "b", "name": "d", "version": "x"}, {"bucket": "other", "name": "d/f04", "version": "no"}]), listing)
    con = duckdb.connect()
    con.execute("SET threads = 4")
    attach_parquet(str(l2), f"read_parquet('{listing}')", "b", con)
    f = pq.ParquetFile(l2)
    assert [f.metadata.row_group(g).num_rows for g in range(f.metadata.num_row_groups)] == [3, 3, 3, 2]
    assert f.schema_arrow.metadata == {b"fmt": b"v2"}
    assert [(r["path"], r["kind"], r["size"], r["version"]) for r in pq.read_table(l2).to_pylist()] == [
        (".", "dir", 0, None), ("d", "dir", 1, None),
        *((f"d/f{i:02d}", "file", i + 2, None if i == 4 else f"v{i}") for i in range(9)),
    ]
