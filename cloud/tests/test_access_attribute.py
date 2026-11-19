"""`dt-cloud access attribute`: the hourly rollup ⋈ host snapshots ⋈ job tables, one day at a time."""

import datetime as dt

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from dt_cloud.access_attribute import attribute_day
from dt_cloud.access_hosts import SCHEMA

DAY = dt.date(2026, 11, 19)
UTC = dt.timezone.utc


def h(hour):
    return dt.datetime(2026, 11, 19, hour, tzinfo=UTC)


def write(path, rows, schema=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def fixture(root):
    req = [
        # 10:00: the VM at 1.1.1.1 ran two tasks (3:1 samples), the one at 2.2.2.2 none, 9.9.9.9 is no VM's
        {"hour": h(10), "requester": "1.1.1.1", "n": 400}, {"hour": h(10), "requester": "2.2.2.2", "n": 50},
        {"hour": h(10), "requester": "9.9.9.9", "n": 7},
        # 11:00: 3.3.3.3 is held by a VM created at 12:30 (an ephemeral IP reused): not that VM's
        {"hour": h(11), "requester": "3.3.3.3", "n": 20},
    ]
    write(root / f"requesters-hourly/{DAY}.parquet", [
        {"day": DAY, "bucket": "b", "op": "GET", "op_raw": "GET_Object", "client": "tensorstore", "user_agent": "tensorstore",
         "prefix": "datakit/store", "bytes_out": 10.0 * r["n"], "bytes_in": 0.0, **r} for r in req])
    snap = [
        {"ts": h(10) + dt.timedelta(minutes=5), "kind": "tpu", "name": "vm-a", "internal_ip": "10.0.0.1", "external_ip": "1.1.1.1", "created": "2026-11-18T00:00:00Z"},
        {"ts": h(10) + dt.timedelta(minutes=5), "kind": "gce", "name": "vm-b", "internal_ip": "10.0.0.2", "external_ip": "2.2.2.2", "created": "2026-11-18T00:00:00Z"},
        {"ts": h(13) + dt.timedelta(minutes=5), "kind": "tpu", "name": "vm-c", "internal_ip": "10.0.0.3", "external_ip": "3.3.3.3", "created": "2026-11-19T12:30:00Z"},
    ]
    write(root / f"hosts/{DAY}/10.parquet", [{**s, "project": "p"} for s in snap], SCHEMA)
    naive = {k: h(k).replace(tzinfo=None) for k in (10, 11)}
    write(root / f"jobs/{DAY}/workers.parquet", [
        {"hour": naive[10], "worker_id": "w-a", "address": "10.0.0.1:10001"},
        {"hour": naive[10], "worker_id": "w-b", "address": "10.0.0.2:10001"},
        {"hour": naive[11], "worker_id": "w-c", "address": "10.0.0.3:10001"},
    ])
    write(root / f"jobs/{DAY}/tasks.parquet", [
        {"hour": naive[10], "task_id": "/ann/train/0", "owner": "ann", "job": "/ann/train", "worker_id": "w-a", "n_samples": 30},
        {"hour": naive[10], "task_id": "/bo/eval/0", "owner": "bo", "job": "/bo/eval", "worker_id": "w-a", "n_samples": 10},
        {"hour": naive[11], "task_id": "/cy/x/0", "owner": "cy", "job": "/cy/x", "worker_id": "w-c", "n_samples": 5},
    ])


def test_attribute_day_splits_by_samples_and_keeps_the_unmatched(tmp_path):
    fixture(tmp_path)
    out = attribute_day(str(tmp_path), DAY)
    assert out == {"day": str(DAY), "status": "written", "rows": 5, "share": {"ip": 0.0566, "task": 0.8386, "vm": 0.1048}}
    rows = duckdb.sql(f"""SELECT hour(timezone('UTC', hour)), requester, vm, owner, job, via, n, bytes_out
        FROM '{tmp_path}/attributed/{DAY}.parquet' ORDER BY hour, requester, owner""").fetchall()
    assert rows == [
        (10, "1.1.1.1", "vm-a", "ann", "/ann/train", "task", 300.0, 3000.0),
        (10, "1.1.1.1", "vm-a", "bo", "/bo/eval", "task", 100.0, 1000.0),
        (10, "2.2.2.2", "vm-b", None, None, "vm", 50.0, 500.0),
        (10, "9.9.9.9", None, None, None, "ip", 7.0, 70.0),
        (11, "3.3.3.3", None, None, None, "ip", 20.0, 200.0),
    ]


def test_attribute_day_redoes_a_day_only_when_its_inputs_change(tmp_path):
    fixture(tmp_path)
    assert attribute_day(str(tmp_path), DAY, skip_unchanged=True)["status"] == "written"
    assert attribute_day(str(tmp_path), DAY, skip_unchanged=True) == {"day": str(DAY), "status": "unchanged"}
    (tmp_path / f"jobs/{DAY}/tasks.parquet").unlink()  # jobs not yet written: requests stop at their VM
    assert attribute_day(str(tmp_path), DAY, skip_unchanged=True)["share"] == {"ip": 0.0566, "vm": 0.9434}
    assert attribute_day(str(tmp_path), dt.date(2026, 11, 20)) == {"day": "2026-11-20", "status": "no-rollup"}
