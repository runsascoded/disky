"""``dt-cloud`` CLI.

``build`` derives the sparse dir -> user attribution table from a marin
``scan_gcs`` objects listing (parquet). The listing never loads fully into
pandas: DuckDB pre-filters it down to the two small row sets the signals
need (distinct ``users/<seg>/`` prefixes and record-file rows).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict
from functools import partial
from pathlib import Path

import duckdb
import pandas as pd
from click import Choice, UsageError, argument, group, option

from .cw_digest import REPLY_HOUR_UTC
from .identity import IDENTITIES_ENV, load_identities
from .site import DEFAULT_URL as SITE_DEFAULT_URL
from .secrets import env_secret, secret
from .index_footer import INDEX_VARIANTS
from .prefixes import load_prefix_map
from .records import mine_record_rows
from .signals import RECORD_BASENAME, manual_rows, record_file_paths, user_prefix_rows

err = partial(print, file=sys.stderr)


err = partial(print, file=sys.stderr)


def _connect() -> "duckdb.DuckDBPyConnection":
    """DuckDB with a hard memory cap — unbounded defaults (80% of RAM) have
    wedged the 61GB work node when combined with pandas-side structures."""
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{os.environ.get('DUCKDB_MEM', '24GB')}'")
    con.execute("SET threads=8")
    return con


@group()
def main() -> None:
    """Per-user attribution and reporting for Marin GCS storage."""


def _hard_exit() -> None:
    """Exit without interpreter teardown. The batch commands that stream GCS
    parquet through gcsfs hung at exit once (2026-09-08: last line printed,
    0% CPU for an hour) — fsspec's event loop being finalized while a file
    object's `__del__` still needs it. Every file is closed explicitly now;
    this is the guarantee."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)

err = partial(print, file=sys.stderr)


err = partial(print, file=sys.stderr)


@main.command()
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", required=True, type=Path, help="Output parquet path for the attribution table")
@option("-R", "--no-records", is_flag=True, help="Skip artifact-record mining (no GETs; path signals only)")
@option("-w", "--workers", default=16, help="Concurrent record reads")
def build(
    identities_path: str,
    listings: tuple[str, ...],
    out: Path,
    no_records: bool,
    workers: int,
) -> None:
    """Build the attribution table from a listing parquet."""
    identities = load_identities(identities_path)
    asof = dt.date.today()
    con = _connect()
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)

    users_df = con.execute(
        "SELECT DISTINCT bucket, regexp_extract(name, '^users/[^/]+/') AS name"
        f" FROM {src} WHERE name LIKE 'users/%'"
    ).df()
    records_df = con.execute(
        f"SELECT DISTINCT bucket, name FROM {src}"
        " WHERE regexp_extract(name, '[^/]+$') = ?",
        [RECORD_BASENAME],
    ).df()

    rows = user_prefix_rows(users_df, identities, asof) + manual_rows(identities, asof)
    if not no_records:
        paths = record_file_paths(records_df)
        err(f"record files to mine: {len(paths)}")
        record_rows, failed = mine_record_rows(paths, identities, asof, max_workers=workers)
        rows += record_rows
        if failed:
            err(f"unreadable record files ({len(failed)}):")
            for path in failed:
                err(f"  {path}")

    table = pd.DataFrame([asdict(row) for row in rows])
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)

    by_source = Counter(row.source for row in rows)
    err(f"wrote {len(rows)} attribution rows to {out}: {dict(by_source)}")
    unknown_users = sorted({row.user for row in rows if row.user is not None and not identities.known(row.user)})
    if unknown_users:
        err(f"users not in {identities_path} (add name/github/aliases): {unknown_users}")


@main.command("executor-mine")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", "out_path", type=Path, default=Path("tmp/executor-infos.parquet"), help="Output parquet")
@option("-w", "--workers", default=64, help="Concurrent GETs")
def executor_mine(listings: tuple[str, ...], out_path: Path, workers: int) -> None:
    """Targeted-GET mine of legacy `.executor_info` sidecars (name/output_path/config gs paths)."""
    from .executor_info import mine_executor_infos

    con = _connect()
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)
    paths = [
        f"gs://{b}/{n}"
        for b, n in con.execute(
            f"SELECT DISTINCT bucket, name FROM {src} WHERE name LIKE '%.executor_info'"
        ).fetchall()
    ]
    mine_executor_infos(paths, out_path, max_workers=workers)


@main.command("wandb-attr")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", required=True, type=Path, help="Output parquet path for wandb attribution rows")
@option("-r", "--runs", "runs_path", required=True, type=Path, help="wandb-mine output parquet")
@option("-x", "--executor-infos", "executor_path", type=Path, default=None, help="executor-mine output parquet (adds executor-wandb rows)")
def wandb_attr(
    identities_path: str,
    listings: tuple[str, ...],
    out: Path,
    runs_path: Path,
    executor_path: Path | None,
) -> None:
    """Attribution rows from W&B runs: run-name ↔ checkpoints/grug dirs + writer-path configs."""
    from .wandb_signal import executor_rows, run_name_rows, writer_path_rows

    identities = load_identities(identities_path)
    asof = dt.date.today()
    runs = pd.read_parquet(runs_path)
    err(f"{len(runs)} mined runs")
    con = _connect()
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)
    # Run-named dirs live at level 2 (checkpoints/<run>/) but also deeper under
    # namespace dirs — checkpoints/isoflop/<run>/, even
    # checkpoints/isoflop/isoflop/<run>/ — so emit levels 2-4 as (parent, leaf).
    run_dirs = con.execute(
        f"""
        WITH l AS (
          SELECT DISTINCT bucket,
            regexp_extract(name, '^([^/]+)/', 1) AS d1,
            regexp_extract(name, '^[^/]+/([^/]+)/', 1) AS d2,
            regexp_extract(name, '^[^/]+/[^/]+/([^/]+)/', 1) AS d3,
            regexp_extract(name, '^[^/]+/[^/]+/[^/]+/([^/]+)/', 1) AS d4
          FROM {src}
          WHERE (name LIKE 'checkpoints/%' OR name LIKE 'grug/%')
        )
        SELECT DISTINCT bucket, d1 AS parent, d2 AS leaf FROM l WHERE d2 IS NOT NULL
        UNION
        SELECT DISTINCT bucket, d1 || '/' || d2 AS parent, d3 AS leaf FROM l WHERE d3 IS NOT NULL
        UNION
        SELECT DISTINCT bucket, d1 || '/' || d2 || '/' || d3 AS parent, d4 AS leaf FROM l WHERE d4 IS NOT NULL
        """
    ).df()
    err(f"{len(run_dirs)} checkpoints/grug level-2/3/4 dirs")
    rows = run_name_rows(runs, run_dirs, identities, asof) + writer_path_rows(runs, identities, asof)
    if executor_path is not None:
        executor_df = pd.read_parquet(executor_path)
        err(f"{len(executor_df)} executor sidecars")
        rows += executor_rows(runs, executor_df, identities, asof)
    table = pd.DataFrame([asdict(row) for row in rows])
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)
    by_source = Counter(row.source for row in rows)
    err(f"wrote {len(rows)} attribution rows to {out}: {dict(by_source)}")


@main.command("attr-report")
@option("-a", "--attribution", "attributions", required=True, multiple=True, help="Attribution parquet(s); repeatable, concatenated")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-n", "--top", default=30, help="Rows in the per-user table")
@option("-u", "--user", "claim_user", default=None, help="Print this user's prefixes (inferred ownership, by bytes)")
def attr_report(
    attributions: tuple[str, ...],
    identities_path: str,
    listings: tuple[str, ...],
    top: int,
    claim_user: str | None,
) -> None:
    """Join listing × attribution (deepest-prefix-wins) → per-user bytes + coverage.

    Users are re-resolved against the *current* identities.yaml, so alias
    curation takes effect without rebuilding attribution parquets.
    """
    identities = load_identities(identities_path)
    con = _connect()
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)
    by_prefix = load_prefix_map(con, attributions, identities, src)

    dirs = con.execute(
        "SELECT bucket || '/' || CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,"
        " sum(size_bytes) AS bytes, count(*) AS objects"
        f" FROM {src} GROUP BY dir"
    ).df()
    err(f"{len(dirs)} distinct dirs")

    from collections import defaultdict

    per_user: dict[str | None, list] = defaultdict(lambda: [0, 0])
    per_source: dict[str, list] = defaultdict(lambda: [0, 0])
    claim: dict[str, list] = defaultdict(lambda: [0, 0])  # attributed-ancestor prefix -> [bytes, objects] for --user
    cache: dict[str, tuple | None] = {}
    prefix_of: dict[str, str] = {}  # dir_key -> matched attribution prefix (only tracked when --user)

    def deepest(dir_key: str) -> tuple | None:
        """Attribution of the deepest attributed ancestor of gs-less 'bucket/a/b'."""
        hit = cache.get(dir_key)
        if hit is not None or dir_key in cache:
            return hit
        probe = dir_key
        chopped = []
        result = None
        while True:
            row = by_prefix.get(f"gs://{probe}/")
            if row is not None:
                result = row
                break
            if "/" not in probe:
                break
            chopped.append(probe)
            probe = probe.rsplit("/", 1)[0]
        for key in chopped:
            cache[key] = result
            if result is not None:
                prefix_of[key] = f"gs://{probe}/"
        cache[dir_key] = result
        if result is not None:
            prefix_of[dir_key] = f"gs://{probe}/"
        return result

    total_bytes = int(dirs["bytes"].sum())
    for dir_key, nbytes, objects in zip(dirs["dir"], dirs["bytes"], dirs["objects"]):
        row = deepest(dir_key)
        user, source = row if row else (None, "none")
        per_user[user][0] += int(nbytes)
        per_user[user][1] += int(objects)
        per_source[source][0] += int(nbytes)
        per_source[source][1] += int(objects)
        if claim_user is not None and user == claim_user:
            c = claim[prefix_of[dir_key]]
            c[0] += int(nbytes)
            c[1] += int(objects)

    print("== coverage by source ==")
    for source, (nbytes, objects) in sorted(per_source.items(), key=lambda kv: -kv[1][0]):
        print(f"{source:>16}  {nbytes/1e12:10.2f} TB  {objects:>12,} objects  {100*nbytes/total_bytes:5.1f}%")

    print(f"\n== top {top} users by bytes ('-' = nobody) ==")
    rows = sorted(per_user.items(), key=lambda kv: -kv[1][0])[:top]
    for user, (nbytes, objects) in rows:
        print(f"{user or '-':>24}  {nbytes/1e12:10.3f} TB  {objects:>12,} objects")

    if claim_user is not None:
        print(f"\n== claim list: {claim_user} ({len(claim)} prefixes) ==")
        for prefix, (nbytes, objects) in sorted(claim.items(), key=lambda kv: -kv[1][0]):
            print(f"{nbytes/1e9:12.2f} GB  {objects:>10,} objects  {prefix}")


@main.command()
@option("-a", "--attribution", "attributions", required=True, multiple=True, help="Attribution parquet(s); repeatable, concatenated")
@option("-d", "--depth", default=2, help="Prefix depth for the gap rollup (name components after bucket)")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-n", "--top", default=40, help="Rows in the gap table")
def gaps(
    attributions: tuple[str, ...],
    depth: int,
    identities_path: str,
    listings: tuple[str, ...],
    top: int,
) -> None:
    """Largest *unattributed* prefixes at a given depth — the targeting list for
    new signals and `prefix_owners` curation."""
    identities = load_identities(identities_path)
    con = _connect()
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)
    by_prefix = load_prefix_map(con, attributions, identities, src)

    dirs = con.execute(
        "SELECT bucket || '/' || CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,"
        " sum(size_bytes) AS bytes, count(*) AS objects"
        f" FROM {src} GROUP BY dir"
    ).df()
    err(f"{len(dirs)} distinct dirs")

    from collections import defaultdict

    cache: dict[str, bool] = {}

    def attributed(dir_key: str) -> bool:
        hit = cache.get(dir_key)
        if hit is not None:
            return hit
        probe = dir_key
        chopped = []
        result = False
        while True:
            if f"gs://{probe}/" in by_prefix:
                result = True
                break
            if "/" not in probe:
                break
            chopped.append(probe)
            probe = probe.rsplit("/", 1)[0]
        for key in chopped:
            cache[key] = result
        cache[dir_key] = result
        return result

    gap: dict[str, list] = defaultdict(lambda: [0, 0])
    total_gap = 0
    for dir_key, nbytes, objects in zip(dirs["dir"], dirs["bytes"], dirs["objects"]):
        if attributed(dir_key):
            continue
        total_gap += int(nbytes)
        head = "/".join(dir_key.split("/")[: depth + 1])  # bucket + depth components
        g = gap[head]
        g[0] += int(nbytes)
        g[1] += int(objects)

    print(f"== top {top} unattributed prefixes at depth {depth} ({total_gap/1e12:.1f} TB total gap) ==")
    for head, (nbytes, objects) in sorted(gap.items(), key=lambda kv: -kv[1][0])[:top]:
        print(f"{nbytes/1e12:9.3f} TB  {objects:>12,} objects  gs://{head}/")


@main.command("wandb-mine")
@option("-e", "--entity", default="marin-community", help="W&B entity to mine")
@option("-E", "--print-edges", is_flag=True, help="Print bisection-tree edges (valid --since/--until values for parallel workers) and exit")
@option("-j", "--jobs", default=1, help="Concurrent (project, window) mining tasks — network-bound threads; ~8 is safe per API key")
@option("-M", "--no-merge", is_flag=True, help="Skip the final concat (parallel range-workers; run once without to merge)")
@option("-o", "--out", "out_path", type=Path, default=Path("tmp/wandb-runs.parquet"), help="Output parquet")
@option("-p", "--project-filter", default=None, help="Substring filter on project names")
@option("-s", "--since", default=None, help="Window start (bisection-tree edge; see -E)")
@option("-u", "--until", default=None, help="Window end (bisection-tree edge; see -E)")
def wandb_mine(
    entity: str,
    print_edges: bool,
    jobs: int,
    no_merge: bool,
    out_path: Path,
    project_filter: str | None,
    since: str | None,
    until: str | None,
) -> None:
    """Mine W&B run metadata (identity + config gs:// paths) for attribution."""
    from .wandb_mine import ROOT_SINCE, ROOT_UNTIL, mine_entity, window_edges

    if print_edges:
        for edge in window_edges():
            print(edge)
        return
    mine_entity(
        entity,
        out_path,
        project_filter,
        since=since or ROOT_SINCE,
        until=until or ROOT_UNTIL,
        merge=not no_merge,
        jobs=jobs,
    )


@main.command("path-index")
@option("-a", "--attribution", "attributions", multiple=True, help="Attribution parquet(s); adds per-node user overlays")
@option("-c", "--dir-cache", "dir_cache", type=Path, default=None, help="Layer-2 cache dir (dir-stats/age-days parquet): attribution-independent rollups reused by re-attribution runs — see specs/dir-agg-cache.md")
@option("-d", "--asof", required=True, help="Scan date the listing came from (YYYY-MM-DD)")
@option("-g", "--age-strata", is_flag=True, help="Add bytes-by-age columns (`age_b0`…`age_b6`, specs/done/row-age-strata.md) to every store row; off by default, so a store keeps its schema until it opts in")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, default=None, help=f"identities.yaml path or URL, needed with -a (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Output dir for JSON files [default: site/public/data/<asof>]")
@option("-r", "--row-group-rows", default=None, type=int, help="Parquet row-group size for the store's sorts (default 8192 — the reader decodes ~one group per depth of a drilled subtree, so 32768 pushed small drills past its decode cap; specs/path-store.md §1.6)")
@option("-u", "--user-sort-tiers", default=None, help="Only these sorts get a `-by-user` copy, comma-separated (`bysize`: the copy a lens view reads; default every sort)")
@option("-U", "--no-user-sorts", "user_sorts", is_flag=True, flag_value=False, default=True, help="Skip every `-by-user` sort copy (a lens then reads the mixed-user sorts, filtered per row — fine below the root, too wide at a user's root view; see -u)")
@option("-P", "--path-index", "path_index", type=Path, default=None, help="Write the path store here (`<dir>/path-index.parquet`, the `path` sort; `path-index-bysize.parquet` and the by-user copies land beside it — specs/path-store.md §4.3)")
@option("-x", "--access", "access", multiple=True, help="Access-log layer-2a agg parquet glob(s); adds per-node last-read ('a') for the read-recency lens")
def build_path_index(
    attributions: tuple[str, ...],
    dir_cache: Path | None,
    asof: str,
    age_strata: bool,
    identities_path: str | None,
    listings: tuple[str, ...],
    out_dir: Path | None,
    path_index: Path | None,
    row_group_rows: int | None,
    user_sort_tiers: str | None,
    user_sorts: bool,
    access: tuple[str, ...],
) -> None:
    """Generate a dated site-data snapshot (tree/age/meta JSONs) from a listing.

    Snapshots live at site/public/data/<asof>/; the sibling scans.json index
    (dates, newest first — the site's scan dropdown) is refreshed afterwards.
    """
    import json
    import re

    from .viz import write_path_index

    if out_dir is None:
        out_dir = Path("site/public/data") / asof
    meta = write_path_index(
        listings, out_dir, asof, attributions, identities_path, access=access, dir_cache=dir_cache, path_index=path_index,
        user_sorts=user_sorts, row_group_rows=row_group_rows, age_strata=age_strata,
        user_sort_tiers=tuple(t for t in user_sort_tiers.split(",") if t) if user_sort_tiers else None,
    )
    err(f"wrote {out_dir}/: age.json meta.json ({meta['total_bytes']/1e12:.0f} TB, {meta['total_objects']:,} objects)")
    data_root = out_dir.parent
    dates = sorted(
        (
            p.name
            for p in data_root.iterdir()
            if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) and (p / "meta.json").exists()
        ),
        reverse=True,
    )
    if dates:
        (data_root / "scans.json").write_text(json.dumps(dates) + "\n")
        err(f"scans.json: {dates}")


@main.command()
@option("-o", "--out", "out_root", type=Path, required=True, help="Local root; objects land at <out>/<bucket>/<object name>")
@option("-w", "--workers", default=16, help="Concurrent downloads")
@argument("globs", nargs=-1, required=True)
def stage(out_root: Path, workers: int, globs: tuple[str, ...]) -> None:
    """Stage /gcs/<bucket>/<pattern> globs onto local disk (parallel download).

    gcsfuse reads are slow (~20-50 MB/s) and path-index makes several passes over
    its inputs; staging to local NVMe first makes those passes local-speed.
    Already-staged files (same size) are skipped, so re-runs are idempotent.
    """
    from .stage import stage_globs

    stage_globs(globs, out_root, workers)


@main.command()
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-o", "--out", type=Path, default=None, help="Write rules JSON (users/aliases/prefix_owners + notes) for the site")
def rules(identities_path: str, out: Path | None) -> None:
    """Validate identities.yaml; optionally export it as site JSON.

    Checks alias collisions/shadowing and prefix_owners rows
    referencing unknown users or malformed/duplicate prefixes. Exits nonzero
    on findings (JSON is still written, so the site shows current state).
    """
    import json

    from .rules import export_rules

    payload, findings = export_rules(identities_path)
    for finding in findings:
        err(f"FINDING: {finding}")
    err(f"{len(payload['users'])} users, {len(payload['prefix_owners'])} prefix rules, {len(findings)} findings")
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1) + "\n")
        err(f"wrote {out}")
    if findings:
        raise SystemExit(1)


# --- index tiers + their D1 footers (specs/view-serving.md, ported from gcs) ---


@main.command()
@option("-d", "--date", default=None, help="Scan date YYYY-MM-DD (default: latest from scans.json)")
@option("-f", "--max-age-days", default=2, type=int, help="Freshness: latest scan must be within this many days")
@option("-j", "--json", "as_json", is_flag=True, help="Emit machine-readable JSON to stdout")
@option("-s", "--subdir", default=None, help="Snapshot subdir under /data/ (default: $SNAPSHOTS_SUBDIR; `cw` for the CoreWeave deployment, empty for the default store)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {SITE_DEFAULT_URL})")
def healthcheck(date: str | None, max_age_days: int, as_json: bool, subdir: str | None, token: str | None, url: str | None) -> None:
    """Live-site health: is the latest scan actually *servable* end-to-end?

    Catches failures where the data pipeline succeeds but the site can't serve
    the scan — e.g. a path-index footer that never synced to D1, so the site
    footer-parses and 1102s (the 2026-08-31 /users outage). Checks scan
    freshness, subtree (the D1-index serving path), and the published data
    JSONs. Exits nonzero if any check fails — wire it into a cron /
    post-snapshot gate.
    """
    from .healthcheck import as_dict, run_checks
    from .site import creds

    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("error: no token — pass --token or set $GCS_USAGE_TOKEN")
    sub = subdir if subdir is not None else os.environ.get("SNAPSHOTS_SUBDIR", "")
    resolved, checks = run_checks(base, tok, date, max_age_days=max_age_days, subdir=sub)
    err(f"healthcheck {base} @ {resolved or '?'}")
    for c in checks:
        err(f"  {'✓' if c.ok else '✗'} {c.name:<16} {c.detail}")
    n_ok = sum(c.ok for c in checks)
    ok = n_ok == len(checks)
    err(f"{'PASS' if ok else 'FAIL'} ({n_ok}/{len(checks)})")
    if as_json:
        print(json.dumps(as_dict(resolved, checks), indent=2))
    if not ok:
        raise SystemExit(1)


def bucket_sources(specs: tuple[str, ...], default_bucket: str) -> list[tuple[str, str]]:
    """`<bucket>=<layer-2 parquet>` pairs → [(bucket, path)]; a bare path is
    ``default_bucket``'s (the single-bucket form)."""
    out: list[tuple[str, str]] = []
    for s in specs:
        bucket, eq, path = s.partition("=")
        out.append((bucket, path) if eq else (default_bucket, s))
    return out


@main.command("index-write")
@option("-A", "--age-only", is_flag=True, help="Only the age pyramid — skip the store's sorts (a ladder-only backfill; sync with `index-sync -A`, the sort pointers keep their generation)")
@option("-b", "--bucket", default=None, help="Bucket a bare (no `<bucket>=`) layer-2 argument describes (default $CW_BUCKET)")
@option("-m", "--mem", default="8GB", help="DuckDB memory limit")
@option("-o", "--out", "out_dir", type=Path, required=True, help="Output dir: path-index.parquet + path-index-bysize.parquet (+ .groups.json sidecars) + age-pyramid-*.parquet")
@option("-r", "--row-group-rows", default=8192, type=int, help="Parquet row-group size for the sorts — the range-read unit and the D1 footer's row count per sort (default 8192; a gcs-sized fleet uses 32768, specs/path-store.md §1.6)")
@option("-t", "--threads", default=8, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", type=Path, default=None, help="DuckDB spill dir (default: <out>/.duckdb-tmp)")
@argument("sources", nargs=-1, required=True)
def index_write(age_only: bool, bucket: str | None, mem: str, out_dir: Path, row_group_rows: int, threads: int, tmp_dir: Path | None, sources: tuple[str, ...]) -> None:
    """Write the scan's path store from its layer-2 parquet(s) — SOURCES are
    `<bucket>=<l2.parquet>` pairs, one per bucket of the scan (a bare path is
    `-b`'s bucket): every row (objects and dirs), bucket-prefixed, in the
    layer-2's column names, cut by the engine into the `path` sort
    (`path-index.parquet`, `(depth, path)`) and the `bysize` sort
    (`path-index-bysize.parquet`, `(⌊log2 size⌋ desc, path)`), 8k-row groups,
    footer sidecars beside them, plus the age pyramid (specs/path-store.md
    §4.2). `index-sync` then publishes their footers to D1."""
    from .index import write_index
    from .sweep import CW_BUCKET

    s = write_index(
        bucket_sources(sources, bucket or CW_BUCKET), out_dir,
        mem=mem, threads=threads, tmp_dir=tmp_dir, age_only=age_only, row_group_rows=row_group_rows,
    )
    if age_only:
        err(f"index-write: age pyramid only — floor {s['pyramid']['floor']}, {len(s['pyramid']['bins'])} tiers over {s['buckets']}")
    else:
        err(f"index-write: {s['rows']:,} rows over {s['buckets']}; sorts {s['sorts']}")
    print(json.dumps(s))


@main.command("over-time-groups")
@option("-b", "--bucket", default="oa-gcs-usage-dvx", help="Data bucket the D1 `path` dirs resolve against")
@option("-g", "--gen", required=True, help="Generation id for the published index dirs (the run's, e.g. the job's $GEN)")
@option("-K", "--group-size", default=None, type=int, help="Scans per sealed group (default: dt_cloud.overtime.OVER_TIME_GROUP_SIZE)")
@option("-l", "--layer2-prefix", default=None, help="Layer-2 dir template with `{scan}` (default: $LAYER2_PREFIX, e.g. cw-l2/{scan}/)")
@option("-m", "--mem", default="8GB", help="DuckDB memory limit")
@option("-n", "--dry-run", is_flag=True, help="Print the groups that would be built; write nothing")
@option("-o", "--out", "out_dir", type=Path, required=True, help="Work dir for the group builds")
@option("-p", "--publish-root", default=None, help="Where the published dirs live (default /gcs/<bucket>, the Batch mount); groups land at <root>/<layer2>/index/<gen>/")
@option("-r", "--data-root", default=None, help="Root the D1 `path` pointer dirs resolve against (default: the publish root)")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-t", "--threads", default=8, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", type=Path, default=None, help="DuckDB spill dir (default: <out>/.duckdb-tmp)")
def over_time_groups(
    bucket: str,
    gen: str,
    group_size: int | None,
    layer2_prefix: str | None,
    mem: str,
    dry_run: bool,
    out_dir: Path,
    publish_root: str | None,
    data_root: str | None,
    store: str,
    threads: int,
    tmp_dir: Path | None,
) -> None:
    """Seal the next over-time groups (specs/obs-axis-indexing.md Phase 1, the
    capped-K shape): partition every scan with a synced `path` index into fixed
    K-scan groups, oldest first, and for each group not yet in the manifest
    build its over-time MS, publish it under the group's LAST scan's layer-2 dir
    (`<layer2>/index/<gen>/over-time.parquet` + scans sidecar), sync the footer
    (`index_schema`/`index_row_groups` as variant `over-time`) and, last, write
    the `pyramid_multiscans` row the site routes by. Idempotent: sealed groups
    never change, so a re-run only appends. Prints `{"groups": [<gid>, …]}` for
    the caller to `publish-r2` each new group's scan dir. The < K tail is served
    by `/api/series`'s per-scan fallback."""
    import shutil
    import time

    from .index_footer import index_dir, sync_d1, synced_variants
    from .overtime import OVER_TIME_GROUP_SIZE, multiscan_row, sealed_groups, sync_manifest, synced_groups, write_over_time_index

    K = group_size or OVER_TIME_GROUP_SIZE
    l2 = layer2_prefix or os.environ.get("LAYER2_PREFIX") or "cw-l2/{scan}/"
    root = publish_root or f"/gcs/{bucket}"
    data = data_root or root
    dates = sorted({d for d, v in synced_variants(store=store) if v == "path"})
    done = synced_groups(store=store)
    todo = [g for g in sealed_groups(dates, K) if g[-1] not in done]
    err(f"over-time-groups: {len(dates)} indexed scans → {len(sealed_groups(dates, K))} sealed groups of {K}, {len(done)} in the manifest, {len(todo)} to build")
    if dry_run:
        for g in todo:
            err(f"  would build {g[-1]}: {g[0]}..{g[-1]} ({len(g)} scans)")
        print(json.dumps({"groups": [g[-1] for g in todo], "dry_run": True}))
        return
    built: list[str] = []
    for g in todo:
        gid = g[-1]
        pairs: list[tuple[str, str]] = []
        for d in g:
            dir_ = index_dir(d, "path", store=store)
            if dir_ is None:
                raise SystemExit(f"over-time-groups: no `path` pointer for {d} (group {gid})")
            pairs.append((d, f"{data}/{dir_}/path-index.parquet"))
        summ = write_over_time_index(pairs, out_dir / gid, mem=mem, threads=threads, tmp_dir=tmp_dir)
        key = f"{l2.format(scan=gid).rstrip('/')}/index/{gen}"
        dest = Path(root) / key
        dest.mkdir(parents=True, exist_ok=True)
        for f in (summ["file"], summ["scans_file"]):
            shutil.copy2(f, dest / Path(f).name)
        n = sync_d1(gid, str(dest / Path(summ["file"]).name), variant="over-time", gen=gen, key=key, store=store)
        sync_manifest(multiscan_row(g, written_at_ms=int(time.time() * 1000), store=store))
        err(f"over-time-groups: sealed {gid} ({g[0]}..{gid}, {len(g)} scans, {summ['rows']:,} intervals, {n} row groups) → {key}")
        built.append(gid)
    print(json.dumps({"groups": built}))


@main.command("index-sync")
@option("-A", "--age-only", is_flag=True, help="Only the age-pyramid variants (a ladder-only backfill; the other variants keep their pointer)")
@option("-b", "--bucket", default="oa-gcs-usage-dvx", help="Data bucket holding the index tiers")
@option("-d", "--dir", "listing_dir", default=None, help="Local/mounted dir holding the parquets (default: <bucket>/<key>)")
@option("-F", "--sorts-only", is_flag=True, help="Only the store's sorts (path, bysize, and their by-user copies where written)")
@option("-g", "--gen", required=True, help="Generation stamp these files belong to (the run's GEN; `legacy` for the pre-generation listing/<date>/ layout)")
@option("-k", "--key", default=None, help="Bucket-relative dir the parquets live under — what the site reads (default: listing/<date>/index/<gen>; listing/<date> for gen `legacy`)")
@option("-L", "--local", is_flag=True, help="Write to the local wrangler D1 instead of --remote")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-v", "--variant", "variants", multiple=True, type=Choice(list(INDEX_VARIANTS)), help="Only sync these variants (default: all)")
@argument("date")
def index_sync(
    age_only: bool,
    bucket: str,
    listing_dir: str | None,
    sorts_only: bool,
    gen: str,
    key: str | None,
    local: bool,
    store: str,
    variants: tuple[str, ...],
    date: str,
) -> None:
    """Publish a scan's index-tier footers to D1 (index_row_groups + the
    index_schema pointer) — one generation of files under one bucket dir.
    Per variant the row groups land first, tagged with the generation, and the
    pointer (gen, dir) flips last, so the site moves from the previous complete
    generation to this one with no window (specs/view-serving.md). Needs
    CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID in the env. `--store` files
    the rows under a secondary store (variants recorded as
    `<store>:<variant>`, the `store` column set — needs the store migration;
    specs/multi-store.md). The default, the primary, writes exactly the
    pre-stores SQL."""
    from .index_footer import SORT_VARIANTS, check_store, exists, sync_d1

    try:
        check_store(store)
    except ValueError as e:
        raise UsageError(str(e)) from None

    key = key or (f"listing/{date}" if gen == "legacy" else f"listing/{date}/index/{gen}")
    base = listing_dir or f"{bucket}/{key}"
    todo = variants or tuple(INDEX_VARIANTS)
    if sorts_only:
        todo = tuple(v for v in todo if v in SORT_VARIANTS)
    if age_only:
        todo = tuple(v for v in todo if v.startswith("age-pyramid"))
    # A deployment produces only some variants (gcs's `path-index` writes no
    # age pyramid or over-time tier; a generation before the store has no
    # `bysize`); an absent file is skipped, not fatal — otherwise every variant
    # after it in `INDEX_VARIANTS` order went unsynced.
    skipped = []
    for variant in todo:
        path = f"{base}/{INDEX_VARIANTS[variant]}"
        if not exists(path):
            skipped.append(variant)
            continue
        n = sync_d1(date, path, variant=variant, gen=gen, key=key, remote=not local, store=store)
        err(f"index-sync: {'' if store == 'primary' else f'[{store}] '}{date} [{variant}] gen {gen} @ {key} — {n} row groups ({'local' if local else 'remote'})")
    if skipped:
        err(f"index-sync: {date} gen {gen} @ {key} — skipped {len(skipped)} absent variant(s): {', '.join(skipped)}")
    if len(skipped) == len(todo):
        err(f"index-sync: no variant file under {base}")
        raise SystemExit(1)


@main.command("index-gc")
@option("-b", "--base", default="oa-gcs-usage-dvx", help="Where the pointers' dirs live, for -r's cold-footer check: the data bucket (default oa-gcs-usage-dvx), a mounted dir, or an fsspec URL (`r2://bucket`)")
@option("-r", "--retain", type=int, default=None, help="Retention: also retire the store sorts' row groups of every scan older than the newest N whose `.groups.parquet` exists (their pointers stay; the reader range-reads that cold footer instead). A variant without one keeps its rows (warned): backfill it with `index-blob`")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@argument("dates", nargs=-1)
def index_gc(base: str, retain: int | None, store: str, dates: tuple[str, ...]) -> None:
    """Delete row groups of index generations no pointer names — a REPROC's
    previous generation, or a sync that died before flipping. All synced
    scans by default; DATES to restrict. With -r, the retention pass too."""
    from .index_footer import gc_d1, retire_d1, synced_variants

    todo = dates or sorted({d for d, _ in synced_variants(store=store)})
    for d in todo:
        n = gc_d1(d, store=store)
        err(f"index-gc: {d} — {n} stale row groups deleted")
    if retain is not None:
        retired, skipped = retire_d1(retain, store=store, base=base)
        for d, v, n in retired:
            err(f"index-gc: retired {d} [{v}] — {n} row groups (its .groups.parquet serves it now)")
        for d, v, missing in skipped:
            err(f"index-gc: WARNING kept {d} [{v}] in D1 — no cold footer at {missing} (backfill: dt-cloud index-blob -P {d})")


@main.command("index-dir")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-v", "--variant", default="path", type=Choice(list(INDEX_VARIANTS)), help="Which variant's dir")
@argument("date")
def index_dir_cmd(store: str, variant: str, date: str) -> None:
    """Print the bucket-relative dir holding a scan's index variant (the D1
    pointer). Exits 1, printing nothing, when that (date, variant) was never
    synced."""
    from .index_footer import index_dir

    d = index_dir(date, variant, store=store)
    if d is None:
        raise SystemExit(1)
    print(d)


@main.command("labels")
@option("-a", "--attribution", "attributions", multiple=True, help="Attribution parquet(s) (as `path-index -a`)")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, default=None, help=f"identities.yaml path or URL, needed with -a (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s) — path-glob rules expand against their dirs")
@option("-o", "--out", "out_dir", type=Path, required=True, help="Output dir: one labels-<bucket>.parquet per bucket")
def labels(attributions: tuple[str, ...], identities_path: str | None, listings: tuple[str, ...], out_dir: Path) -> None:
    """Export mgu's attribution as DT label tables — `(prefix, usr)` per bucket,
    prefix relative to the bucket — for `disk-tree import -e duckdb -L
    labels-<bucket>.parquet -c usr` (spec mgu-scale-unification.md §B): the
    same prefix map `path-index` attributes with, so the two cascades can be
    compared slice for slice."""
    import duckdb

    from .viz import write_labels

    con = duckdb.connect()
    for bucket, n in write_labels(con, listings, attributions, identities_path, out_dir).items():
        err(f"labels: {bucket}: {n} prefixes → {out_dir / f'labels-{bucket}.parquet'}")


@main.command("index-blob")
@option("-a", "--all", "all_dates", is_flag=True, help="Every scan with a synced pointer (instead of DATES)")
@option("-b", "--bucket", default="oa-gcs-usage-dvx", help="Data bucket holding the index tiers")
@option("-d", "--dir", "listing_dir", default=None, help="Local/mounted/gs:// dir holding the parquets (default: gs://<bucket>/<key>); one DATE only")
@option("-g", "--gen", default=None, help="Generation the files belong to (`legacy` for listing/<date>/); default: each variant's D1 pointer dir")
@option("-J", "--from-json", is_flag=True, help="Build the .groups.parquet from the .groups.json already beside the tier, not the tier's own footer (implies -P)")
@option("-k", "--key", default=None, help="Bucket-relative dir the parquets live under (default: listing/<date>/index/<gen>; listing/<date> for gen `legacy`)")
@option("-n", "--dry-run", is_flag=True, help="Print what would be written; write nothing")
@option("-P", "--parquet-only", is_flag=True, help="Only the cold footer tier (`.groups.parquet`); leave the .groups.json as it is")
@option("-s", "--store", default="primary", help="The store whose pointers name the dirs (no -g): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-S", "--sorts-only", is_flag=True, help="Only the store's sorts (`SORT_VARIANTS`: what `index-gc -r` retires)")
@option("-v", "--variant", "variants", multiple=True, type=Choice(list(INDEX_VARIANTS)), help="Only these variants (default: all)")
@argument("dates", nargs=-1)
def index_blob(
    all_dates: bool,
    bucket: str,
    listing_dir: str | None,
    gen: str | None,
    from_json: bool,
    key: str | None,
    dry_run: bool,
    parquet_only: bool,
    store: str,
    sorts_only: bool,
    variants: tuple[str, ...],
    dates: tuple[str, ...],
) -> None:
    """Write each tier's footer sidecars beside its parquet — the cold footer
    tier `<tier>.groups.parquet` and the `<tier>.groups.json` blob, the rows
    `index-sync` puts in D1. The backfill for generations synced before
    `index-sync` wrote them: `index-gc -r` retires a scan's rows from D1 only
    once its `.groups.parquet` exists, and the site then range-reads it
    (specs/path-store.md §1.6). E.g. before lowering retention on gcs:

        dt-cloud index-blob -a -S -P

    Each variant's dir comes from `-d`/`-k`/`-g`, else its D1 pointer; an
    absent tier (a deployment writes only some variants) is skipped."""
    from .index_footer import SORT_VARIANTS, exists, extract, groups_parquet_path, index_dir, read_groups_blob, synced_variants, write_groups_blob, write_groups_parquet

    if all_dates == bool(dates):
        raise UsageError("give DATES or -a, not both")
    if listing_dir and (all_dates or len(dates) > 1):
        raise UsageError("-d names one scan's dir: one DATE only")
    by_pointer = not (listing_dir or gen is not None or key is not None)
    synced = synced_variants(store=store) if all_dates or by_pointer else []
    todo_dates = sorted({d for d, _ in synced}) if all_dates else list(dates)
    todo_vars = variants or tuple(INDEX_VARIANTS)
    if sorts_only:
        todo_vars = tuple(v for v in todo_vars if v in SORT_VARIANTS)
    have = set(synced)
    for date in todo_dates:
        for variant in todo_vars:
            if listing_dir:
                base = listing_dir
            elif gen is not None or key is not None:
                k = key or (f"listing/{date}" if gen == "legacy" else f"listing/{date}/index/{gen}")
                base = f"gs://{bucket}/{k}"
            else:
                if (date, variant) not in have:
                    continue
                k = index_dir(date, variant, store=store)
                if k is None:
                    continue
                base = f"gs://{bucket}/{k}"
            path = f"{base}/{INDEX_VARIANTS[variant]}"
            if not exists(path):
                err(f"index-blob: {date} [{variant}] no tier at {path}; skipped")
                continue
            if dry_run:
                err(f"index-blob: {date} [{variant}] would write {groups_parquet_path(path)}{'' if parquet_only or from_json else ' + .groups.json'}")
                continue
            schema, rows = read_groups_blob(path) if from_json else extract(path)
            out, n = write_groups_parquet(path, schema, rows)
            err(f"index-blob: {date} [{variant}] {len(rows)} groups → {out} ({n:,} B)")
            if not (parquet_only or from_json):
                out, n = write_groups_blob(path, schema, rows)
                err(f"index-blob: {date} [{variant}] {len(rows)} groups → {out} ({n:,} B)")


@main.command("index-extras")
@option("-a", "--attribution", "attributions", multiple=True, required=True, help="Attribution parquet(s) (as `path-index -a`)")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Where to write attr.tsv (default: beside the index)")
@option("-P", "--path-index", "path_index", type=Path, required=True, help="Floor-free path-index.parquet of the scan (every dir is a row)")
@argument("date")
def index_extras(attributions: tuple[str, ...], identities_path: str, out_dir: Path | None, path_index: Path, date: str) -> None:
    """Backfill a scan's provenance sidecar (`attr.tsv`) from its floor-free
    path index + attribution parquets: each attributing prefix's user /
    source / evidence. `path-index` writes the same file for a fresh scan."""
    import duckdb

    from .extras import write_extras
    from .viz import prefix_labels

    con = duckdb.connect()
    src = f"read_parquet('{path_index}')"
    con.execute(f"CREATE TEMP VIEW idx_dirs AS SELECT DISTINCT path AS fp FROM {src}")
    # Path-glob rules expand against `(bucket, name)` dirs — from the index's own paths.
    con.execute(
        "CREATE TEMP VIEW listing_dirs AS SELECT split_part(fp, '/', 1) AS bucket,"
        " CASE WHEN position('/' IN fp) > 0 THEN substr(fp, position('/' IN fp) + 1) END AS name FROM idx_dirs"
    )
    pfx_df = prefix_labels(con, attributions, identities_path, "listing_dirs")
    counts = write_extras(pfx_df, out_dir or path_index.parent)
    err(f"index-extras {date}: {json.dumps(counts)}")


@main.command("warm-cache")
@option("-d", "--date", help="Scan to warm (default: latest under --root)")
@option("-j", "--jobs", default=4, type=int, help="Concurrent requests (default 4)")
@option("-n", "--dry-run", is_flag=True, help="Print the request paths; fetch nothing")
@option("-r", "--root", help="Snapshots root (default gs://$DATA_BUCKET/snapshots)")
@option("-t", "--token", help="Site read token (default $GCS_USAGE_TOKEN)")
@option("-u", "--url", "site_url", default=None, help="Site base (default gcs.oa.dev)")
@option("-W", "--widths", default="512,1280,1536,1792,1920", help="Canvas widths to warm (the client sends ceil(innerWidth/128)*128; default = phone + common laptops)")
def warm_cache(date: str | None, jobs: int, dry_run: bool, root: str | None, token: str | None, site_url: str | None, widths: str) -> None:
    """Warm the site's subtree + diff caches for a scan: replay the home
    page's default requests (one subtree, the diff span chips 1d/3d/7d/14d/30d
    and the previous-scan pair, each with its summary) at the common canvas
    widths, so the first viewer anywhere gets a cache hit (colo cache + global
    KV). Non-fatal: a failed request just leaves that view cold."""
    from . import warm as wm

    # Deployment config: SITE_URL / SNAPSHOTS_SUBDIR (the CoreWeave job exports
    # cw-s3.oa.dev + snapshots/cw); defaults are the GCS deployment's.
    site_url = site_url or os.environ.get("SITE_URL") or SITE_DEFAULT_URL
    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots" + (f"/{os.environ['SNAPSHOTS_SUBDIR'].strip('/')}" if os.environ.get('SNAPSHOTS_SUBDIR') else '')
    dates = wm.scan_dates(root)
    if not dates:
        raise SystemExit("warm-cache: no scans under root")
    date = date or dates[-1]
    if date not in dates:
        raise SystemExit(f"warm-cache: {date} is not a published scan")
    paths = wm.plan(date, dates, tuple(int(w) for w in widths.split(",")))
    if dry_run:
        for p in paths:
            print(p)
        return
    # Auth: an agent bearer token (`-t` / GCS_USAGE_TOKEN — the app gate), or a
    # Cloudflare Access service-token pair (CF_ACCESS_CLIENT_ID/SECRET — a
    # whole-host edge-gated deployment). Same request either way.
    token = secret(token, "GCS_USAGE_TOKEN")
    cid, csec = env_secret("CF_ACCESS_CLIENT_ID"), env_secret("CF_ACCESS_CLIENT_SECRET")
    if token:
        headers = {"Authorization": f"Bearer {token}"}
    elif cid and csec:
        headers = {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": csec}
    else:
        raise SystemExit("warm-cache: need GCS_USAGE_TOKEN (or -t), or CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET")
    res = wm.warm(site_url, headers, paths, jobs=jobs)
    bad = [r for r in res if r[1] != 200]
    err(f"warm-cache: {len(res) - len(bad)}/{len(res)} warmed for {date} in {sum(r[2] for r in res):.0f}s of request time" + (f"; {len(bad)} failed" if bad else ""))


@main.group()
def lifecycle() -> None:
    """Bucket lifecycle rules as a tracked file: `pull` (live → JSON), `diff`
    (file vs live), `push` (file → bucket, whole-config write + read-back
    verification), `gc-rule` (print the S3 bucket-wide noncurrent-version GC
    rule to add to a file). `gs://<bucket>` reads GCS (ADC: the job SA, or
    your gcloud application-default login); a bare name is S3 / CAIOS with the
    keys from the env (see `sweep`). Several `-b` → one JSON map keyed by
    bucket, the per-scan snapshot shape."""


def _lifecycle_clients(buckets: tuple[str, ...]):
    """The S3 and/or GCS client the given bucket URIs need (`None` for a cloud
    none of them name), so one `pull` can snapshot a mixed set."""
    from .lifecycle import is_gcs

    gcs = s3 = None
    if any(is_gcs(b) for b in buckets):
        from google.cloud import storage

        gcs = storage.Client()
    if any(not is_gcs(b) for b in buckets):
        from .sweep import s3_client

        s3 = s3_client()
    return s3, gcs


_LC_BUCKET = option("-b", "--bucket", "buckets", multiple=True, help="`gs://<bucket>` (GCS) or a bare S3 bucket name; repeatable (default $CW_BUCKET)")


def _lc_buckets(buckets: tuple[str, ...]) -> tuple[str, ...]:
    if buckets:
        return buckets
    from .sweep import CW_BUCKET

    return (CW_BUCKET,)


@lifecycle.command("pull")
@_LC_BUCKET
@option("-k", "--keep-going", is_flag=True, help="A bucket whose rules can't be read (e.g. no `storage.buckets.get`) is reported and left out, instead of failing the whole pull — for the job's fleet snapshot")
@option("-o", "--out", type=Path, help="Write here instead of stdout")
def lifecycle_pull(buckets: tuple[str, ...], keep_going: bool, out: Path | None) -> None:
    """One bucket → the bare rule list; several → `{<bucket>: rules}` in this order."""
    from .lifecycle import dump, dump_map, pull_any, pull_many

    buckets = _lc_buckets(buckets)
    s3, gcs = _lifecycle_clients(buckets)
    if len(buckets) == 1:
        text = dump(pull_any(buckets[0], s3=s3, gcs=gcs), bucket=buckets[0])
    else:
        text = dump_map(pull_many(list(buckets), s3=s3, gcs=gcs, keep_going=keep_going))
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text)
        err(f"lifecycle: {', '.join(buckets)} → {out}")


@lifecycle.command("diff")
@_LC_BUCKET
@argument("path", type=Path)
def lifecycle_diff(buckets: tuple[str, ...], path: Path) -> None:
    """Exit 1 when PATH (intended) differs from the live rules of the one -b bucket."""
    from .lifecycle import diff_any, load, pull_any

    (bucket,) = _lc_buckets(buckets)
    s3, gcs = _lifecycle_clients((bucket,))
    d = diff_any(bucket, load(str(path)), pull_any(bucket, s3=s3, gcs=gcs))
    print(json.dumps(d))
    if any(d.values()):
        sys.exit(1)


@lifecycle.command("push")
@_LC_BUCKET
@option("-n", "--dry-run", is_flag=True, help="Print the diff that would be applied; touch nothing")
@argument("path", type=Path)
def lifecycle_push(buckets: tuple[str, ...], dry_run: bool, path: Path) -> None:
    """Replace the one -b bucket's lifecycle configuration with PATH (read back + verified)."""
    from .lifecycle import diff_any, load, pull_any, push_any

    (bucket,) = _lc_buckets(buckets)
    s3, gcs = _lifecycle_clients((bucket,))
    intended = load(str(path))
    base = pull_any(bucket, s3=s3, gcs=gcs)
    d = diff_any(bucket, intended, base)
    if not any(d.values()):
        err(f"lifecycle: {bucket} already matches {path}")
        return
    err(f"lifecycle: {'would apply' if dry_run else 'applying'} to {bucket}: {json.dumps(d)}")
    if dry_run:
        return
    live = push_any(bucket, intended, base=base, s3=s3, gcs=gcs)  # refuses if live moved since the diff
    err(f"lifecycle: {bucket} now has {len(live)} rule(s), verified")


@lifecycle.command("gc-rule")
@option("-d", "--days", default=1, help="NoncurrentDays (1 while versioning is off; the undo window when it's on)")
@option("-p", "--prefix", default="", help="Scope (default: whole bucket)")
def lifecycle_gc_rule(days: int, prefix: str) -> None:
    from .lifecycle import gc_rule

    print(json.dumps(gc_rule(days, prefix), indent=2))


@main.group("plan-sweep")
def plan_sweep() -> None:
    """Plan-first deletion on CoreWeave: build manifests from a plan and execute them (boto3/CAIOS)."""


@plan_sweep.command("manifest")
@option("-d", "--date", required=True, help="Scan id (SNAP_ID) whose layer-2 parquet to pin")
@option("-l", "--l2", "l2_path", help="Layer-2 parquet path (default: /gcs/<data>/cw-l2/<date>/<bucket>.parquet)")
@option("-o", "--out", required=True, help="Output dir for manifest/ + plan-summary.json")
@argument("plan_path")
def plan_sweep_manifest(date: str, l2_path: str | None, out: str, plan_path: str) -> None:
    """Expand a curated PLAN (json) into an object-level deletion manifest.

    Deletes nothing; reads the pinned layer-2 parquet and writes
    manifest/<bucket>.parquet + plan-summary.json under --out."""
    import json

    from .sweep import DATA_BUCKET, build_manifest, load_plan

    plan = load_plan(plan_path)
    if l2_path is None:
        l2_path = f"/gcs/{DATA_BUCKET}/cw-l2/{date}/{plan.bucket}.parquet"
    summary = build_manifest(l2_path, plan, out)
    err(f"manifest: {summary['objects']} objects, {summary['bytes']} bytes -> {summary['manifest']}")
    print(json.dumps(summary))


@plan_sweep.command("execute")
@option("-G", "--no-versioning-guard", is_flag=True, help="Skip the versioning preflight: a real delete is then PERMANENT (no delete marker to undo)")
@option("-r", "--for-real", is_flag=True, help="Actually delete (writes recoverable delete markers); default is a dry run")
@argument("run_dir")
def plan_sweep_execute(no_versioning_guard: bool, for_real: bool, run_dir: str) -> None:
    """Execute the manifest under RUN_DIR against CoreWeave S3 (boto3).

    Default is a dry run (touches nothing). `--for-real` deletes reviewed keys
    whose (size, mtime) still match; refused unless the bucket has versioning
    Status=Enabled — `-G` disables that guard (deletes become permanent; the
    summary records `versioning_guard: false`)."""
    import json

    from .sweep import execute_plan

    s = execute_plan(run_dir, for_real=for_real, require_versioning=not no_versioning_guard)
    err(
        f"{'REAL' if for_real else 'DRY'}: {s['deleted_objects']} objs / {s['deleted_bytes']} bytes; "
        f"gone {s['skipped_gone']} overwritten {s['skipped_overwritten']} "
        f"drift {s['drift_new']} failed {s['delete_failed']}"
    )
    print(json.dumps(s))


@plan_sweep.command("undo")
@option("-n", "--dry-run", is_flag=True, help="Report what would be restored without touching anything")
@option("-p", "--prefix", "prefixes", multiple=True, help="Restrict undo to keys under this prefix (repeatable)")
@argument("run_dir")
def plan_sweep_undo(dry_run: bool, prefixes: tuple[str, ...], run_dir: str) -> None:
    """Undo a real run under RUN_DIR: remove its delete markers (recoverable
    delete). Must run before `purge`."""
    import json

    from .sweep import undo_run

    s = undo_run(run_dir, prefixes=list(prefixes) or None, dry_run=dry_run)
    err(f"{'DRY ' if dry_run else ''}undo: restored {s['restored']} (failed {s['restore_failed']}, skipped {s['skipped']})")
    print(json.dumps(s))


@plan_sweep.command("purge")
@option("-n", "--dry-run", is_flag=True, help="Report what would be purged without touching anything")
@argument("run_dir")
def plan_sweep_purge(dry_run: bool, run_dir: str) -> None:
    """Permanently drop every version of a real run's deleted keys under RUN_DIR
    — the irreversible space-reclaim stage, after the undo hold."""
    import json

    from .sweep import purge_run

    s = purge_run(run_dir, dry_run=dry_run)
    err(f"{'DRY ' if dry_run else ''}purge: {s['purged_versions']} versions / {s['purged_bytes']} bytes (failed {s['purge_failed']})")
    print(json.dumps(s))


@main.group()
def sweep() -> None:
    """The GCS executor's phases — manifest / execute / undo (specs/staged-delete.md)."""


@sweep.command("manifest")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets (default: every bucket the plan names)")
@option("-d", "--date", required=True, help="Scan date whose listing to plan from (pinned)")
@option("-o", "--out", default=None, help="Output dir (default gs://oa-gcs-usage-dvx/sweep/<date>-p<plan_id>)")
@option("-p", "--plan", "plan_path", required=True, help="The dispatched plan.json (path or gs:// URL): its items are the delete set, and the buckets are the plan's (∩ -b)")
@option("-r", "--root", default="gs://oa-gcs-usage-dvx", help="Listing root (gs:// or local mount)")
def sweep_manifest(only_buckets: tuple[str, ...], date: str, out: str | None, plan_path: str, root: str) -> None:
    """Object-level manifest of a staged plan (specs/staged-delete.md): stream
    the pinned listing and write per-bucket parquets of the ELIGIBLE keys —
    every key under a staged prefix — plus a category summary. The plan is
    the whole intent: nothing carves out. Pure read + artifact write —
    deletes nothing."""
    import fsspec
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .staged_plan import CATEGORIES, load_plan

    sp = load_plan(plan_path)
    buckets = [b for b in sp.buckets if not only_buckets or b in only_buckets]
    if not buckets:
        raise SystemExit(f"no plan bucket among -b {', '.join(only_buckets)} (plan {sp.plan_id} names {', '.join(sp.buckets)})")
    out = out or f"gs://oa-gcs-usage-dvx/sweep/{date}-p{sp.plan_id}"
    err(f"sweep manifest: scan {date} from plan {sp.plan_id} ({sp.name!r}) → {out}"
        + f" · {sum(len(sp.sweep[b]) for b in buckets)} staged prefix(es) on {', '.join(buckets)}")
    # The staged prefixes are the run's bands: `sweep execute` lists one
    # segment below each and accounts per band, so every `deletion_bands` row
    # is one staged item.
    summary: dict = {
        "date": date, "plan_id": sp.plan_id, "plan_name": sp.name,
        "approved": [a for b in buckets for a in sp.bands(b)],
        "buckets": {},
    }

    fs, rootpath = fsspec.core.url_to_fs(root)
    schema = pa.schema([
        ("name", pa.string()), ("size_bytes", pa.int64()),
        ("storage_class_id", pa.int8()), ("created", pa.timestamp("us", tz="UTC")),
        ("dir", pa.string()),
    ])
    for bucket in buckets:
        shards = sorted(fs.glob(f"{rootpath}/listing/{date}/{bucket}/*.parquet"))
        if not shards:
            raise SystemExit(f"no listing shards for {bucket} under {root}/listing/{date}/")
        cache: dict[str, str] = {}
        cats = {c: [0, 0] for c in CATEGORIES}  # bytes, objects
        bands = sp.sweep[bucket]
        writer = None
        out_path = f"{out}/manifest/{bucket}.parquet"
        ofs, opath = fsspec.core.url_to_fs(out_path)
        ofs.makedirs(opath.rsplit("/", 1)[0], exist_ok=True)
        n = 0
        for shard in shards:
          # Open/close each shard deterministically: a gcsfs file left for the
          # interpreter's exit to finalize calls into fsspec's event loop while
          # it is tearing down and can hang the process forever (observed
          # 2026-09-08: the manifest step's last line printed, then 0% CPU for
          # an hour and `sweep execute` never started).
          with fs.open(shard, "rb") as fh:
            pf = pq.ParquetFile(fh)
            for batch in pf.iter_batches(columns=["name", "size_bytes", "storage_class_id", "created"], batch_size=1 << 17):
                df = batch.to_pandas()
                n += len(df)
                inb = df["name"].str.startswith(bands)
                if not inb.all():
                    cats["outside_bands"][0] += int(df["size_bytes"][~inb].sum())
                    cats["outside_bands"][1] += int((~inb).sum())
                    df = df[inb]
                    if df.empty:
                        continue
                dirs = df["name"].str.rpartition("/")[0]
                for dn in dirs.unique():
                    if dn not in cache:
                        cache[dn] = sp.classify(bucket, dn)
                cat = dirs.map(lambda dn: cache[dn])
                sizes = df["size_bytes"]
                for c, g in sizes.groupby(cat):
                    cats[c][0] += int(g.sum())
                    cats[c][1] += len(g)
                elig = cat == "eligible"
                if elig.any():
                    sel = df[elig].copy()
                    sel["dir"] = dirs[elig]
                    t = pa.Table.from_pandas(sel, preserve_index=False).select(schema.names).cast(schema)
                    if writer is None:
                        writer = pq.ParquetWriter(opath, schema, filesystem=ofs)
                    writer.write_table(t)
        if writer is not None:
            writer.close()
        summary["buckets"][bucket] = {"objects": n, "dirs": len(cache), **{c: {"bytes": b, "objects": o} for c, (b, o) in cats.items() if o}}
        eb, eo = cats["eligible"]
        err(f"  {bucket}: {n:,} keys, {len(cache):,} dirs — eligible {eb / 1e12:.2f} TB / {eo:,} objects")

    tot = {c: [0, 0] for c in CATEGORIES}
    for b in summary["buckets"].values():
        for c in CATEGORIES:
            if c in b:
                tot[c][0] += b[c]["bytes"]
                tot[c][1] += b[c]["objects"]
    summary["total"] = {c: {"bytes": v[0], "objects": v[1]} for c, v in tot.items() if v[1]}
    with fsspec.open(f"{out}/plan-summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    err("\ntotals by category:")
    for c, (b, o) in tot.items():
        if o:
            err(f"  {c:16s} {b / 1e12:10.2f} TB  {o:>13,} objects")
    err(f"\nwrote {out}/plan-summary.json")
    _hard_exit()


@sweep.command("execute")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets")
@option("-D", "--drift", type=Choice(["skip", "proceed"]), default="skip", help="Dirs that gained new keys since the scan: skip (default) or proceed (manifest keys only — new keys always survive)")
@option("-w", "--workers", default=8, type=int, help="Concurrent directory re-lists")
@option("-W", "--delete-workers", default=32, type=int, help="Concurrent delete batches (100 objects each), shared by every re-list; the bucket's ~1000 writes/s is the ceiling")
@option("--for-real", is_flag=True, help="Actually delete (default: dry-run writes would-delete/)")
@option("--no-record", is_flag=True, help="Skip the D1 deletion_runs/bands record (recorded by default)")
@argument("plan_dir")
def sweep_execute(only_buckets: tuple[str, ...], drift: str, delete_workers: int, workers: int, for_real: bool, no_record: bool, plan_dir: str) -> None:
    """Execute (default: DRY-RUN) a `sweep manifest` plan: fresh re-list per
    eligible dir, generation-matched deletes of manifest∩live keys whose
    timeCreated is unchanged. The plan is the whole intent: nothing is
    re-classified. `--for-real` additionally requires ≥7d soft delete on every
    bucket."""
    import fsspec

    from .sweep_exec import DELETE_ATTEMPTS, execute_plan

    DELETE_ATTEMPTS_NOTE = f"{DELETE_ATTEMPTS} attempts"
    with fsspec.open(f"{plan_dir}/plan-summary.json") as fh:
        plan_summary = json.load(fh)
    err(f"execute {'FOR REAL' if for_real else '(dry-run)'} plan {plan_summary['plan_id']} ({plan_summary.get('plan_name')!r})")

    started = int(dt.datetime.now(dt.timezone.utc).timestamp())
    actor = os.environ.get("USER", "?")
    if not no_record:
        # The run's D1 row goes in now (finished NULL) so /staged lists it while
        # the re-list runs — hours, on the big bands; completed at the end.
        from .sweep_exec import record_run_start
        try:
            run_id = record_run_start(plan_summary, plan_dir, actor=actor, started_ts=started, for_real=for_real, buckets=only_buckets)
            err(f"recorded deletion run {run_id} (in progress)")
        except Exception as e:  # recording must never block the run
            err(f"WARN: deletion-run start record failed: {e}")
    # A clean stop: `sweep stop PLAN` drops PLAN/STOP (polled every 10 s), or
    # SIGTERM — roots not yet started are left for a re-run, everything done
    # is logged and recorded, and the job ends red (exit 130).
    import signal
    import threading
    from .sweep_exec import stop_file_watch
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop_file_watch(plan_dir, stop)
    summary = execute_plan(
        plan_dir,
        for_real=for_real,
        only_buckets=only_buckets,
        drift=drift,
        workers=workers,
        delete_workers=delete_workers,
        stop=stop,
    )
    finished = int(dt.datetime.now(dt.timezone.utc).timestamp())
    total = sum(b.get("delete_bytes", 0) for b in summary["buckets"].values())
    err(f"\n{'deleted' if for_real else 'would delete'}: {total / 1e12:.2f} TB total")
    failed = {b: v["failed_dirs"] for b, v in summary["buckets"].items() if v.get("failed_dirs")}
    if not no_record:
        from .sweep_exec import record_run
        # The undo deadline follows the narrowest window actually measured on
        # the run's buckets (the guard already refused anything under 7 d).
        windows = [int(v["soft_delete_days"]) for v in summary["buckets"].values() if "soft_delete_days" in v]
        try:
            run_id = record_run(summary, summary["_plan"], actor=actor, started_ts=started, finished_ts=finished, soft_delete_days=min(windows) if windows else 7)
            err(f"recorded deletion run {run_id}")
        except Exception as e:  # recording must never mask a completed run
            err(f"WARN: deletion-run record failed: {e}")
    if stop.is_set():
        skipped = sum(v.get("interrupted", {}).get("roots_skipped", 0) for v in summary["buckets"].values())
        err(f"STOPPED: {skipped:,} listing root(s) not started — re-run the plan to finish (done keys resolve as skipped_gone)")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    if failed:
        # Logged and recorded above; the job still ends red so nobody reads
        # "succeeded" over deletes GCS never answered for.
        n = sum(d["objects"] for ds in failed.values() for d in ds)
        err(f"ERROR: {n:,} delete(s) in {sum(map(len, failed.values())):,} dir(s) got no definitive answer after {DELETE_ATTEMPTS_NOTE} — see `failed_dirs` in the summary; a re-run settles them (already-gone → skipped_gone)")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(2)
    _hard_exit()


@sweep.command("stop")
@argument("plan_dir")
def sweep_stop(plan_dir: str) -> None:
    """Ask a running `sweep execute` on PLAN_DIR to stop cleanly: drops
    PLAN_DIR/STOP, which the executor polls every 10 s — roots not yet
    started are left for a re-run, everything done is logged and recorded."""
    import fsspec
    with fsspec.open(f"{plan_dir}/STOP", "w") as fh:
        fh.write(dt.datetime.now(dt.timezone.utc).isoformat())
    err(f"wrote {plan_dir}/STOP")
    _hard_exit()


@sweep.command("undo")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets")
@option("-n", "--dry-run", is_flag=True, help="List what would be restored; call nothing")
@option("-p", "--prefix", "prefixes", multiple=True, help="Only objects under these prefixes (gs://bucket/dir/); default: everything the run deleted")
@option("-w", "--workers", default=16, type=int, help="Concurrent restore calls")
@option("--no-record", is_flag=True, help="Skip the D1 undo_state / undone_objects update")
@argument("run")
def sweep_undo(only_buckets: tuple[str, ...], dry_run: bool, prefixes: tuple[str, ...], workers: int, no_record: bool, run: str) -> None:
    """Restore what a real run deleted, from its `deleted/` logs — the
    soft-delete restore of exactly the logged generations, valid until the
    run's `undo_deadline` (finish + the buckets' 7-day window). RUN is the D1
    run id (`<scan>-p<plan_id>/<utc stamp>`, as /staged lists it) or the run's
    gs:// log dir. Re-runnable: names already live again are left alone."""
    from .index_footer import _creds, _d1_query, _q
    from .sweep_exec import record_undo, undo_run

    row = None
    try:
        tok, acct = _creds()
        rows = _d1_query(
            "SELECT run_id, mode, undo_deadline, undo_state, log_dir, deleted_objects FROM deletion_runs "
            f"WHERE run_id = {_q(run)} OR log_dir = {_q(run)}", acct, tok,
        )
        row = rows[0] if rows else None
    except Exception as e:
        if not run.startswith("gs://"):
            raise SystemExit(f"D1 lookup failed and RUN is not a gs:// log dir: {e}")
        err(f"WARN: D1 lookup failed ({e}); proceeding on the log dir alone (no deadline check, no record)")
    if row is None and not run.startswith("gs://"):
        raise SystemExit(f"no deletion run {run!r} in D1 (see /staged for run ids)")
    if row is not None and row["mode"] != "real":
        raise SystemExit(f"{row['run_id']} was a dry run — nothing to undo")
    log_dir = row["log_dir"] if row is not None else run
    deadline = row.get("undo_deadline") if row is not None else None
    if row is not None:
        err(f"undo {row['run_id']}: {row['deleted_objects']:,} deleted objects, undo_state={row['undo_state']}, "
            f"window until {dt.datetime.fromtimestamp(deadline, dt.timezone.utc):%Y-%m-%d %H:%MZ}" if deadline else f"undo {row['run_id']}")
    summary = undo_run(log_dir, only_buckets=only_buckets, prefixes=prefixes, dry_run=dry_run, workers=workers, deadline=deadline)
    if row is not None and not no_record and not dry_run:
        try:
            err(f"recorded undo_state={record_undo(row['run_id'], summary, int(row['deleted_objects'] or 0))}")
        except Exception as e:  # recording must never mask a completed undo
            err(f"WARN: undo record failed: {e}")
    _hard_exit()


@main.group()
def access() -> None:
    """GCS usage-log (access-log) ingest — layer-1a/2a parquet + watermarks."""


@access.command("ingest")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: the marin fleet)")
@option("-c", "--max-chunk-gb", default=32.0, help="Max staged CSV bytes per processing chunk")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="Output/state bucket")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: marin-usage-logs)")
@option("-M", "--memory-limit", default=None, help="DuckDB memory limit (default: $DUCKDB_MEM or 8GB)")
@option("-n", "--max-chunks", default=None, type=int, help="Stop after N chunks per bucket (smoke runs)")
@option("-s", "--stage-dir", type=Path, default=None, help="Local staging dir (default: $STAGE_DIR or /tmp, + /access-stage)")
@option("-w", "--workers", default=16, help="Concurrent CSV downloads")
def access_ingest(
    buckets: tuple[str, ...],
    max_chunk_gb: float,
    data_bucket: str,
    log_bucket: str | None,
    memory_limit: str | None,
    max_chunks: int | None,
    stage_dir: Path | None,
    workers: int,
) -> None:
    """Incrementally ingest new usage CSVs → layer-1a/2a parquet in the data bucket."""
    from .access import FLEET, USAGE_LOG_BUCKET, ingest

    ingest(
        buckets=buckets or FLEET,
        log_bucket=log_bucket or USAGE_LOG_BUCKET,
        data_bucket=data_bucket,
        stage_dir=(stage_dir or Path(os.environ.get("STAGE_DIR") or "/tmp") / "access-stage"),
        memory_limit=memory_limit or os.environ.get("DUCKDB_MEM_ACCESS") or "8GB",
        max_chunk_gb=max_chunk_gb,
        workers=workers,
        max_chunks=max_chunks,
    )


@access.command("sweep")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: the marin fleet)")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="State bucket holding the watermarks")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: marin-usage-logs)")
@option("-T", "--through-watermark", is_flag=True, help="Sweep through the watermark itself, not watermark − lag")
@option("-w", "--workers", default=16, help="Concurrent copy+delete pairs")
def access_sweep(
    buckets: tuple[str, ...],
    data_bucket: str,
    log_bucket: str | None,
    through_watermark: bool,
    workers: int,
) -> None:
    """Move already-ingested CSVs out of `usage/` into `ingested/` (7d TTL).

    The polled `ingest` does this itself at the end of each run, but only up to
    watermark − 6h, leaving the lag window in place for late deliveries.

    `-T` sweeps the lag window too, which is the one-shot cutover step to the
    list-based drain: the drain treats anything left in `usage/` as un-ingested,
    so the polled path's residue has to be cleared first. Run it only after the
    polled ingest has been switched off for good.
    """
    from google.cloud import storage

    from .access import FLEET, LAG_HOURS, USAGE_LOG_BUCKET, load_state, sweep_ingested

    client = storage.Client()
    total = 0
    for b in buckets or FLEET:
        state = load_state(client, data_bucket, b)
        total += sweep_ingested(
            client, log_bucket or USAGE_LOG_BUCKET, b, state.get("watermark"),
            workers=workers, lag_hours=0 if through_watermark else LAG_HOURS,
        )
    err(f"sweep: {total} CSV(s) moved to ingested/")


@access.command("status")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: the marin fleet)")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="Output/state bucket")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: marin-usage-logs)")
def access_status(buckets: tuple[str, ...], data_bucket: str, log_bucket: str | None) -> None:
    """Per-bucket watermark vs delivered backlog (files/bytes awaiting ingest)."""
    from google.cloud import storage

    from .access import FLEET, USAGE_LOG_BUCKET, list_new, load_state

    client = storage.Client()
    for b in buckets or FLEET:
        state = load_state(client, data_bucket, b)
        todo = list_new(client, log_bucket or USAGE_LOG_BUCKET, b, state)
        n_bytes = sum(s for _, s in todo)
        print(
            f"{b:22s}  watermark={state.get('watermark') or '(none)'}  "
            f"backlog={len(todo)} files / {n_bytes / 1e9:.1f} GB"
        )


@main.group()
def job() -> None:
    """Read-only ops for the daily snapshot Batch job (status/logs/watch/metrics)."""


def _resolve_job(name: str) -> dict:
    from .gcp import batch_job, batch_jobs

    if name in ("", "latest"):
        jobs = batch_jobs()
        if not jobs:
            raise SystemExit("no Batch jobs found")
        return jobs[0]
    return batch_job(name)


@job.command("status")
@option("-n", "--limit", default=8, help="Jobs to list")
@argument("name", required=False)
def job_status(limit: int, name: str | None) -> None:
    """List recent Batch jobs, or one job's state + status events."""
    import json

    from .gcp import batch_jobs

    if name is None:
        for j in batch_jobs()[:limit]:
            print(f"{j['name'].rsplit('/', 1)[-1]}  {j['status'].get('state', '?'):22} {j.get('createTime', '')}")
        return
    j = _resolve_job(name)
    print(f"{j['name'].rsplit('/', 1)[-1]}  {j['status'].get('state', '?')}  uid={j.get('uid')}")
    for e in j["status"].get("statusEvents", []):
        print(f"  {e.get('eventTime', '')[11:19]} {e.get('type', ''):16} {e.get('description', '')[:200]}")
    if rund := j["status"].get("runDuration"):
        print(f"  runDuration: {rund}")
    env = j["taskGroups"][0]["taskSpec"].get("environment", {}).get("variables", {})
    print(f"  env: {json.dumps(env)}")


@job.command("logs")
@option("-a", "--asc", is_flag=True, help="Oldest first (default: newest first)")
@option("-g", "--grep", default=None, help="Regex filter on textPayload (server-side)")
@option("-k", "--key-markers", is_flag=True, help="Only [rss]/stage/WARN/DONE/error marker lines")
@option("-n", "--limit", default=40, help="Max entries")
@argument("name", required=False)
def job_logs(asc: bool, grep: str | None, key_markers: bool, limit: int, name: str | None) -> None:
    """Container stdout for a Batch job (batch_task_logs; agent noise excluded)."""
    from .gcp import log_entries, task_log_filter

    j = _resolve_job(name or "latest")
    if key_markers:
        grep = r"\[rss\]|stage |WARN|SNAPSHOT-JOB-DONE|Deployment complete|reusing|objects listed|Error|Killed|Traceback"
    for e in log_entries(task_log_filter(j["uid"], grep), limit=limit, asc=asc):
        print(f"{e.get('timestamp', '')[:19]} {e.get('textPayload', '').rstrip()}")


@job.command("watch")
@option("-i", "--interval", default=90, help="Poll interval (seconds)")
@argument("name", required=False)
def job_watch(interval: int, name: str | None) -> None:
    """Poll a Batch job to terminal state, then print its key log markers."""
    import time
    from datetime import datetime, timezone

    from click import Context

    j = _resolve_job(name or "latest")
    short = j["name"].rsplit("/", 1)[-1]
    err(f"watching {short} (uid={j['uid']})")
    while True:
        state = _resolve_job(short)["status"].get("state", "?")
        err(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {state}")
        if state in ("SUCCEEDED", "FAILED", "DELETION_IN_PROGRESS"):
            break
        time.sleep(interval)
    ctx = Context(job_logs)
    ctx.invoke(job_logs, asc=True, grep=None, key_markers=True, limit=60, name=short)
    if state != "SUCCEEDED":
        raise SystemExit(1)


@job.command("submit-listing")
@option("-b", "--bucket", "buckets", multiple=True, help="Bucket(s) to list [default: whole fleet]")
@option("-d", "--date", "date", required=True, help="Listing date — output goes to listing/<date>/<bucket>/")
@option("-m", "--machine", default="n2-standard-32", help="Machine type per task")
@option("-P", "--procs", default=24, help="bulk-list worker processes per task")
@option("-w", "--workers", "threads", default=10, help="Concurrent prefix streams per process")
@option("-W", "--wait", "wait", is_flag=True, help="Block until the job reaches a terminal state")
def job_submit_listing(
    buckets: tuple[str, ...],
    date: str,
    machine: str,
    procs: int,
    threads: int,
    wait: bool,
) -> None:
    """Submit the DIY fleet-listing Batch job (one task per bucket).

    Tasks reuse completed listings (``-x reuse``), so re-submitting for the
    same date only re-lists buckets that haven't finished — safe to retry.
    """
    from .batch import BUCKET_JOB_REGIONS, FLEET_BUCKETS, REGION, listing_job_spec, submit_job, wait_jobs

    bkts = list(buckets) or FLEET_BUCKETS
    by_region: dict[str, list[str]] = {}
    for b in bkts:
        by_region.setdefault(BUCKET_JOB_REGIONS.get(b, REGION), []).append(b)
    jobs = []
    for region, rb in by_region.items():
        spec = listing_job_spec(date, rb, machine=machine, procs=procs, threads=threads, region=region)
        name = submit_job(spec, region=region)
        err(f"submitted {name} [{region}]: {len(rb)} bucket task(s) on {machine}")
        print(name)
        jobs.append((name, region))
    if wait:
        states = wait_jobs(jobs, log=err)
        if bad := {n: s for n, s in states.items() if s != "SUCCEEDED"}:
            raise SystemExit(f"listing job(s) failed: {bad}")


@job.command("metrics")
@option("-m", "--metric", type=Choice(["cpu", "net", "disk"]), default="cpu", help="Metric to show")
@option("-n", "--minutes", default=30, help="Lookback window")
@argument("name", required=False)
def job_metrics(metric: str, minutes: int, name: str | None) -> None:
    """VM utilization for a Batch job (finds the instance via agent logs)."""
    from .gcp import METRICS, job_instance_id, vm_metric

    j = _resolve_job(name or "latest")
    inst = job_instance_id(j["uid"])
    if not inst:
        raise SystemExit(f"no instance found in agent logs for {j['uid']} (job not started yet?)")
    unit = METRICS[metric][2]
    terminal = j["status"].get("state") in ("SUCCEEDED", "FAILED")
    span = dict(start=j.get("createTime"), end=j.get("updateTime")) if terminal else {}
    for t, v in vm_metric(inst, metric, minutes, **span):
        print(f"{t[:19]} {v:8.1f} {unit}")


@main.group()
def sii() -> None:
    """Read-only Storage Insights inventory-report ops."""


SII_BUCKETS = ["marin-us-east1", "marin-us-east5", "marin-us-central1", "marin-eu-west4", "marin-us-west4"]


@sii.command("status")
@option("-b", "--bucket", "buckets", multiple=True, help="Bucket(s) to check [default: all 5 SII buckets]")
def sii_status(buckets: tuple[str, ...]) -> None:
    """Per-bucket SII health: report config, latest generated report, and which
    days' shards have actually landed in gs://<bucket>/inventory-reports/."""
    import re as _re
    from collections import defaultdict

    from google.cloud import storage

    from .gcp import sii_report_configs, sii_report_details

    client = storage.Client()
    for b in buckets or SII_BUCKETS:
        location = b.removeprefix("marin-")
        print(f"== {b}")
        cfgs = [
            c
            for c in sii_report_configs(location)
            if c.get("objectMetadataReportOptions", {}).get("storageFilters", {}).get("bucket") == b
        ]
        if not cfgs:
            print("  NO report config")
            continue
        for c in cfgs:
            details = sii_report_details(c["name"])
            freq = c.get("frequencyOptions", {}).get("frequency", "?")
            print(f"  config {c['name'].rsplit('/', 1)[-1][:8]}… ({freq}); {len(details)} reports generated")
            for r in details[:2]:
                m = r.get("reportMetrics", {})
                print(
                    f"    {r.get('snapshotTime', '')[:16]} records={int(m.get('processedRecordsCount', 0)):,}"
                    f" shards={r.get('shardsCount', '?')}"
                )
        by_day: dict[str, list] = defaultdict(list)
        for blob in client.list_blobs(b, prefix="inventory-reports/"):
            if blob.name.endswith(".parquet") and (m := _re.search(r"_(\d{4}-\d{2}-\d{2})T", blob.name)):
                by_day[m.group(1)].append(blob)
        for day in sorted(by_day, reverse=True)[:3]:
            blobs = by_day[day]
            latest = max(x.time_created for x in blobs)
            print(f"    landed {day}: {len(blobs)} shards ({sum(x.size for x in blobs) / 1e9:.1f} GB, written {latest:%m-%d %H:%M}Z)")


@main.command("export")
@option("-d", "--date", default=None, help="Scan date YYYY-MM-DD[THHMM] (default: the newest in the store's scans.json)")
@option("-e", "--executor", default=None, type=Choice(["sweep", "plan-sweep"]), help="`runs` only: the site's executor route family (`Store.executor`: gcs `sweep`, cw `plan-sweep`)")
@option("-l", "--list", "list_sources", is_flag=True, help="Print the sources and their columns, and exit")
@option("-o", "--out", default="-", help="CSV output path (default: stdout)")
@option("-s", "--subdir", default=None, help="Snapshot subdir under /data/ for scans.json (default: $SNAPSHOTS_SUBDIR; `cw` on cw-s3)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {SITE_DEFAULT_URL})")
@option("-U", "--unit", default="B", type=Choice(["B", "GiB", "TiB"]), help="Byte columns as raw bytes (default) or rounded GiB / TiB, header `<col> (<unit>)`")
@argument("source", required=False)
def export_cmd(date: str | None, executor: str | None, list_sources: bool, out: str, subdir: str | None, token: str | None, url: str | None, unit: str, source: str | None) -> None:
    """Export one named SOURCE from the live site API as a CSV with a fixed
    column contract (`--list` shows them) — the input `sheet-push -k` mirrors
    into a Google Sheet tab. See specs/done/sheet-mirror.md."""
    from .sheet_mirror import ExportArgs, export, list_sources as sources_lines, write_csv  # noqa: PLC0415
    from .site import creds, get_json  # noqa: PLC0415

    if list_sources:
        print("\n".join(sources_lines()))
        return
    if not source:
        raise SystemExit("export: SOURCE required (see `dt-cloud export --list`)")
    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("export: no token (-t or $GCS_USAGE_TOKEN)")
    args = ExportArgs(date=date, executor=executor, subdir=subdir if subdir is not None else (env_secret("SNAPSHOTS_SUBDIR") or ""), unit=unit)
    columns, rows = export(source, lambda path, params: get_json(base, tok, path, params), args)
    if out == "-":
        write_csv(columns, rows, sys.stdout)
    else:
        with open(out, "w", newline="") as fh:
            write_csv(columns, rows, fh)
    err(f"{source}: {len(rows)} rows → {out}")


@main.command("sheet-push")
@option("-c", "--create", is_flag=True, help="create the tab if the sheet has none by that title (header row frozen + bold, columns sized to the first fill)")
@option("-D", "--disclaimer", help="static footer text 2 rows below the table; a '; last change <ts>' stamp is appended that only advances when data changes")
@option("-I", "--impersonate", help="service-account email to impersonate for Sheets auth (needs Token Creator); default is ambient ADC")
@option("-k", "--key", default=None, help="stable row identity column: existing rows keep their order, new keys append, removed keys clear (compacted on an otherwise-unchanged run); default positional")
@option("-n", "--dry-run", is_flag=True, help="parse + summarize, don't touch the sheet")
@option("-w", "--worksheet", required=True, help="tab to sync, by title (never the first tab by default: the sheet may hold human-authored tabs)")
@argument("sheet_id")
@argument("csv_path", default="-")
def sheet_push(create: bool, disclaimer: str | None, impersonate: str | None, key: str | None, dry_run: bool, worksheet: str, sheet_id: str, csv_path: str) -> None:
    """Push a CSV (header + rows, e.g. from `export`) into one named tab of a
    Google Sheet — the generic CSV → tab writer behind the sheet mirror.

    Syncs ONE named tab in place (`-w <title>`); other tabs (derived views
    people add) are untouched. Writes only the cells whose value actually
    changed (diffing the tab's current contents, numerically where possible),
    so Google's Version History highlights just the real deltas — and
    formatting / frozen rows survive. With `-k <column>` the diff is by key,
    not position: an added row is one new row at the end, a removed row one
    cleared row (holes are compacted on a later run whose data is otherwise
    unchanged). `-D` writes an "auto-synced" footer two rows below the table,
    whose "last change" stamp only advances when data moves — so a no-op run
    writes nothing. Idempotent.

    Auth is Application Default Credentials: the job's GCP service account in
    Cloud Run, or your `gcloud auth application-default` locally. The sheet
    must be shared (Editor) with that identity, and the Sheets API enabled in
    the project. `-c` creates a missing tab (appended last, header frozen).

        dt-cloud export owners -o owners.csv && dt-cloud sheet-push -k user -w 'Storage by user' <id> owners.csv
    """
    import csv
    import datetime
    import io as _io

    from .sheet_mirror import plan_sheet, push  # noqa: PLC0415

    text = sys.stdin.read() if csv_path == "-" else Path(csv_path).read_text()
    rows = [r for r in csv.reader(_io.StringIO(text)) if r]
    if not rows:
        raise SystemExit("expected a CSV with at least a header row, got nothing")
    if key and key not in rows[0]:
        raise SystemExit(f"-k {key!r} is not a column of {rows[0]}")
    err(f"{len(rows) - 1} rows → sheet {sheet_id} tab '{worksheet}'{f' by {key!r}' if key else ''}")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if dry_run:
        plan_sheet([], rows, now, key=key, disclaimer=disclaimer)  # validates keys (dupes/empties)
        err("dry-run — not writing")
        return

    import google.auth  # noqa: PLC0415 — optional (sheets extra), imported on use
    import gspread  # noqa: PLC0415

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if impersonate:
        from google.auth import impersonated_credentials  # noqa: PLC0415
        source, _ = google.auth.default()
        creds = impersonated_credentials.Credentials(
            source_credentials=source, target_principal=impersonate, target_scopes=scopes,
        )
    else:
        creds, _ = google.auth.default(scopes=scopes)
    sheet = gspread.authorize(creds).open_by_key(sheet_id)
    created = False
    try:
        ws = sheet.worksheet(worksheet)
    except gspread.WorksheetNotFound:
        if not create:
            raise SystemExit(f"sheet {sheet_id} has no tab {worksheet!r} (-c creates it)") from None
        ws = sheet.add_worksheet(worksheet, rows=len(rows) + 10, cols=len(rows[0]))
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
        created = True
        err(f"created tab '{worksheet}'")
    plan = push(ws, rows, now, key=key, disclaimer=disclaimer, cell=gspread.Cell)
    if created:
        # Width from the table only (auto-resize would stretch column A to the
        # footer); set once, so later width tweaks by people stick.
        sheet.batch_update({"requests": [
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": 7 * max(len(r[i]) for r in rows if i < len(r)) + 24},
                "fields": "pixelSize",
            }}
            for i in range(len(rows[0]))
        ]})
    verb = "changed" if plan.data_changed else ("unchanged, compacted" if plan.compacted else "unchanged")
    holes = f", {plan.holes} cleared row(s) held for compaction" if plan.holes else ""
    err(f"synced '{ws.title}': {len(plan.cells)} cell(s) written ({plan.data_rows} data rows, data {verb}{holes})")


@main.group("sheet-mirror")
def sheet_mirror() -> None:
    """A deployment's `sheet-mirror.yml` → what `deploy/sheet-mirror/` runs."""


@sheet_mirror.command("env")
@argument("config")
def sheet_mirror_env(config: str) -> None:
    """Print the deploy variables (SITE, TOKEN_SECRET, SCHEDULE, PROJECT,
    REGION, SA, JOB, TRIGGER, IMAGE) as shell-quoted `KEY=value` lines, for
    `build.sh` / `deploy.sh` to `eval`. CONFIG is a path, or `-` for stdin."""
    from .sheet_mirror import env_lines, read_config  # noqa: PLC0415

    print("\n".join(env_lines(read_config(config))))


@sheet_mirror.command("render")
@argument("config")
def sheet_mirror_render(config: str) -> None:
    """Print CONFIG with every `${NAME}` substituted from the environment, as
    YAML — validated first; an unset NAME is an error. `deploy.sh` bakes this
    into the job, so an id kept out of the repo (e.g. `sheet: ${GCS_SHEET_ID}`,
    set in an untracked `.envrc`) reaches the job but never git."""
    from .sheet_mirror import render_config  # noqa: PLC0415
    print(render_config(sys.stdin.read() if config == "-" else Path(config).read_text()), end="")


@sheet_mirror.command("plan")
@argument("config")
def sheet_mirror_plan(config: str) -> None:
    """Validate CONFIG and print one line per mirror — shell-quoted
    `source= site= subdir= sheet= tab= key= footer= executor= unit=` assignments —
    for `sync.sh` to `eval` in its loop. CONFIG is a path, or `-` for stdin."""
    from .sheet_mirror import plan_lines, read_config  # noqa: PLC0415

    print("\n".join(plan_lines(read_config(config))))


@main.command("cascade-a2a")
@option("-b", "--bucket", required=True, help="Bucket the DT tier was imported as (its rows are relative to it)")
@option("-i", "--index", "index_path", required=True, help="mgu floor-free path-index parquet (`path, depth, usr, b, o, wts, wb, c2, c3, c4, a`)")
@option("-j", "--json", "as_json", is_flag=True, help="Machine-readable report on stdout")
@option("-n", "--top", default=10, help="Examples per mismatch class")
@argument("dirs_tier")
def cascade_a2a(bucket: str, index_path: str, as_json: bool, top: int, dirs_tier: str) -> None:
    """The A.3 gate (spec mgu-scale-unification.md): DT's `import -e duckdb
    --label usr` dirs tier against mgu's path index for one bucket, joined on
    `(path, usr)` — rows only one side has, and per-column disagreements
    (`b`↔`size`, `o`↔`n_files`, `c2..c4`↔`sum_storage_class_id_*`,
    `wts/wb`↔`mtime_mean`). Exit 1 on any difference."""
    from .cascade_a2a import compare, render

    report = compare(bucket, index_path, dirs_tier, top)
    print(json.dumps(report, indent=1, default=str) if as_json else render(report))
    if not report["ok"]:
        raise SystemExit(1)


def _cw_icons_dir() -> Path:
    """`job/icons-cw` in both layouts: pip-installed in the job image (cwd=/app →
    /app/job/icons-cw) or the repo checkout (…/parents[3]/job/icons-cw)."""
    cands = (Path.cwd() / "job" / "icons-cw", Path(__file__).resolve().parents[3] / "job" / "icons-cw")
    return next((c for c in cands if c.exists()), cands[-1])


@main.command("cw-digest")
@option("-c", "--channel", help="Slack channel id (default $SLACK_CHANNEL)")
@option("-D", "--reply-delay", "reply_delay", default=0.0, type=float, help="Seconds to sleep between replies (e.g. 305 for a spaced backfill so per-reply sender chrome survives)")
@option("-F", "--for-real", is_flag=True, help="With --redo-replies: actually post the new replies and delete the old ones (default: print the plan)")
@option("-H", "--reply-hour", type=int, default=REPLY_HOUR_UTC, help="UTC hour the sender variant's daily reply is taken from: the day's first scan at/after it (default 12 → the 12:01Z morning scan, 8:01 am ET; 00:01Z scans still feed the OP + plot)")
@option("-i", "--icons-dir", type=Path, default=None, help="Where the plot PNG is written + deployed from (default job/icons-cw)")
@option("-m", "--month", help="Month YYYY-MM (default: current UTC month)")
@option("-n", "--dry-run", is_flag=True, help="Render the plot + print OP/replies; post & host nothing")
@option("-r", "--root", help="Snapshots root (default gs://$DATA_BUCKET/snapshots/cw)")
@option("-t", "--token", help="Slack bot token (default $SLACK_BOT_TOKEN)")
@option("-u", "--url", "site_url", default=None, help="Site base for links (default cw-s3.oa.dev)")
@option("-R", "--redo-replies", is_flag=True, help="Re-post the month's replies under the current day rule, then delete the old ones (dry-run unless --for-real)")
@option("-V", "--variant", type=Choice(["sender", "body"]), default="sender", help="Reply style: headline as the sender name, posted once from the day's morning scan (sender) or bold in the body, edited as the day's scans land (body)")
def cw_digest(channel: str | None, reply_delay: float, for_real: bool, reply_hour: int, icons_dir: Path | None, month: str | None, dry_run: bool, redo_replies: bool, root: str | None, token: str | None, site_url: str | None, variant: str) -> None:
    """Converge the monthly digest thread in #cw-s3-usage: an OP edited in place
    (month-to-date + weekly bullets + quota sparkline) + one reply per UTC day,
    via thrds. State in gs://<bucket>/digest/cw/<channel>/<variant>/<YYYY-MM>.json.
    See specs/cw-slack-digest.md."""
    from . import cw_digest as dg

    site_url = site_url or dg.DEFAULT_URL
    m = (
        dt.datetime.strptime(month, "%Y-%m").date()
        if month
        else dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    )
    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots/cw"

    if dry_run:
        month = dg.load_month(root, m)
        if month is None:
            raise SystemExit(f"digest: no scans for {m:%Y-%m}")
        import tempfile

        out = Path(tempfile.gettempdir()) / f"cw-digest-{m:%Y%m}.png"
        dg.render_plot(month, m, out, root)
        err(f"rendered plot → {out}")
        print(dg.op_body(month, m, "<plot-url>", site_url))
        print(f"\n--- replies ({variant}: username | body | icon) ---")
        for day in dg.day_rows(month, variant, reply_hour):
            r = dg.reply(day, variant, site_url)
            print(f"{r.username} | {r.body} | {(r.icon_url or r.icon_emoji or '').split('/')[-1]}")
        return

    channel = channel or os.environ.get("SLACK_CHANNEL")
    token = secret(token, "SLACK_BOT_TOKEN")
    if not (channel and token):
        raise SystemExit("digest: need SLACK_BOT_TOKEN + SLACK_CHANNEL (or -t/-c)")
    icons = icons_dir or _cw_icons_dir()

    def deploy(local: Path, name: str) -> str | None:
        # publish the cw icons dir (the CORS _headers + the fresh plot) to the
        # icons Pages project's `cw` preview branch — never its production
        # branch, whose root alias serves the arrow avatars both digests use.
        # Return the deployment-specific URL (served instantly), which the OP
        # image uses to avoid racing alias propagation (→ Slack invalid_blocks).
        import re
        import shutil
        import subprocess

        # The job image installs wrangler globally (`npm install -g`) but has
        # no `npx` shim, so prefer the binary; `npx` only serves a laptop run.
        wrangler = [shutil.which("wrangler")] if shutil.which("wrangler") else ["npx", "wrangler"] if shutil.which("npx") else None
        if wrangler is None:
            raise SystemExit("digest: neither `wrangler` nor `npx` on PATH — can't publish the plot")
        r = subprocess.run(
            [*wrangler, "pages", "deploy", str(icons), "--project-name", dg.ICONS_PROJECT, "--branch", dg.ICONS_BRANCH, "--commit-dirty=true"],
            check=True, capture_output=True, text=True,
        )
        err(r.stdout)
        found = re.search(r"https://[a-z0-9]+\.gcs-usage-icons\.pages\.dev", r.stdout + r.stderr)
        return found.group(0) if found else None

    if redo_replies:
        # rule change: re-post every reply under the current day rule, then retire the old ones
        plan = dg.redo_replies(root, m, token, channel, variant, site_url=site_url, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay, reply_hour=reply_hour, for_real=for_real)
        if for_real:
            err(f"digest: re-threaded {m:%Y-%m} ({variant}): {len(plan.get('posted', {}))} replies" + (f", {len(plan['stale'])} old left undeleted" if plan.get("stale") else ""))
            return
        old = {day: e for day, e in plan["old"]}
        print(f"digest --redo-replies {m:%Y-%m} in {channel} ({variant}; dry-run — -F/--for-real applies):")
        print(f"  old replies to delete: {len(plan['old'])}")
        for day, e in plan["old"]:
            print(f"    {day}  {e['scan']}  ts={e['ts']}")
        print(f"  new replies to post: {len(plan['new'])}")
        for day, scan, head in plan["new"]:
            same = "  (same scan as the old reply)" if day in old and old[day]["scan"] == scan else ""
            print(f"    {day}  {scan}  {head!r}{same}")
        return
    dg.post_digest(root, m, token, channel, variant, site_url=site_url, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay, reply_hour=reply_hour)
    err(f"digest: converged {m:%Y-%m} ({variant})")


if __name__ == "__main__":
    main()


def _icons_dir() -> Path:
    """`job/icons` in both layouts: pip-installed in the job image (cwd=/app →
    /app/job/icons) or the repo checkout (…/parents[3]/job/icons)."""
    cands = (Path.cwd() / "job" / "icons", Path(__file__).resolve().parents[3] / "job" / "icons")
    return next((c for c in cands if c.exists()), cands[-1])


@main.command()
@option("-b", "--bot-token", help="Discord bot token: opens the month's thread + resolves app emoji (default $DISCORD_BOT_TOKEN; with -P discord)")
@option("-c", "--channel", help="Slack channel id (default $SLACK_CHANNEL)")
@option("-D", "--reply-delay", "reply_delay", default=0.0, type=float, help="Seconds to sleep between replies (e.g. 305 for a spaced Slack backfill so per-reply sender chrome survives; Discord needs none)")
@option("-E", "--edit-replies", is_flag=True, help="Re-edit every already-posted reply to its current body (backfill after a format change; -P discord only)")
@option("-m", "--month", help="Month YYYY-MM (default: current UTC month)")
@option("-n", "--dry-run", is_flag=True, help="Render the plot + print OP/replies; post & host nothing")
@option("-P", "--platform", type=Choice(["slack", "discord"]), default="slack", help="Which twin to converge (default slack)")
@option("-r", "--root", help="Snapshots root (default gs://$DATA_BUCKET/snapshots)")
@option("-t", "--token", help="Slack bot token (default $SLACK_BOT_TOKEN)")
@option("-u", "--url", "site_url", default=None, help="Site base for links (default gcs.oa.dev)")
@option("-w", "--webhook", help="Discord webhook URL in the digest channel (default $DISCORD_GCS_USAGE_WEBHOOK; with -P discord)")
def digest(bot_token: str | None, channel: str | None, reply_delay: float, edit_replies: bool, month: str | None, dry_run: bool, platform: str, root: str | None, token: str | None, site_url: str | None, webhook: str | None) -> None:
    """Converge the Shape-C monthly digest thread: an OP (month-to-date headline,
    per-week bullets, mosaic plot) edited in place + one reply per scan (headline
    sender, $/mo body, colour-coded arrow avatar). Slack (default; state in
    gs://<bucket>/digest/<YYYY-MM>.json) or its Discord twin (`-P discord`: webhook
    OP with the plot attached, bot-opened thread, per-scan webhook replies; state
    in digest/discord/<channel>/<YYYY-MM>.json). See specs/done/slack-digest-shape-c.md."""
    from . import digest as dg

    site_url = site_url or dg.DEFAULT_URL
    m = (
        dt.datetime.strptime(month, "%Y-%m").date()
        if month
        else dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    )
    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots"

    if dry_run:
        rows = dg.load_month(root, m)
        if not rows:
            raise SystemExit(f"digest: no scans for {m:%Y-%m}")
        import tempfile

        out = Path(tempfile.gettempdir()) / f"digest-{m:%Y%m}.png"
        dg.render_plot(rows, m, out)
        err(f"rendered plot → {out}")
        print(dg.op_body(rows, m, "<plot-url>", site_url))
        print("\n--- replies (sender | body | avatar) ---")
        for r in rows:
            s, b, a = dg.reply(r, site_url)
            print(f"{s} | {b} | {a.split('/')[-1]}")
        return

    if platform == "discord":
        webhook = secret(webhook, "DISCORD_GCS_USAGE_WEBHOOK")
        bot_token = secret(bot_token, "DISCORD_BOT_TOKEN")
        if not (webhook and bot_token):
            raise SystemExit("digest: -P discord needs DISCORD_GCS_USAGE_WEBHOOK + DISCORD_BOT_TOKEN (or -w/-b)")
        dg.post_digest_discord(root, m, webhook, bot_token, site_url=site_url, edit_replies=edit_replies)
        err(f"digest: converged {m:%Y-%m} (discord)")
        return
    if edit_replies:
        raise SystemExit("digest: -E/--edit-replies is Discord-only (Slack replies are never edited)")
    channel = channel or os.environ.get("SLACK_CHANNEL")
    token = secret(token, "SLACK_BOT_TOKEN")
    if not (channel and token):
        raise SystemExit("digest: need SLACK_BOT_TOKEN + SLACK_CHANNEL (or -t/-c)")
    icons = _icons_dir()

    def deploy(local: Path, name: str) -> str | None:
        # publish the icons dir (incl. the freshly-rendered plot) to the Pages
        # project; needs CLOUDFLARE_* + node/wrangler. Return the deployment-
        # specific URL (served instantly), which the OP image uses to avoid
        # racing root-alias CDN propagation (→ Slack `invalid_blocks`).
        import re
        import shutil
        import subprocess

        # The job image installs wrangler globally (`npm install -g`) but has
        # no `npx` shim, so prefer the binary; `npx` only serves a laptop run.
        wrangler = [shutil.which("wrangler")] if shutil.which("wrangler") else ["npx", "wrangler"] if shutil.which("npx") else None
        if wrangler is None:
            raise SystemExit("digest: neither `wrangler` nor `npx` on PATH — can't publish the plot")
        r = subprocess.run(
            [*wrangler, "pages", "deploy", str(icons), "--project-name", "gcs-usage-icons", "--branch", "main", "--commit-dirty=true"],
            check=True, capture_output=True, text=True,
        )
        err(r.stdout)
        m = re.search(r"https://[a-z0-9]+\.gcs-usage-icons\.pages\.dev", r.stdout + r.stderr)
        return m.group(0) if m else None

    dg.post_digest(root, m, token, channel, site_url=site_url, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay)
    err(f"digest: converged {m:%Y-%m}")


@main.command("discord-emoji")
@option("-b", "--bot-token", help="Discord bot token (default $DISCORD_BOT_TOKEN)")
@option("-i", "--icons", type=Path, help="Dir of arrow_deg*.png glyphs (default job/icons/arrows)")
@option("-n", "--dry-run", is_flag=True, help="Say what would be uploaded; upload nothing")
def discord_emoji(bot_token: str | None, icons: Path | None, dry_run: bool) -> None:
    """Upload the digest's trend-arrow glyphs (arrow_deg-80 … arrow_deg80) as
    application emoji on the bot, so `digest -P discord` can render `:arrow_degN:`
    as `<:arrow_degN:id>` (negatives become `arrow_degmN`: Discord names allow no
    `-`). Idempotent: names already on the app are kept. Prints `name id` for the
    whole set on stdout."""
    import re

    from . import digest as dg
    from . import discord_api as api

    bot_token = secret(bot_token, "DISCORD_BOT_TOKEN")
    if not bot_token:
        raise SystemExit("discord-emoji: need DISCORD_BOT_TOKEN (or -b)")
    icons = icons or _icons_dir() / "arrows"
    glyphs = {
        dg.emoji_name(int(m.group(1))): p
        for p in sorted(icons.glob("arrow_deg*.png"))
        if (m := re.fullmatch(r"arrow_deg(-?\d+)\.png", p.name))
    }
    if not glyphs:
        raise SystemExit(f"discord-emoji: no arrow_deg*.png under {icons}")
    app = api.app_id(bot_token)
    have = api.app_emojis(bot_token, app)
    for name, p in glyphs.items():
        if name in have:
            continue
        if dry_run:
            err(f"would upload {name} <- {p.name}")
            continue
        have[name] = api.upload_app_emoji(bot_token, app, name, p)
        err(f"uploaded {name} <- {p.name}")
    for name in sorted(have):
        print(name, have[name])


@main.command("discord-webhook")
@option("-b", "--bot-token", help="Discord bot token (default $DISCORD_BOT_TOKEN)")
@option("-c", "--channel", required=True, help="Channel id, or `#name` resolved in --guild")
@option("-g", "--guild", help="Guild id for a `#name` channel (default $DISCORD_GUILD)")
@option("-N", "--name", default="GCS usage", help="Webhook name (default 'GCS usage')")
def discord_webhook(bot_token: str | None, channel: str, guild: str | None, name: str) -> None:
    """Create (or reuse, by name) a webhook owned by the bot's application in a
    channel, and print its URL on stdout. App-owned matters: Discord renders the
    bot's application emoji (`discord-emoji`) only from the bot or a webhook the
    bot owns — through a user-created webhook `<:name:id>` silently degrades to
    `:name:`. Needs Manage Webhooks on the channel. The URL embeds a secret:
    redirect stdout into a secret store or a 0600 file, never a log."""
    from . import discord_api as api

    bot_token = secret(bot_token, "DISCORD_BOT_TOKEN")
    if not bot_token:
        raise SystemExit("discord-webhook: need DISCORD_BOT_TOKEN (or -b)")
    if channel.startswith("#"):
        guild = guild or os.environ.get("DISCORD_GUILD")
        if not guild:
            raise SystemExit("discord-webhook: a `#name` channel needs -g/--guild (or $DISCORD_GUILD)")
        chans = api.guild_channels(bot_token, guild)
        if channel[1:] not in chans:
            raise SystemExit(f"discord-webhook: no text channel {channel} in guild {guild}")
        channel = chans[channel[1:]]
    app = api.app_id(bot_token)
    mine = [h for h in api.channel_webhooks(bot_token, channel) if h.get("application_id") == app and h["name"] == name]
    if mine:
        hook = mine[0]
        err(f"discord-webhook: reusing app-owned webhook {hook['id']} ({name!r}) in channel {channel}")
    else:
        hook = api.create_webhook(bot_token, channel, name)
        err(f"discord-webhook: created app-owned webhook {hook['id']} ({name!r}) in channel {channel}")
    print(api.webhook_url(hook))


@main.command("publish-r2")
@option("-b", "--bucket", "src_bucket", default=None, help="Source GCS scan store (default $DATA_BUCKET)")
@option("-l", "--layer2", default=None, help="Layer-2 dir template, `{scan}` = the scan id (default $LAYER2_PREFIX, else listing/{scan}/index/; cw: cw-l2/{scan}/)")
@option("-L", "--no-listings", is_flag=True, help="Leave the canonical per-bucket listings (`<layer-2 dir>/<bucket>.parquet`) in GCS only; copy the tiers + snapshot JSONs")
@option("-n", "--dry-run", is_flag=True, help="List the keys that would be copied; copy nothing")
@option("-p", "--prefix", "prefixes", multiple=True, help="Key prefix to publish (repeatable; default: the scan's served subset — snapshots/<subdir>/<scan>/ + the layer-2 dir)")
@option("-s", "--subdir", default=None, help="Snapshots subdir of this store (default $SNAPSHOTS_SUBDIR, else none)")
@option("-w", "--workers", default=8, type=int, help="Concurrent HEADs/uploads (default 8)")
@argument("scan")
def publish_r2(src_bucket: str | None, layer2: str | None, no_listings: bool, dry_run: bool, prefixes: tuple[str, ...], subdir: str | None, workers: int, scan: str) -> None:
    """Copy one scan's served artifacts GCS → R2.

    The final "publish to the serving cloud" stage of an ingest that builds
    against GCS: snapshot JSONs + the layer-2 dir (`index/<gen>/` tiers,
    `.groups.json` manifests, age pyramids; on cw also the canonical parquets).
    Idempotent — same size + md5 already in R2 is skipped — so it doubles as
    the backfill over old scans. R2 via the env: R2_ENDPOINT, R2_BUCKET,
    R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (`s3` extra).
    """
    from . import publish as pub

    pub.publish(
        scan,
        src_bucket=src_bucket or pub.DATA_BUCKET,
        prefixes=list(prefixes) or None,
        subdir=pub.SNAPSHOTS_SUBDIR if subdir is None else subdir,
        layer2=layer2 or pub.LAYER2_PREFIX,
        dry_run=dry_run,
        workers=workers,
        listings=not no_listings,
    )
