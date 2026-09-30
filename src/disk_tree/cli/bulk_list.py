"""`disk-tree bulk-list` — sharded live listing of a cloud bucket to canonical
listing parquet (layer-1).

Feed the result into ``disk-tree import`` to get canonical per-path scans
(layer-2). Split so each stage stays independently retriable and cacheable:

    # 1. list bucket → shards under out_dir/*.parquet (+ _SUCCESS.json)
    disk-tree bulk-list gcs://marin-us-central1 -o gs://oa-dvx/listing/2026-08-05/marin-us-central1

    # 2. aggregate into a canonical scan
    disk-tree import -e duckdb -l 'gs://oa-dvx/listing/2026-08-05/marin-us-central1/*.parquet' -b marin-us-central1
"""

from __future__ import annotations

import os

from click import Choice, argument, option
from utz import err

from disk_tree.cli.base import cli


@cli.command('bulk-list')
@option('-a', '--adaptive', is_flag=True, help='Adaptive range-splitting: workers bisect their remaining key range whenever peers are idle — no weights or fanout discovery needed (spec: adaptive-listing.md)')
@option('-E', '--endpoint-url', default=None, help='S3-compatible endpoint URL (required for r2://; also usable for MinIO / non-AWS S3)')
@option('-f', '--profile', default=None, help='AWS credential profile to authenticate with (for a cross-account source/target); default: ambient credentials')
@option('-o', '--out', 'out_dir', required=True, help='Output dir for listing shards (local path or fsspec URL such as `gs://...`)')
@option('-p', '--prefix', default=None, help='Restrict listing to this bucket-relative prefix')
@option('-P', '--procs', default=None, type=int, help='Worker processes (default: os.cpu_count()). LIST throughput is GIL-bound, not endpoint-bound: a single process saturates at ~8K keys/s no matter how many threads it runs, while processes scale ~linearly (measured on CoreWeave: 1/2/4/8/16 procs -> 8.3K/17.1K/34.5K/68.1K/89.8K keys/s). Scale this with cores, not -w.')
@option('-r', '--region', default=None, help='AWS region name (S3 backend only)')
@option('-w', '--threads', default=3, help='Threads per worker (default 3). Only enough to hide per-request latency (~0.1s) while the GIL is released on network I/O; beyond that threads contend for the GIL with parquet/pandas work on the same process and add nothing.')
@option('-W', '--weights-from', default=None, help='Glob to a prior listing parquet used to bin-pack + range-split hot prefixes (planned mode only)')
@option('-x', '--exists', type=Choice(['error', 'clear', 'reuse']), default='error', help='Behavior when --out already has shards: error/clear/reuse')
@option('--warm-from', default=None, help="Prior adaptive listing dir whose _SUCCESS.json range boundaries seed this run (adaptive mode only)")
@argument('uri')
def bulk_list_cmd(
    adaptive: bool,
    endpoint_url: str | None,
    profile: str | None,
    out_dir: str,
    prefix: str | None,
    procs: int | None,
    region: str | None,
    threads: int,
    weights_from: str | None,
    exists: str,
    warm_from: str | None,
    uri: str,
):
    """Bulk-list `URI` (e.g. `gcs://bucket`) to sharded listing parquet at `--out`."""
    # Default to one process per core: listing is GIL-bound per interpreter, so
    # cores — not the endpoint — set the ceiling (see -P's help for the numbers).
    procs = procs or os.cpu_count() or 6
    if adaptive:
        if weights_from:
            raise ValueError("--weights-from is the planned-mode input; adaptive mode warm-starts via --warm-from")
        total = bulk_list_adaptive_uri(
            uri, out_dir=out_dir,
            prefix=prefix, procs=procs, threads=threads,
            exists=exists, warm_from=warm_from,
            endpoint_url=endpoint_url, region=region, profile=profile,
        )
    else:
        if warm_from:
            raise ValueError("--warm-from requires --adaptive")
        total = bulk_list_uri(
            uri, out_dir=out_dir,
            prefix=prefix, procs=procs, threads=threads,
            exists=exists, weights_from=weights_from,
            endpoint_url=endpoint_url, region=region, profile=profile,
        )
    err(f"listed {total:,} objects to {out_dir}")


def bulk_list_adaptive_uri(
    uri: str,
    out_dir: str,
    prefix: str | None = None,
    procs: int | None = None,
    threads: int = 8,
    exists: str = 'error',
    warm_from: str | None = None,
    endpoint_url: str | None = None,
    region: str | None = None,
    profile: str | None = None,
) -> int:
    """Scheme-dispatched adaptive listing (see `find/bulk_adaptive.py`)."""
    from disk_tree.backends.url import parse_url
    from disk_tree.find.bulk_adaptive import list_bucket_adaptive, load_warm_ranges

    parsed = parse_url(uri)
    eff_prefix = prefix if prefix is not None else (parsed.path.strip('/') or None)
    warm_ranges = load_warm_ranges(warm_from) if warm_from else None
    if parsed.scheme == 'gcs':
        from disk_tree.find.bulk_gcs import GcsBulkLister
        lister = GcsBulkLister()
    elif parsed.scheme in ('s3', 'r2'):
        if parsed.scheme == 'r2' and not endpoint_url:
            raise ValueError("r2:// requires --endpoint-url (Cloudflare R2's S3-compatible endpoint)")
        from disk_tree.find.bulk_s3 import S3BulkLister
        lister = S3BulkLister(scheme=parsed.scheme, endpoint_url=endpoint_url, region_name=region, profile=profile)
    else:
        raise ValueError(f"bulk-list requires a cloud URI (gcs://, s3://, r2://); got {uri!r}")
    return list_bucket_adaptive(
        lister, bucket=parsed.host, out_dir=out_dir,
        procs=procs, threads=threads, prefix=eff_prefix,
        exists=exists, warm_ranges=warm_ranges,
    )


def bulk_list_uri(
    uri: str,
    out_dir: str,
    prefix: str | None = None,
    procs: int | None = None,
    threads: int = 8,
    exists: str = 'error',
    weights_from: str | None = None,
    endpoint_url: str | None = None,
    region: str | None = None,
    profile: str | None = None,
) -> int:
    """Scheme-dispatched bulk listing; shared by `bulk-list` and `fetch`/`pull`/`sync`."""
    from disk_tree.backends.url import parse_url

    parsed = parse_url(uri)
    eff_prefix = prefix if prefix is not None else (parsed.path.strip('/') or None)
    if parsed.scheme == 'gcs':
        from disk_tree.find.bulk_gcs import list_gcs_bucket_to_parquet
        return list_gcs_bucket_to_parquet(
            bucket=parsed.host,
            out_dir=out_dir,
            procs=procs, threads=threads,
            prefix=eff_prefix,
            exists=exists, weights_from=weights_from,
        )
    if parsed.scheme in ('s3', 'r2'):
        if parsed.scheme == 'r2' and not endpoint_url:
            raise ValueError("r2:// requires --endpoint-url (Cloudflare R2's S3-compatible endpoint)")
        from disk_tree.find.bulk_s3 import list_s3_bucket_to_parquet
        return list_s3_bucket_to_parquet(
            bucket=parsed.host,
            out_dir=out_dir,
            procs=procs, threads=threads,
            prefix=eff_prefix,
            exists=exists, weights_from=weights_from,
            endpoint_url=endpoint_url,
            region_name=region,
            scheme=parsed.scheme,
            profile=profile,
        )
    raise ValueError(f"bulk-list requires a cloud URI (gcs://, s3://, r2://); got {uri!r}")
