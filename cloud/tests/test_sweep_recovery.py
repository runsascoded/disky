from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dt_cloud.sweep_recovery import recovery_sample


def test_bounded_read_only_soft_deleted_sample():
    calls = []
    def listing(bucket, **kwargs):
        calls.append((bucket, kwargs))
        return [SimpleNamespace(name="a/key", generation=42, size=5, soft_delete_time=datetime(2026, 10, 5, tzinfo=timezone.utc), hard_delete_time=datetime(2026, 10, 12, tzinfo=timezone.utc))]
    result = recovery_sample("gs://b/a/", client=SimpleNamespace(list_blobs=listing))
    assert calls == [("b", {"prefix": "a/", "soft_deleted": True, "max_results": 1})]
    assert result == {"prefix": "gs://b/a/", "limit": 1, "samples": [{"name": "a/key", "generation": 42, "size_bytes": 5, "soft_deleted": "2026-10-05T00:00:00+00:00", "hard_delete": "2026-10-12T00:00:00+00:00"}], "complete_inventory": False}


@pytest.mark.parametrize("prefix,limit", [("gs://b/", 1), ("gs://b/a", 1), ("s3://b/a/", 1), ("gs://b/a/", 0), ("gs://b/a/", 101)])
def test_invalid_requests_never_reach_storage(prefix, limit):
    with pytest.raises(ValueError):
        recovery_sample(prefix, limit, client=object())
