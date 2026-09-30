import pytest

from dt_cloud.stage import split_glob


def test_split_glob_sii_pattern():
    assert split_glob("/gcs/marin-us-east1/inventory-reports/*_2026-07-29T*_*.parquet") == (
        "marin-us-east1",
        "inventory-reports/",
        "inventory-reports/*_2026-07-29T*_*.parquet",
    )


def test_split_glob_listing_shards():
    assert split_glob("/gcs/oa-gcs-usage-dvx/central2-listing/2026-07-29/*.parquet") == (
        "oa-gcs-usage-dvx",
        "central2-listing/2026-07-29/",
        "central2-listing/2026-07-29/*.parquet",
    )


def test_split_glob_literal_path_prefix_is_whole_name():
    assert split_glob("/gcs/oa-gcs-usage-dvx/attr/attribution-wandb.parquet") == (
        "oa-gcs-usage-dvx",
        "attr/attribution-wandb.parquet",
        "attr/attribution-wandb.parquet",
    )


@pytest.mark.parametrize(
    "bad",
    [
        "gs://bucket/x.parquet",
        "/gcs/bucket-only",
        "/gcs/bucket-only/",
    ],
)
def test_split_glob_rejects_non_fuse_paths(bad):
    with pytest.raises(ValueError):
        split_glob(bad)
