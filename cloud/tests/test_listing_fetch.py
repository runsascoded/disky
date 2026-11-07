"""`dt-cloud fetch-listing`: an existing listing copied to a local dir, marker last, earlier shards cleared."""
import fsspec
import pytest

from dt_cloud.listing_fetch import fetch_listing


def test_copies_shards_then_the_marker_from_a_url(tmp_path):
    mem = fsspec.filesystem("memory")
    for name, body in (("shard-0.parquet", b"a"), ("shard-1.parquet", b"bb"), ("_SUCCESS.json", b"{}"), ("notes.txt", b"x")):
        mem.pipe(f"/cap/{name}", body)
    out = tmp_path / "listing"
    out.mkdir()
    (out / "shard-9.parquet").write_bytes(b"an earlier attempt's")
    assert fetch_listing("memory://cap", out) == ["shard-0.parquet", "shard-1.parquet"]
    assert sorted((p.name, p.read_bytes()) for p in out.iterdir()) == [
        ("_SUCCESS.json", b"{}"), ("shard-0.parquet", b"a"), ("shard-1.parquet", b"bb"),
    ]


def test_refuses_a_listing_without_its_marker(tmp_path):
    src = tmp_path / "cap"
    src.mkdir()
    (src / "shard-0.parquet").write_bytes(b"a")
    with pytest.raises(FileNotFoundError) as e:
        fetch_listing(str(src), tmp_path / "out")
    assert str(e.value) == f"{src}/_SUCCESS.json: no finished listing there"
    assert not (tmp_path / "out").exists()
