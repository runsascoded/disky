from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = spec_from_file_location("cw_inventory", Path(__file__).resolve().parents[2] / "job/cw-isolation-inventory.py")
inventory = module_from_spec(spec)
spec.loader.exec_module(inventory)


def test_cw_allowlist() -> None:
    assert inventory.prefixes("2026-10-10cw") == [
        "cw-l2/", "snapshots/cw/", "digest/cw/", "probes/cw/", "cw-sweep/", "sweep/cw/",
        "meta-l2/", "snapshots/meta/", "static-names/2026-10-10cw/",
    ]


@pytest.mark.parametrize("gen", ["", ".", "..", "other/generation"])
def test_generation_cannot_expand_scope(gen: str) -> None:
    with pytest.raises(ValueError, match="^static generation must be one nonempty path segment$"):
        inventory.prefixes(gen)


def test_records_pin_identity_and_checksums() -> None:
    blob = SimpleNamespace(name="cw-l2/scan/a.parquet", generation=42, size=123, storage_class="STANDARD", md5_hash="md5", crc32c="crc")
    record = inventory.object_record(blob, "source")
    assert record == {"bucket": "source", "key": "cw-l2/scan/a.parquet", "generation": "42", "bytes": 123, "storage_class": "STANDARD", "md5": "md5", "crc32c": "crc"}
    assert inventory.summarize([record], ["cw-l2/", "digest/cw/"]) == {
        "cw-l2/": {"objects": 1, "bytes": 123}, "digest/cw/": {"objects": 0, "bytes": 0},
    }


@pytest.mark.parametrize("changes", [{"generation": None}, {"size": None}, {"md5_hash": None, "crc32c": None}])
def test_incomplete_identity_is_refused(changes: dict) -> None:
    values = dict(name="key", generation=42, size=123, md5_hash="md5", crc32c="crc")
    values.update(changes)
    with pytest.raises(ValueError, match="^object metadata lacks generation/size/checksum: key$"):
        inventory.object_record(SimpleNamespace(**values), "source")


@pytest.mark.parametrize("roots", [["cw-l2/"], ["listing/", "listing/x/"]])
def test_outside_or_overlapping_scope_is_refused(roots: list[str]) -> None:
    with pytest.raises(ValueError, match="^object must match exactly one allowed prefix: listing/x/a$"):
        inventory.summarize([{"key": "listing/x/a", "bytes": 1}], roots)


@pytest.mark.parametrize("variant, filename", [
    ("path", "path-index"), ("bysize", "path-index-bysize"), ("user", "path-index-by-user"),
    ("coarse16", "path-index-coarse16"), ("coarse16-user", "path-index-coarse16-by-user"),
    ("bysize-user", "path-index-bysize-by-user"), ("age", "age-index"),
    ("age-pyramid-1y", "age-pyramid-1y"), ("meta:age-pyramid-1h", "age-pyramid-1h"),
    ("meta:path", "path-index"), ("over-time", "over-time"),
])
def test_exact_d1_file_references(variant: str, filename: str) -> None:
    assert inventory.index_key({"dir": "prefix/gen/", "variant": variant}) == f"prefix/gen/{filename}.parquet"


def test_unknown_variant_refuses() -> None:
    with pytest.raises(ValueError, match="^unsupported index variant: unknown$"):
        inventory.index_key({"dir": "prefix/gen", "variant": "unknown"})


def test_embedded_old_bucket_locations() -> None:
    assert inventory.old_references({"bucket": "source", "runs": [{"uri": "gs://source/key"}], "keep": "other"}, "source") == [
        {"field": "/bucket", "value": "source"}, {"field": "/runs/0/uri", "value": "gs://source/key"},
    ]
