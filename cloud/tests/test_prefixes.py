"""Tests for the shared prefix-map load + deepest-prefix-wins lookup."""

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from dt_cloud.identity import load_identities
from disk_tree.listing import prepare_listing
from dt_cloud.prefixes import deepest_lookup, load_prefix_map

IDENTITIES_YAML = """\
users:
  ryan-williams:
    aliases: [rw]
prefix_owners:
  - prefix: gs://b*/datasets/
    user: data-team
  - prefix: gs://b1/scratch/rw/
    user: ryan-williams
"""


@pytest.fixture
def identities(tmp_path: Path):
    path = tmp_path / "identities.yaml"
    path.write_text(IDENTITIES_YAML)
    return load_identities(path)


@pytest.fixture
def listing(tmp_path: Path) -> str:
    path = tmp_path / "listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1", "b2", "c9"],
            "name": ["x/a.bin", "y/b.bin", "z/c.bin"],
            "size_bytes": [1, 1, 1],
        }
    ).to_parquet(path)
    return str(path)


@pytest.fixture
def attribution(tmp_path: Path) -> str:
    path = tmp_path / "attribution.parquet"
    pd.DataFrame(
        {
            "prefix": ["gs://b1/users/rw/", "gs://b1/datasets/finelog/"],
            "user": ["rw", None],
            "source": ["user-prefix", "manual"],
            "asof": [dt.date(2026, 7, 20)] * 2,
        }
    ).to_parquet(path)
    return str(path)


def test_load_prefix_map(identities, listing: str, attribution: str):
    con = duckdb.connect()
    by_prefix = load_prefix_map(con, (attribution,), identities, prepare_listing(con, (listing,)))
    # parquet rows re-resolve raw users against current identities; wildcard
    # prefix_owners fan out over the listing's buckets (b* matches b1/b2, not c9)
    assert by_prefix == {
        "gs://b1/users/rw/": ("ryan-williams", "user-prefix"),
        "gs://b1/datasets/finelog/": (None, "manual"),
        "gs://b1/datasets/": ("data-team", "manual"),
        "gs://b2/datasets/": ("data-team", "manual"),
        "gs://b1/scratch/rw/": ("ryan-williams", "manual"),
    }


def test_deepest_lookup_wins_and_misses(identities, listing: str, attribution: str):
    con = duckdb.connect()
    deepest = deepest_lookup(load_prefix_map(con, (attribution,), identities, prepare_listing(con, (listing,))))
    # deepest ancestor wins over shallower manual rows (a deeper NULL-user row
    # — explicit nobody — beats the shallower owner)
    assert deepest("b1/datasets/finelog/part-0") == (None, "manual")
    assert deepest("b1/datasets/other") == ("data-team", "manual")
    assert deepest("b1/users/rw/ckpt/step-1") == ("ryan-williams", "user-prefix")
    assert deepest("b1/unrelated/dir") is None
    assert deepest("c9/datasets/x") is None


GLOB_IDENTITIES_YAML = """\
users:
  calvin-xu: {}
prefix_owners:
  - prefix: gs://b1/grug/swarm_*/
    user: calvin-xu
"""


def test_path_glob_expands_against_listing(tmp_path: Path):
    """A `*` in the path part fans out over the listing's actual dirs at that
    depth (one segment per glob star — `swarm_*` must not swallow `moe_*` or
    reach deeper levels)."""
    ident_path = tmp_path / "identities.yaml"
    ident_path.write_text(GLOB_IDENTITIES_YAML)
    identities = load_identities(ident_path)
    listing = tmp_path / "glob-listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1"] * 5 + ["b2"],
            "name": [
                "grug/swarm_fisher_000001-aa/ckpt.bin",
                "grug/swarm_fisher_000002-bb/opt/state.bin",
                "grug/moe_67b-cc/ckpt.bin",          # non-matching sibling
                "grug/swarm_deep/sub/x.bin",          # matches at level 2 only
                "other/swarm_fisher_000003-dd/x.bin", # wrong parent dir
                "grug/swarm_fisher_000004-ee/x.bin",  # wrong bucket
            ],
            "size_bytes": [1] * 6,
        }
    ).to_parquet(listing)
    con = duckdb.connect()
    by_prefix = load_prefix_map(con, (), identities, prepare_listing(con, (str(listing),)))
    assert by_prefix == {
        "gs://b1/grug/swarm_fisher_000001-aa/": ("calvin-xu", "manual"),
        "gs://b1/grug/swarm_fisher_000002-bb/": ("calvin-xu", "manual"),
        "gs://b1/grug/swarm_deep/": ("calvin-xu", "manual"),
    }
    deepest = deepest_lookup(by_prefix)
    assert deepest("b1/grug/swarm_fisher_000002-bb/opt") == ("calvin-xu", "manual")
    assert deepest("b1/grug/moe_67b-cc") is None


def test_write_labels_one_table_per_bucket_prefixes_relative(tmp_path: Path, identities, listing: str, attribution: str):
    """DT's `import --label` tables: `(prefix, usr)` per bucket — the bucket-
    wide glob rule lands as `''`, deeper rules lose the bucket, users resolve
    to canonical ids, and a bucket with no rule gets an empty table."""
    from dt_cloud.viz import write_labels

    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "labels"
    con = duckdb.connect()
    counts = write_labels(con, (listing,), (attribution,), identities_path, out)
    assert counts == {"b1": 4, "b2": 1, "c9": 0}
    assert sorted(p.name for p in out.iterdir()) == ["labels-b1.parquet", "labels-b2.parquet", "labels-c9.parquet"]
    read = lambda b: [dict(zip(["prefix", "usr"], r)) for r in con.execute(f"SELECT prefix, usr FROM read_parquet('{out / f'labels-{b}.parquet'}')").fetchall()]
    assert read("b1") == [
        {"prefix": "datasets", "usr": "data-team"},
        {"prefix": "datasets/finelog", "usr": None},
        {"prefix": "scratch/rw", "usr": "ryan-williams"},
        {"prefix": "users/rw", "usr": "ryan-williams"},
    ]
    assert read("b2") == [{"prefix": "datasets", "usr": "data-team"}]
    assert read("c9") == []
