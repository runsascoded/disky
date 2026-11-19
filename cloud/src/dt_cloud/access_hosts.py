"""The VMs behind a deployment's bucket requests: a snapshot of each configured project's TPU nodes and Compute
instances, one row per network endpoint, with its internal and external IPs. A usage-log requester is a VM's external
IP, which only the live APIs report (Cloud Asset Inventory's history keeps TPU nodes but not their external IPs), so
the snapshot has to be taken while the VM lives: hourly, beside the access ingest (`specs/access-job-attribution.md`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pyarrow as pa

SCHEMA = pa.schema([
    ("ts", pa.timestamp("s", tz="UTC")),
    ("project", pa.string()),
    ("zone", pa.string()),
    ("kind", pa.string()),
    ("name", pa.string()),
    ("labels", pa.string()),
    ("internal_ip", pa.string()),
    ("external_ip", pa.string()),
    ("created", pa.string()),
    ("state", pa.string()),
])


def tpu_rows(project: str, nodes: list[dict], ts: datetime) -> list[dict]:
    """A TPU node per network endpoint (one per host of a multi-host slice)."""
    rows = []
    for n in nodes:
        _, _, _, zone, _, name = n["name"].split("/")
        for e in n.get("networkEndpoints") or [{}]:
            rows.append({
                "ts": ts, "project": project, "zone": zone, "kind": "tpu", "name": name,
                "labels": json.dumps(n.get("labels") or {}, sort_keys=True),
                "internal_ip": e.get("ipAddress"), "external_ip": (e.get("accessConfig") or {}).get("externalIp"),
                "created": n.get("createTime"), "state": n.get("state"),
            })
    return rows


def gce_rows(project: str, instances: list[dict], ts: datetime) -> list[dict]:
    """A Compute instance per network interface (its first access config's NAT IP is the external one)."""
    rows = []
    for i in instances:
        zone = i["zone"].rsplit("/", 1)[-1]
        for nic in i.get("networkInterfaces") or [{}]:
            ac = (nic.get("accessConfigs") or [{}])[0]
            rows.append({
                "ts": ts, "project": project, "zone": zone, "kind": "gce", "name": i["name"],
                "labels": json.dumps(i.get("labels") or {}, sort_keys=True),
                "internal_ip": nic.get("networkIP"), "external_ip": ac.get("natIP"),
                "created": i.get("creationTimestamp"), "state": i.get("status"),
            })
    return rows


def snapshot(projects: list[str], ts: datetime | None = None) -> pa.Table:
    """Every TPU node and Compute instance in `projects` now, as `SCHEMA` rows."""
    from .gcp import _pages, pages

    ts = (ts or datetime.now(timezone.utc)).replace(microsecond=0)
    rows: list[dict] = []
    for p in projects:
        rows += tpu_rows(p, _pages(f"https://tpu.googleapis.com/v2/projects/{p}/locations/-/nodes", "nodes"), ts)
        agg = pages(f"https://compute.googleapis.com/compute/v1/projects/{p}/aggregated/instances", maxResults="500")
        rows += gce_rows(p, [i for page in agg for scoped in page.get("items", {}).values() for i in scoped.get("instances", [])], ts)
    return pa.Table.from_pylist(rows, schema=SCHEMA)


def write(table: pa.Table, out_root: str) -> str:
    """`table` at `<out_root>/<YYYY-MM-DD>/<HH>.parquet` (its snapshot hour, UTC; a later snapshot that hour replaces
    it); returns the path."""
    import pyarrow.parquet as pq

    ts = table.column("ts")[0].as_py() if table.num_rows else datetime.now(timezone.utc)
    path = f"{out_root.rstrip('/')}/{ts:%Y-%m-%d}/{ts:%H}.parquet"
    if path.startswith("gs://"):
        import gcsfs

        with gcsfs.GCSFileSystem().open(path, "wb") as f:
            pq.write_table(table, f, compression="zstd")
    else:
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path, compression="zstd")
    return path
