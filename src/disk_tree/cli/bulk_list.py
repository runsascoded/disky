"""`disk-tree bulk-list` — sharded live listing of a cloud bucket to canonical
listing parquet (layer-1).

Feed the result into ``disk-tree import`` to get canonical per-path scans
(layer-2). Split so each stage stays independently retriable and cacheable:

    # 1. list bucket → shards under out_dir/*.parquet (+ _SUCCESS.json)
    disk-tree bulk-list gcs://my-bucket -o gs://my-data/listing/2026-08-05/my-bucket

    # 2. aggregate into a canonical scan
    disk-tree import -e duckdb -l 'gs://my-data/listing/2026-08-05/my-bucket/*.parquet' -b my-bucket
"""

from __future__ import annotations

import os

from click import Choice, argument, option
from utz import err

from disk_tree.cli.base import cli


def _resolve_procs(procs: int | None) -> int:
    """Default to one process per core: listing is GIL-bound per interpreter,
    so cores — not the endpoint — set the ceiling (see -P's help for the numbers)."""
    return procs or os.cpu_count() or 6


def _bucket(host: str | None, uri: str) -> str:
    if not host:
        raise ValueError(f"bulk-list needs a bucket in the URI; got {uri!r}")
    return host


@cli.command('bulk-list')
@option('-a', '--adaptive', is_flag=True, help='Adaptive range-splitting: workers bisect their remaining key range whenever peers are idle — no weights or fanout discovery needed (spec: specs/done/adaptive-listing.md)')
@option('-D', '--no-commit-dates', is_flag=True, help="hf:// only: date repo files by the repo's `lastModified` instead of each file's last commit (tree pages of 1000 entries instead of 50)")
@option('-E', '--endpoint-url', default=None, help='S3-compatible endpoint URL (required for r2://; also usable for MinIO / non-AWS S3)')
@option('-f', '--profile', default=None, help='AWS credential profile to authenticate with (for a cross-account source/target); default: ambient credentials')
@option('-L', '--lfs-files', is_flag=True, help='hf:// only: list old versions per LFS object (`@history/<file>/<oid>`) via `lfs-files`, which needs a token with write access; default: one `@history/@unlisted` row per repo for the bytes `usedStorage` bills beyond the current tree')
@option('-o', '--out', 'out_dir', required=True, help='Output dir for listing shards (local path or fsspec URL such as `gs://...`)')
@option('-p', '--prefix', default=None, help='Restrict listing to this bucket-relative prefix')
@option('-P', '--procs', default=None, type=int, help='Worker processes (default: os.cpu_count()). LIST throughput is GIL-bound, not endpoint-bound: a single process saturates at ~8K keys/s no matter how many threads it runs, while processes scale ~linearly (measured on CoreWeave: 1/2/4/8/16 procs -> 8.3K/17.1K/34.5K/68.1K/89.8K keys/s). Scale this with cores, not -w.')
@option('-r', '--region', default=None, help='AWS region name (S3 backend only)')
@option('-w', '--threads', default=3, help='Threads per worker (default 3; for hf://, the requests in flight — the Hub drew 429s at 8). Only enough to hide per-request latency (~0.1s) while the GIL is released on network I/O; beyond that threads contend for the GIL with parquet/pandas work on the same process and add nothing.')
@option('-W', '--weights-from', default=None, help='Glob to a prior listing parquet used to bin-pack + range-split hot prefixes (planned mode only)')
@option('-x', '--exists', type=Choice(['error', 'clear', 'reuse']), default='error', help='Behavior when --out already has shards: error/clear/reuse')
@option('--warm-from', default=None, help="Prior adaptive listing dir whose _SUCCESS.json range boundaries seed this run (adaptive mode only)")
@argument('uri')
def bulk_list_cmd(
    adaptive: bool,
    no_commit_dates: bool,
    endpoint_url: str | None,
    profile: str | None,
    lfs_files: bool,
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
    """Bulk-list `URI` (e.g. `gcs://bucket`, `hf://<namespace>`) to sharded listing parquet at `--out`."""
    if uri.startswith('hf://'):
        if adaptive or weights_from or warm_from or prefix or procs or endpoint_url or profile or region:
            raise ValueError("hf:// takes only -w, -x, -L and -D (narrow with the URI: hf://<ns>/<kind>/<name>)")
        from disk_tree.find.bulk_hf import list_hf_to_parquet
        total = list_hf_to_parquet(
            uri, out_dir=out_dir, threads=threads, exists=exists,
            lfs_files=lfs_files, commit_dates=not no_commit_dates,
        )
        err(f"listed {total:,} objects to {out_dir}")
        return
    if lfs_files or no_commit_dates:
        raise ValueError("-L/--lfs-files and -D/--no-commit-dates apply to hf:// only")
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
    from disk_tree.find.bulk_adaptive import PagedLister, list_bucket_adaptive, load_warm_ranges

    parsed = parse_url(uri)
    n_procs = _resolve_procs(procs)
    eff_prefix = prefix if prefix is not None else (parsed.path.strip('/') or None)
    warm_ranges = load_warm_ranges(warm_from) if warm_from else None
    lister: PagedLister
    if parsed.scheme == 'gcs':
        from disk_tree.find.bulk_gcs import GcsBulkLister
        lister = GcsBulkLister()
    elif parsed.scheme in ('s3', 'r2'):
        if parsed.scheme == 'r2' and not endpoint_url:
            raise ValueError("r2:// requires --endpoint-url (Cloudflare R2's S3-compatible endpoint)")
        from disk_tree.find.bulk_s3 import S3BulkLister
        lister = S3BulkLister(scheme=parsed.scheme, endpoint_url=endpoint_url, region_name=region, profile=profile)
    else:
        raise ValueError(f"bulk-list requires a cloud URI (gcs://, s3://, r2://, hf://); got {uri!r}")
    return list_bucket_adaptive(
        lister, bucket=_bucket(parsed.host, uri), out_dir=out_dir,
        procs=n_procs, threads=threads, prefix=eff_prefix,
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
    n_procs = _resolve_procs(procs)
    eff_prefix = prefix if prefix is not None else (parsed.path.strip('/') or None)
    if parsed.scheme == 'gcs':
        from disk_tree.find.bulk_gcs import list_gcs_bucket_to_parquet
        return list_gcs_bucket_to_parquet(
            bucket=_bucket(parsed.host, uri),
            out_dir=out_dir,
            procs=n_procs, threads=threads,
            prefix=eff_prefix,
            exists=exists, weights_from=weights_from,
        )
    if parsed.scheme in ('s3', 'r2'):
        if parsed.scheme == 'r2' and not endpoint_url:
            raise ValueError("r2:// requires --endpoint-url (Cloudflare R2's S3-compatible endpoint)")
        from disk_tree.find.bulk_s3 import list_s3_bucket_to_parquet
        return list_s3_bucket_to_parquet(
            bucket=_bucket(parsed.host, uri),
            out_dir=out_dir,
            procs=n_procs, threads=threads,
            prefix=eff_prefix,
            exists=exists, weights_from=weights_from,
            endpoint_url=endpoint_url,
            region_name=region,
            scheme=parsed.scheme,
            profile=profile,
        )
    raise ValueError(f"bulk-list requires a cloud URI (gcs://, s3://, r2://, hf://); got {uri!r}")
