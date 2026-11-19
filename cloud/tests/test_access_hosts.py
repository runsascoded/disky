"""`dt-cloud access hosts`: a project's TPU nodes and Compute instances, one row per network endpoint, with both IPs."""

import json
from datetime import datetime, timezone

import pyarrow.parquet as pq

from dt_cloud.access_hosts import SCHEMA, gce_rows, tpu_rows, write

TS = datetime(2026, 11, 19, 1, 28, 59, tzinfo=timezone.utc)


def test_tpu_rows_one_per_endpoint():
    """A multi-host slice is one node with an endpoint per host; each host's requests come from its own external IP."""
    nodes = [{
        "name": "projects/p/locations/us-central2-b/nodes/marin-tpu-v4-8",
        "labels": {"iris-marin-scale-group": "tpu_v4-reserved_8-us-central2-b"},
        "createTime": "2026-11-17T09:31:00Z", "state": "READY",
        "networkEndpoints": [
            {"ipAddress": "10.130.0.6", "port": 8470, "accessConfig": {"externalIp": "35.186.21.86"}},
            {"ipAddress": "10.130.0.7", "port": 8470},
        ],
    }]
    assert [(r["zone"], r["kind"], r["name"], r["internal_ip"], r["external_ip"], r["state"], json.loads(r["labels"])) for r in tpu_rows("p", nodes, TS)] == [
        ("us-central2-b", "tpu", "marin-tpu-v4-8", "10.130.0.6", "35.186.21.86", "READY", {"iris-marin-scale-group": "tpu_v4-reserved_8-us-central2-b"}),
        ("us-central2-b", "tpu", "marin-tpu-v4-8", "10.130.0.7", None, "READY", {"iris-marin-scale-group": "tpu_v4-reserved_8-us-central2-b"}),
    ]


def test_gce_rows_take_each_interface_and_its_nat_ip():
    instances = [{
        "name": "marin-cpu-vm-1", "zone": "https://www.googleapis.com/compute/v1/projects/p/zones/europe-west4-b",
        "creationTimestamp": "2026-09-07T21:00:00.000-07:00", "status": "RUNNING",
        "networkInterfaces": [{"networkIP": "10.164.0.175", "accessConfigs": [{"natIP": "34.90.1.2"}]}],
    }, {
        "name": "internal-only", "zone": ".../zones/us-central1-a", "status": "RUNNING",
        "networkInterfaces": [{"networkIP": "10.128.0.9"}],
    }]
    assert [(r["zone"], r["kind"], r["name"], r["internal_ip"], r["external_ip"], r["labels"]) for r in gce_rows("p", instances, TS)] == [
        ("europe-west4-b", "gce", "marin-cpu-vm-1", "10.164.0.175", "34.90.1.2", "{}"),
        ("us-central1-a", "gce", "internal-only", "10.128.0.9", None, "{}"),
    ]


def test_write_files_the_snapshot_under_its_utc_hour(tmp_path):
    import pyarrow as pa

    table = pa.Table.from_pylist(tpu_rows("p", [{"name": "projects/p/locations/z/nodes/n", "networkEndpoints": [{"ipAddress": "10.0.0.1"}]}], TS), schema=SCHEMA)
    path = write(table, str(tmp_path / "hosts"))
    assert (path, pq.read_table(path).column("name").to_pylist()) == (str(tmp_path / "hosts/2026-11-19/01.parquet"), ["n"])
