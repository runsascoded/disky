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

from .identity import IDENTITIES_ENV, load_identities
from .deploy import data_bucket, site_token, site_url as resolve_site_url
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
@option("-p", "--run-root", "run_roots", multiple=True, default=("checkpoints",), help="Top-level dir holding run-named dirs (repeatable; default `checkpoints`)")
@option("-r", "--runs", "runs_path", required=True, type=Path, help="wandb-mine output parquet")
@option("-x", "--executor-infos", "executor_path", type=Path, default=None, help="executor-mine output parquet (adds executor-wandb rows)")
def wandb_attr(
    identities_path: str,
    listings: tuple[str, ...],
    out: Path,
    run_roots: tuple[str, ...],
    runs_path: Path,
    executor_path: Path | None,
) -> None:
    """Attribution rows from W&B runs: run-name ↔ dirs under the run roots (`-p`) + writer-path configs."""
    from .wandb_signal import executor_rows, run_name_rows, writer_path_rows

    identities = load_identities(identities_path)
    asof = dt.date.today()
    runs = pd.read_parquet(runs_path)
    err(f"{len(runs)} mined runs")
    con = _connect()
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)
    # Run-named dirs live at level 2 (<root>/<run>/) but also deeper under
    # namespace dirs — <root>/<ns>/<run>/, even <root>/<ns>/<ns>/<run>/ — so
    # emit levels 2-4 as (parent, leaf).
    roots = " OR ".join("name LIKE '" + r.strip("/").replace("'", "''") + "/%'" for r in run_roots)
    run_dirs = con.execute(
        f"""
        WITH l AS (
          SELECT DISTINCT bucket,
            regexp_extract(name, '^([^/]+)/', 1) AS d1,
            regexp_extract(name, '^[^/]+/([^/]+)/', 1) AS d2,
            regexp_extract(name, '^[^/]+/[^/]+/([^/]+)/', 1) AS d3,
            regexp_extract(name, '^[^/]+/[^/]+/[^/]+/([^/]+)/', 1) AS d4
          FROM {src}
          WHERE ({roots})
        )
        SELECT DISTINCT bucket, d1 AS parent, d2 AS leaf FROM l WHERE d2 IS NOT NULL
        UNION
        SELECT DISTINCT bucket, d1 || '/' || d2 AS parent, d3 AS leaf FROM l WHERE d3 IS NOT NULL
        UNION
        SELECT DISTINCT bucket, d1 || '/' || d2 || '/' || d3 AS parent, d4 AS leaf FROM l WHERE d4 IS NOT NULL
        """
    ).df()
    err(f"{len(run_dirs)} level-2/3/4 dirs under {', '.join(run_roots)}")
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
@option("-e", "--entity", envvar="WANDB_ENTITY", required=True, help="W&B entity to mine (default $WANDB_ENTITY)")
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
@option("-c", "--dir-cache", "dir_cache", type=Path, default=None, help="Layer-2 cache dir (dir-stats/age-days parquet): attribution-independent rollups reused by re-attribution runs — see gcs:specs/dir-agg-cache.md")
@option("-d", "--asof", required=True, help="Scan date the listing came from (YYYY-MM-DD)")
@option("-g", "--age-strata", is_flag=True, help="Add bytes-by-age columns (`age_b0`…`age_b6`, specs/done/row-age-strata.md) to every store row; off by default, so a store keeps its schema until it opts in")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, default=None, help=f"identities.yaml path or URL, needed with -a (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Output dir for JSON files [default: site/public/data/<asof>]")
@option("-r", "--row-group-rows", default=None, type=int, help="Parquet row-group size for the store's sorts (default 8192 — the reader decodes ~one group per depth of a drilled subtree, so 32768 pushed small drills past its decode cap; specs/path-store.md §1.6)")
@option("-S", "--search", is_flag=True, help="Also write the search sidecars beside the `path` sort (`path-index.{rows,trigrams,rows-search}.parquet`: the filter view's segment-name index, specs/path-store-search.md); upload them with the generation dir")
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
    search: bool,
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
        user_sorts=user_sorts, row_group_rows=row_group_rows, age_strata=age_strata, search=search,
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
@option("-t", "--token", default=None, help="Bearer token (default: $SITE_TOKEN)")
@option("-u", "--url", default=None, help="Site base URL (default: $SITE_URL)")
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


@main.command()
@option("-b", "--budget", default=None, type=float, help="Fail a scenario whose slowest request exceeds this many seconds")
@option("-c", "--cold", is_flag=True, help="Key subtree and diff reads past the edge cache (a random `minArea` ≈ the default), to measure uncached cost")
@option("-j", "--json", "as_json", is_flag=True, help="Emit the run record (every request) as JSON to stdout")
@option("-o", "--out", default=None, help="Write the run record to this path or prefix (`…/` or `gs://…/` → `<prefix><ts>.json`)")
@option("-q", "--hit", default=None, help="Filter term the filter-hit scenario searches for (default: the largest bucket's largest child)")
@option("-s", "--subdir", default=None, help="Snapshot subdir under /data/ (default: $SNAPSHOTS_SUBDIR)")
@option("-S", "--serial", is_flag=True, help="Send each scenario's requests one at a time (default: concurrently, as a page load does)")
@option("-t", "--token", default=None, help="Bearer token (default: $SITE_TOKEN; none for a public deployment like r2.rbw.sh)")
@option("-u", "--url", default=None, help="Site base URL (default: $SITE_URL)")
def probe(budget: float | None, cold: bool, as_json: bool, out: str | None, hit: str | None, subdir: str | None, serial: bool, token: str | None, url: str | None) -> None:
    """Replay the site's page loads (root, largest bucket, a matching and a
    non-matching path filter) against a live deployment.

    Each scenario's API requests go out concurrently, like the browser's, and
    each response's status, wall time, edge-cache tier and server time is
    recorded. Exits nonzero on any 5xx or transport failure (and on a scenario
    over `--budget`). `-o` keeps the record, so a prefix of runs is a latency
    time series.
    """
    import fsspec

    import random

    from .probe import cold_min_area, http_fetch, record, resolve_targets, run, scenarios, summarize
    from .site import creds

    base, tok = creds(token, url)
    sub = subdir if subdir is not None else os.environ.get("SNAPSHOTS_SUBDIR", "")
    fetch = http_fetch(base, tok)
    t = resolve_targets(fetch, sub, hit)
    min_area = cold_min_area(random.Random()) if cold else ""
    err(f"probe {base} @ {t.date} (vs {t.prev}; bucket {t.bucket}; filter hit {t.hit!r}{'; cold' if cold else ''})")
    results = run(fetch, scenarios(t, min_area), parallel=not serial)
    ok, lines = summarize(results, round(budget * 1000) if budget is not None else None)
    for line in lines:
        err(line)
    err("PASS" if ok else "FAIL")
    rec = {**record(base, t, results), "cold": cold}
    if out:
        path = f"{out}{rec['ts']}.json" if out.endswith("/") else out
        with fsspec.open(path, "w") as fh:
            json.dump(rec, fh)
        err(f"wrote {path}")
    if as_json:
        print(json.dumps(rec, indent=2))
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
@option("-b", "--bucket", default=None, help="Bucket a bare (no `<bucket>=`) layer-2 argument describes (default $SWEEP_BUCKET)")
@option("-m", "--mem", default="8GB", help="DuckDB memory limit")
@option("-o", "--out", "out_dir", type=Path, required=True, help="Output dir: path-index.parquet + path-index-bysize.parquet (+ .groups.json sidecars) + age-pyramid-*.parquet")
@option("-r", "--row-group-rows", default=8192, type=int, help="Parquet row-group size for the sorts — the range-read unit and the D1 footer's row count per sort (default 8192; a gcs-sized fleet uses 32768, specs/path-store.md §1.6)")
@option("-S", "--search", is_flag=True, help="Also write the search sidecars beside the `path` sort (`path-index.{rows,trigrams,rows-search}.parquet`, specs/path-store-search.md)")
@option("-t", "--threads", default=8, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", type=Path, default=None, help="DuckDB spill dir (default: <out>/.duckdb-tmp)")
@argument("sources", nargs=-1, required=True)
def index_write(age_only: bool, bucket: str | None, mem: str, out_dir: Path, row_group_rows: int, search: bool, threads: int, tmp_dir: Path | None, sources: tuple[str, ...]) -> None:
    """Write the scan's path store from its layer-2 parquet(s) — SOURCES are
    `<bucket>=<l2.parquet>` pairs, one per bucket of the scan (a bare path is
    `-b`'s bucket): every row (objects and dirs), bucket-prefixed, in the
    layer-2's column names, cut by the engine into the `path` sort
    (`path-index.parquet`, `(depth, path)`) and the `bysize` sort
    (`path-index-bysize.parquet`, `(⌊log2 size⌋ desc, path)`), 8k-row groups,
    footer sidecars beside them, plus the age pyramid (specs/path-store.md
    §4.2). `index-sync` then publishes their footers to D1."""
    from .index import write_index
    from .sweep import sweep_bucket

    bare = any("=" not in s for s in sources)
    s = write_index(
        bucket_sources(sources, bucket or (sweep_bucket() if bare else "")), out_dir,
        mem=mem, threads=threads, tmp_dir=tmp_dir, age_only=age_only, row_group_rows=row_group_rows, search=search,
    )
    if age_only:
        err(f"index-write: age pyramid only — floor {s['pyramid']['floor']}, {len(s['pyramid']['bins'])} tiers over {s['buckets']}")
    else:
        err(f"index-write: {s['rows']:,} rows over {s['buckets']}; sorts {s['sorts']}")
    print(json.dumps(s))


@main.command("over-time-groups")
@option("-b", "--bucket", envvar="DATA_BUCKET", required=True, help="Data bucket the D1 `path` dirs resolve against; default $DATA_BUCKET")
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


@main.command("churn")
@option("-c", "--column", "columns", multiple=True, help="Only compare these value columns (repeatable; default: every non-key column both files share)")
@option("-m", "--mem", default="8GB", help="DuckDB memory limit")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Also write the delta (`delta.parquet`, `delta-objects.parquet`) here and report its bytes")
@option("-t", "--threads", default=4, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", type=Path, default=None, help="DuckDB spill dir (default: DuckDB's)")
@argument("a")
@argument("b")
def churn_cmd(columns: tuple[str, ...], mem: str, out_dir: Path | None, threads: int, tmp_dir: Path | None, a: str, b: str) -> None:
    """Rows added / removed / changed from scan A to scan B (specs/storage-consolidation.md
    phase 3): A and B are two `path` sorts (or layer-2s) — local paths; stage
    remote ones first. Keyed `(depth, path)` (+ an owner label both carry),
    joined one depth at a time; counts per `kind` and per changed column, as
    JSON on stdout."""
    from .churn import scan_churn

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{mem}'; SET threads={threads}")
    if tmp_dir is not None:
        con.execute(f"SET temp_directory='{tmp_dir}'")
    print(json.dumps(scan_churn(a, b, out_dir=out_dir, columns=list(columns) or None, con=con), indent=1))


@main.command("over-time-churn")
@argument("groups", nargs=-1, required=True)
def over_time_churn_cmd(groups: tuple[str, ...]) -> None:
    """Per-scan dir churn (changed / added / removed paths at each scan boundary)
    of sealed over-time groups (`over-time.parquet`, local paths), as JSON lines
    on stdout — one object per group."""
    from .churn import group_churn

    con = _connect()
    for g in groups:
        print(json.dumps({"group": g, **group_churn(g, con=con)}))


@main.command("index-sync")
@option("-A", "--age-only", is_flag=True, help="Only the age-pyramid variants (a ladder-only backfill; the other variants keep their pointer)")
@option("-b", "--bucket", envvar="DATA_BUCKET", required=True, help="Data bucket holding the index tiers; default $DATA_BUCKET")
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
@option("-b", "--base", envvar="DATA_BUCKET", default=None, help="Where the pointers' dirs live, for -r's cold-footer check: the data bucket (default $DATA_BUCKET), a mounted dir, or an fsspec URL (`r2://bucket`)")
@option("-F", "--files", "targets", multiple=True, help="Also delete the files of generation dirs no pointer names (repeatable): `gs://<bucket>` (the scan store), `r2` (the R2 serving bucket: `$R2_BUCKET`, via publish-r2's `R2_*` env) or `r2://<bucket>`. Only under scans with a `path` pointer; never a pointed dir; never one younger than -m")
@option("-m", "--min-age", default="2d", help="-F's grace period: a generation whose newest object is younger is kept, so an in-flight reindex is never raced; e.g. 36h, 2d (default)")
@option("-n", "--dry-run", is_flag=True, help="Print what would go (D1 rows counted; generation dirs per store with bytes, and totals); delete nothing")
@option("-r", "--retain", type=int, default=None, help="Retention: also retire the store sorts' row groups of every scan older than the newest N whose `.groups.parquet` exists (their pointers stay; the reader range-reads that cold footer instead). A variant without one keeps its rows (warned): backfill it with `index-blob`")
@option("-R", "--no-rows", is_flag=True, help="Skip the D1 row sweep (e.g. an -F pass over every scan after the job's per-scan row sweep)")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-w", "--workers", default=8, type=int, help="-F: concurrent listings (default 8)")
@argument("dates", nargs=-1)
def index_gc(
    base: str,
    targets: tuple[str, ...],
    min_age: str,
    dry_run: bool,
    retain: int | None,
    no_rows: bool,
    store: str,
    workers: int,
    dates: tuple[str, ...],
) -> None:
    """Delete row groups of index generations no pointer names — a REPROC's
    previous generation, or a sync that died before flipping. All synced
    scans by default; DATES to restrict. With -r, the retention pass too.

    With -F, also those generations' files (`<layer-2>/index/<gen>/`) in each
    target store (`dt_cloud.gen_gc`): dirs no `index_schema` row names, under
    scans that have a `path` pointer, older than -m. E.g. the backlog over
    every scan, GCS + R2, dry run first:

        dt-cloud index-gc -R -n -F gs://$DATA_BUCKET -F r2
    """
    import time

    from .gen_gc import open_store, parse_age, sweep
    from .index_footer import d1_variant, gc_d1, pointers, retire_d1, synced_variants

    if dry_run and retain is not None:
        raise UsageError("-n covers the row sweep and -F, not -r")
    try:
        grace = parse_age(min_age)
    except ValueError as e:
        raise UsageError(str(e)) from None
    stores = [open_store(t) for t in targets]
    if not no_rows:
        todo = dates or sorted({d for d, _ in synced_variants(store=store)})
        for d in todo:
            n = gc_d1(d, store=store, dry_run=dry_run)
            err(f"index-gc: {d} — {n} stale row groups {'would be ' if dry_run else ''}deleted")
    if stores:
        sweep(
            pointers(), stores,
            now=time.time(), min_age=grace, dry_run=dry_run, reread=pointers,
            path_variant=d1_variant("path", store), dates=dates or None, workers=workers,
        )
    if retain is not None:
        retired, skipped = retire_d1(retain, store=store, base=base or data_bucket())
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
    labels-<bucket>.parquet -c usr` (spec specs/done/mgu-scale-unification.md §B): the
    same prefix map `path-index` attributes with, so the two cascades can be
    compared slice for slice."""
    import duckdb

    from .viz import write_labels

    con = duckdb.connect()
    for bucket, n in write_labels(con, listings, attributions, identities_path, out_dir).items():
        err(f"labels: {bucket}: {n} prefixes → {out_dir / f'labels-{bucket}.parquet'}")


@main.command("index-blob")
@option("-a", "--all", "all_dates", is_flag=True, help="Every scan with a synced pointer (instead of DATES)")
@option("-b", "--bucket", envvar="DATA_BUCKET", required=True, help="Data bucket holding the index tiers; default $DATA_BUCKET")
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
@option("-t", "--token", help="Site read token (default $SITE_TOKEN)")
@option("-u", "--url", "site_url", default=None, help="Site base (default $SITE_URL)")
@option("-W", "--widths", default="512,1280,1536,1792,1920", help="Canvas widths to warm (the client sends ceil(innerWidth/128)*128; default = phone + common laptops)")
def warm_cache(date: str | None, jobs: int, dry_run: bool, root: str | None, token: str | None, site_url: str | None, widths: str) -> None:
    """Warm the site's subtree + diff caches for a scan: replay the home
    page's default requests (one subtree, the diff span chips 1d/3d/7d/14d/30d
    and the previous-scan pair, each with its summary) at the common canvas
    widths, so the first viewer anywhere gets a cache hit (colo cache + global
    KV). Non-fatal: a failed request just leaves that view cold."""
    from . import warm as wm

    # Deployment config: SITE_URL, DATA_BUCKET, SNAPSHOTS_SUBDIR (the job exports them).
    site_url = resolve_site_url(site_url)
    root = root or f"gs://{data_bucket()}/snapshots" + (f"/{os.environ['SNAPSHOTS_SUBDIR'].strip('/')}" if os.environ.get('SNAPSHOTS_SUBDIR') else '')
    dates = wm.scan_dates(root)
    if not dates:
        raise SystemExit("warm-cache: no scans under root")
    date = date or dates[-1]
    if date not in dates:
        raise SystemExit(f"warm-cache: {date} is not a published scan")
    # Auth: an agent bearer token (`-t` / SITE_TOKEN — the app gate), or a
    # Cloudflare Access service-token pair (CF_ACCESS_CLIENT_ID/SECRET — a
    # whole-host edge-gated deployment). Same request either way.
    def auth() -> dict[str, str]:
        tok = site_token(token)
        cid, csec = env_secret("CF_ACCESS_CLIENT_ID"), env_secret("CF_ACCESS_CLIENT_SECRET")
        if tok:
            return {"Authorization": f"Bearer {tok}"}
        if cid and csec:
            return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": csec}
        raise SystemExit("warm-cache: need SITE_TOKEN (or -t), or CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET")

    headers: dict[str, str] | None = None
    targets = wm.env_paths()
    if targets is None:
        # No `WARM_PATHS`: the root + its top level, as the site lists them.
        headers = auth()
        targets = wm.top_paths(site_url, headers, date)
    paths = wm.plan(date, dates, targets, tuple(int(w) for w in widths.split(",")))
    if dry_run:
        for p in paths:
            print(p)
        return
    headers = headers or auth()
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


_LC_BUCKET = option("-b", "--bucket", "buckets", multiple=True, help="`gs://<bucket>` (GCS) or a bare S3 bucket name; repeatable (default $SWEEP_BUCKET)")


def _lc_buckets(buckets: tuple[str, ...]) -> tuple[str, ...]:
    if buckets:
        return buckets
    from .sweep import sweep_bucket

    return (sweep_bucket(),)


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

    from .sweep import build_manifest, load_plan

    plan = load_plan(plan_path)
    if l2_path is None:
        l2_path = f"/gcs/{data_bucket()}/cw-l2/{date}/{plan.bucket}.parquet"
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


@sweep.command("sync-progress")
@option("-i", "--interval", default=0, type=int, help="Repeat until the job ends (≥30 seconds); default backfills once")
@option("-o", "--sql-output", default=None, help="Export INSERT SQL instead of writing D1 (for local browser verification)")
@argument("job_name")
def sweep_sync_progress(interval: int, sql_output: str | None, job_name: str) -> None:
    """Backfill/tail a recorded Batch run's task-log progress into D1."""
    from .sweep_progress import sync_progress

    sync_progress(job_name, interval, sql_output)


@sweep.command("manifest")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets (default: every bucket the plan names)")
@option("-d", "--date", required=True, help="Scan date whose listing to plan from (pinned)")
@option("-j", "--workers", default=0, type=int, help="Concurrent listing shards (default 2 × CPUs, ≤ 64)")
@option("-o", "--out", default=None, help="Output dir (default gs://$DATA_BUCKET/sweep/<date>-p<plan_id>)")
@option("-p", "--plan", "plan_path", required=True, help="The dispatched plan.json (path or gs:// URL): its items are the delete set, and the buckets are the plan's (∩ -b)")
@option("-r", "--root", default=None, help="Listing root (gs:// or local mount; default gs://$DATA_BUCKET)")
def sweep_manifest(only_buckets: tuple[str, ...], date: str, workers: int, out: str | None, plan_path: str, root: str | None) -> None:
    """Object-level manifest of a staged plan (specs/staged-delete.md): scan
    the pinned listing (every shard in parallel, row groups outside the staged
    prefixes pruned — `sweep_manifest.py`) and write per-bucket parquets of the
    ELIGIBLE keys — every key under a staged prefix — plus a category summary.
    The plan is the whole intent: nothing carves out. Pure read + artifact
    write — deletes nothing."""
    import time

    import fsspec

    from .staged_plan import CATEGORIES, load_plan
    from .sweep_manifest import build_manifests

    sp = load_plan(plan_path)
    buckets = [b for b in sp.buckets if not only_buckets or b in only_buckets]
    if not buckets:
        raise SystemExit(f"no plan bucket among -b {', '.join(only_buckets)} (plan {sp.plan_id} names {', '.join(sp.buckets)})")
    root = root or f"gs://{data_bucket()}"
    out = out or f"gs://{data_bucket()}/sweep/{date}-p{sp.plan_id}"
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

    t0 = time.monotonic()
    summary["buckets"] = build_manifests(root, date, {b: sp.sweep[b] for b in buckets}, out, workers=workers or None)
    err(f"manifest: {time.monotonic() - t0:.0f}s")

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


@sweep.command("reuse-manifest")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Exact reviewed bucket cut")
@option("-d", "--date", required=True, help="Pinned scan date")
@option("-o", "--out", required=True, help="New run directory; manifests must not exist")
@option("-p", "--plan", "plan_path", required=True, help="The new dispatch's plan.json")
@argument("source")
def sweep_reuse_manifest(
    only_buckets: tuple[str, ...],
    date: str,
    out: str,
    plan_path: str,
    source: str,
) -> None:
    """Reuse an identical completed DR's manifest; never delete anything."""
    from .sweep_reuse import reuse_manifest

    summary = reuse_manifest(source, plan_path, date, out, only_buckets)
    err(f"reused reviewed manifest from {source} → {out} ({len(summary['buckets'])} buckets)")
    print(json.dumps(summary, indent=2))
    _hard_exit()


@sweep.command("execute")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets")
@option("-D", "--drift", type=Choice(["skip", "proceed"]), default="skip", help="Dirs that gained new keys since the scan: skip (default) or proceed (manifest keys only — new keys always survive)")
@option("-m", "--max-root-objects", default=250_000, type=int, help="Split listing roots above this many manifest objects when safe")
@option("-w", "--workers", default=0, type=int, help="Concurrent directory re-lists (default 2 × CPUs, ≤ 16)")
@option("-W", "--delete-workers", default=32, type=int, help="Concurrent delete batches (100 objects each), shared by every re-list")
@option("--for-real", is_flag=True, help="Actually delete (default: dry-run writes would-delete/)")
@option("--no-record", is_flag=True, help="Skip the D1 deletion_runs/bands record (recorded by default)")
@argument("plan_dir")
def sweep_execute(only_buckets: tuple[str, ...], drift: str, max_root_objects: int, delete_workers: int, workers: int, for_real: bool, no_record: bool, plan_dir: str) -> None:
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
    progress_run_id = None
    if not no_record:
        # The run's D1 row goes in now (finished NULL) so /staged lists it while
        # the re-list runs — hours, on the big bands; completed at the end.
        from .sweep_exec import record_run_start
        try:
            run_id = record_run_start(plan_summary, plan_dir, actor=actor, started_ts=started, for_real=for_real, buckets=only_buckets)
            progress_run_id = run_id
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

    def progress(snapshot: dict) -> None:
        from .sweep_progress import sample, write_samples

        if progress_run_id is not None:
            write_samples(progress_run_id, [sample(snapshot)])

    summary = execute_plan(
        plan_dir,
        for_real=for_real,
        only_buckets=only_buckets,
        drift=drift,
        workers=workers,
        delete_workers=delete_workers,
        max_root_objects=max_root_objects,
        stop=stop,
        on_progress=progress if not no_record else None,
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


@sweep.command("batch-manifest")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets")
@argument("plan_dir")
def sweep_batch_manifest(only_buckets: tuple[str, ...], plan_dir: str) -> None:
    """Freeze a completed DR's delete decisions as generation-pinned CSVs."""
    from .sweep_batch import build_batch_manifests

    summary = build_batch_manifests(plan_dir, only_buckets=only_buckets)
    print(json.dumps(summary, indent=2))


@sweep.command("benchmark")
@option("-b", "--bucket", required=True, help="One target bucket from the plan")
@option("-j", "--workers", multiple=True, type=int, default=(8, 16, 32, 64), help="Listing concurrency to measure; repeatable")
@option("-m", "--max-root-objects", default=250_000, type=int, help="Executor root-splitting threshold")
@option("-n", "--roots", default=128, type=int, help="Representative listing roots to benchmark")
@option("-o", "--out", default=None, help="Optional JSON result path or URL")
@option("-r", "--max-results-per-root", default=50_000, type=int, help="Cap objects read from each root per trial")
@argument("plan_dir")
def sweep_benchmark(
    bucket: str,
    workers: tuple[int, ...],
    max_root_objects: int,
    roots: int,
    out: str | None,
    max_results_per_root: int,
    plan_dir: str,
) -> None:
    """Read-only cloud benchmark of the executor's real listing roots."""
    import fsspec

    from .sweep_benchmark import benchmark_listings

    result = benchmark_listings(
        plan_dir,
        bucket,
        workers=workers,
        roots=roots,
        max_root_objects=max_root_objects,
        max_results_per_root=max_results_per_root,
    )
    payload = json.dumps(result, indent=2)
    if out:
        with fsspec.open(out, "w") as fh:
            fh.write(payload + "\n")
        err(f"wrote {out}")
    print(payload)


@sweep.command("profile")
@option("-b", "--bucket", required=True, help="One reviewed target bucket")
@option("-g", "--groups", default=4, type=int, help="Spread manifest row groups to sample")
@option("-j", "--workers", default=4, type=int, help="Executor listing workers")
@option("-n", "--rows", default=10_000, type=int, help="Maximum manifest rows per sampled group")
@option("-o", "--out", "artifact_out", default=None, help="Empty diagnostic artifact directory or gs:// URL (includes remote log I/O)")
@option("-w", "--work-dir", required=True, help="Empty local directory for diagnostic artifacts and pstats")
@argument("plan_dir")
def sweep_profile(
    bucket: str,
    groups: int,
    workers: int,
    rows: int,
    artifact_out: str | None,
    work_dir: str,
    plan_dir: str,
) -> None:
    """Bounded dry-only executor profile; no deletes, no D1 writes."""
    from .sweep_profile import profile_executor

    print(json.dumps(profile_executor(plan_dir, bucket, work_dir, groups, rows, workers, artifact_out=artifact_out), indent=2))
    _hard_exit()


@sweep.command("benchmark-manifest")
@option("-b", "--bucket", required=True, help="One staged bucket")
@option("-d", "--date", required=True, help="Pinned listing date")
@option("-j", "--workers", default=4, type=int, help="Concurrent shards")
@option("-n", "--shards", default=4, type=int, help="Largest input shards to sample")
@option("-o", "--out", required=True, help="Empty diagnostic artifact directory or gs:// URL")
@option("-p", "--plan", "plan_path", required=True, help="Reviewed plan.json")
@option("-r", "--root", required=True, help="Listing store root")
@option("-w", "--work-dir", required=True, help="Empty local directory for staged input shards")
def sweep_benchmark_manifest(
    bucket: str,
    date: str,
    workers: int,
    shards: int,
    out: str,
    plan_path: str,
    root: str,
    work_dir: str,
) -> None:
    """Bounded manifest I/O benchmark, with exact row equality checks."""
    from .sweep_manifest_benchmark import benchmark_manifest_io

    result = benchmark_manifest_io(root, date, plan_path, bucket, out, work_dir, shards, workers)
    print(json.dumps(result, indent=2))
    _hard_exit()


@sweep.command("benchmark-delete")
@option("-j", "--workers", default=16, type=int, help="Scratch upload/delete workers (1..64)")
@option("-m", "--method", "methods", multiple=True, type=Choice(["xml", "json", "gcloud", "reviewed", "reviewed-warm"]), help="Methods to compare; reviewed-warm benchmarks the journal pipeline at an 8k/s scratch ceiling")
@option("-n", "--objects", default=10_000, type=int, help="New one-byte scratch objects per method (1..100000)")
@option("-s", "--verified-soft-delete-days", default=None, type=int, help="Scratch-only retention already verified by an admin; for job identities lacking bucket metadata access")
@argument("target")
def sweep_benchmark_delete(workers: int, methods: tuple[str, ...], objects: int, verified_soft_delete_days: int | None, target: str) -> None:
    """Create and delete bounded synthetic scratch data; never production data.

    TARGET must be gs://<bucket>/sweep/smoke-tests/delete-<unique-id>, outside
    `PROTECTED_BUCKETS` (comma-separated globs; required, '' for none).
    It must be empty and the bucket must retain soft deletes for seven days.
    """
    from .sweep_delete_benchmark import benchmark_deletes

    print(json.dumps(benchmark_deletes(target, objects, workers, methods or ("xml", "json", "gcloud"), verified_soft_delete_days=verified_soft_delete_days), indent=2))
    _hard_exit()


@sweep.command("execute-reviewed")
@option("-B", "--bucket-workers", default=6, type=int, help="Concurrent buckets (1..6), sharing the delete-worker pool")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Subset of reviewed DR buckets")
@option("-c", "--pacing", type=Choice(["guided", "adaptive"]), default="guided", help="Per-bucket feedback pacing; guided retains the GCS 2x/20m envelope, adaptive probes 25%/healthy minute")
@option("-e", "--backend", type=Choice(["xml", "json"]), default="xml", help="Generation-addressed delete backend")
@option("-j", "--workers", default=16, type=int, help="Concurrent full batches (1..64)")
@option("-o", "--out", required=True, help="New output run directory for progress and undo records")
@option("-p", "--plan", "plan_path", required=True, help="Current staged plan.json; must exactly match DR intent")
@option("-r", "--max-rate", default=8000, type=int, help="Per-bucket attempted-object/s ceiling, including retries; starts at ≤1000")
@option("-w", "--work-dir", required=True, help="Empty local directory for pinned decision-log cache")
@option("-x", "--exclude-run", "exclude_runs", multiple=True, help="Subtract this drained run's reconciled settled identities; include its full resume chain")
@option("--for-real", is_flag=True, help="Delete reviewed generations; default validates and writes a dry log")
@option("--no-record", is_flag=True, help="Skip D1 run/band records (scratch only)")
@argument("source")
def sweep_execute_reviewed(
    bucket_workers: int,
    only_buckets: tuple[str, ...],
    pacing: str,
    backend: str,
    workers: int,
    out: str,
    plan_path: str,
    max_rate: int,
    work_dir: str,
    exclude_runs: tuple[str, ...],
    for_real: bool,
    no_record: bool,
    source: str,
) -> None:
    """Stream a completed DR's exact generations; no fresh relist/drift audit.

    Full pinned-log preflight, recoverable deletes, bounded batches, progress,
    and per-object undo records. This is the normal real web-dispatch path.
    """
    import signal
    import threading
    from .sweep_exec import record_run, record_run_start, stop_file_watch
    from .sweep_reviewed import execute_reviewed

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop_file_watch(out, stop)
    started = int(dt.datetime.now(dt.timezone.utc).timestamp())
    actor = os.environ.get("USER", "?")

    progress_run_id = None

    def prepared(plan: dict) -> None:
        nonlocal progress_run_id
        if for_real and not no_record:
            try:
                progress_run_id = record_run_start(plan, out, actor, started, for_real, only_buckets)
            except Exception as error:
                err(f"WARN: deletion-run start record failed: {error}")

    def progress(snapshot: dict) -> None:
        from .sweep_progress import sample, write_samples

        if progress_run_id is not None:
            write_samples(progress_run_id, [sample(snapshot)])

    summary = execute_reviewed(source, plan_path, out, work_dir, for_real, only_buckets, backend, workers, max_rate, stop, on_prepared=prepared, bucket_workers=bucket_workers, exclude_runs=exclude_runs, on_progress=progress if not no_record else None, pacing=pacing)
    if for_real and not no_record:
        windows = [int(entry["soft_delete_days"]) for entry in summary["buckets"].values()]
        try:
            record_run(summary, summary["_plan"], actor, started, int(dt.datetime.now(dt.timezone.utc).timestamp()), min(windows) if windows else 7)
        except Exception as error:
            err(f"WARN: deletion-run final record failed: {error}")
    failed = any(entry.get("failed_dirs") or entry.get("error") for entry in summary["buckets"].values())
    print(json.dumps({key: value for key, value in summary.items() if key != "_plan"}, indent=2))
    if stop.is_set() or failed:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130 if stop.is_set() else 2)
    _hard_exit()


@sweep.command("batch-submit")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets")
@option("--for-real", is_flag=True, help="Actually delete; default submits a managed dry run")
@option("-p", "--project", default=None, help="Job project (default $GCP_PROJECT / ADC project)")
@argument("plan_dir")
def sweep_batch_submit(only_buckets: tuple[str, ...], for_real: bool, project: str | None, plan_dir: str) -> None:
    """Submit generation-pinned Storage Batch Operations jobs, dry by default."""
    from .sweep_batch import submit_batch_jobs

    result = submit_batch_jobs(plan_dir, dry_run=not for_real, only_buckets=only_buckets, project=project)
    print(json.dumps(result, indent=2))


@sweep.command("wait-drained")
@option("-t", "--timeout", default=7200, type=int, help="Maximum seconds to wait; timeout never starts deletion")
@argument("run")
def sweep_wait_drained(timeout: int, run: str) -> None:
    """Handoff barrier: require STOP, wait for the old delete pool's final summary."""
    from .sweep_job import wait_drained

    summary = wait_drained(run, timeout)
    counts = Counter()
    for bucket in summary["buckets"].values():
        counts.update(bucket.get("decisions", {}))
    print(json.dumps({"drained": run, "decisions": dict(counts)}))
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


@sweep.command("recovery-check")
@option("-l", "--limit", default=1, type=int, help="Soft-deleted samples per prefix (1–100; not a full inventory)")
@argument("prefixes", nargs=-1, required=True)
def sweep_recovery_check(limit: int, prefixes: tuple[str, ...]) -> None:
    """Read-only, bounded inspection of recoverable generations and their expiry."""
    from .sweep_recovery import recovery_sample
    for prefix in prefixes:
        print(json.dumps(recovery_sample(prefix, limit)))


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
    run's recorded `undo_deadline`. Actual retention starts per object at
    deletion, so that record is not an object-level guarantee. RUN is the D1
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
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: $ACCESS_BUCKETS, space-separated)")
@option("-c", "--max-chunk-gb", default=32.0, help="Max staged CSV bytes per processing chunk")
@option("-d", "--data-bucket", envvar="DATA_BUCKET", required=True, help="Output/state bucket; default $DATA_BUCKET")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: $ACCESS_LOG_BUCKET)")
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
    from .access import access_buckets, access_log_bucket, ingest

    ingest(
        buckets=buckets or access_buckets(),
        log_bucket=log_bucket or access_log_bucket(),
        data_bucket=data_bucket,
        stage_dir=(stage_dir or Path(os.environ.get("STAGE_DIR") or "/tmp") / "access-stage"),
        memory_limit=memory_limit or os.environ.get("DUCKDB_MEM_ACCESS") or "8GB",
        max_chunk_gb=max_chunk_gb,
        workers=workers,
        max_chunks=max_chunks,
    )


@access.command("sweep")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: $ACCESS_BUCKETS, space-separated)")
@option("-d", "--data-bucket", envvar="DATA_BUCKET", required=True, help="State bucket holding the watermarks; default $DATA_BUCKET")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: $ACCESS_LOG_BUCKET)")
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

    from .access import LAG_HOURS, access_buckets, access_log_bucket, load_state, sweep_ingested

    client = storage.Client()
    total = 0
    for b in buckets or access_buckets():
        state = load_state(client, data_bucket, b)
        total += sweep_ingested(
            client, log_bucket or access_log_bucket(), b, state.get("watermark"),
            workers=workers, lag_hours=0 if through_watermark else LAG_HOURS,
        )
    err(f"sweep: {total} CSV(s) moved to ingested/")


@access.command("status")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: $ACCESS_BUCKETS, space-separated)")
@option("-d", "--data-bucket", envvar="DATA_BUCKET", required=True, help="Output/state bucket; default $DATA_BUCKET")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: $ACCESS_LOG_BUCKET)")
def access_status(buckets: tuple[str, ...], data_bucket: str, log_bucket: str | None) -> None:
    """Per-bucket watermark vs delivered backlog (files/bytes awaiting ingest)."""
    from google.cloud import storage

    from .access import access_buckets, access_log_bucket, list_new, load_state

    client = storage.Client()
    for b in buckets or access_buckets():
        state = load_state(client, data_bucket, b)
        todo = list_new(client, log_bucket or access_log_bucket(), b, state)
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
        message = e.get("textPayload") or e.get("jsonPayload", {}).get("message", "")
        print(f"{e.get('timestamp', '')[:19]} {message.rstrip()}")


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
@option("-b", "--bucket", "buckets", multiple=True, help="Bucket(s) to list [default: $FLEET_BUCKETS, space-separated]")
@option("-d", "--date", "date", required=True, help="Listing date — output goes to listing/<date>/<bucket>/")
@option("-L", "--legacy-weights", "legacy", multiple=True, help="`<bucket>=<subdir>`: also take that bucket's chunk weights from an older listing layout under the data bucket (`<subdir>/*`), after its DIY listings; repeatable")
@option("-m", "--machine", default="n2-standard-32", help="Machine type per task")
@option("-P", "--procs", default=24, help="bulk-list worker processes per task")
@option("-w", "--workers", "threads", default=10, help="Concurrent prefix streams per process")
@option("-W", "--wait", "wait", is_flag=True, help="Block until the job reaches a terminal state")
def job_submit_listing(
    buckets: tuple[str, ...],
    date: str,
    legacy: tuple[str, ...],
    machine: str,
    procs: int,
    threads: int,
    wait: bool,
) -> None:
    """Submit the DIY fleet-listing Batch job (one task per bucket; one job per
    region, from `$LISTING_REGIONS`). Runs as `$JOB_SA` from `$JOB_IMAGE` in
    `$GCP_PROJECT`, writing under `$DATA_BUCKET`.

    Tasks reuse completed listings (``-x reuse``), so re-submitting for the
    same date only re-lists buckets that haven't finished — safe to retry.
    """
    from .batch import REGION, fleet_buckets, listing_job_spec, listing_regions, submit_job, wait_jobs

    bkts = list(buckets) or fleet_buckets()
    old = dict(spec.split("=", 1) for spec in legacy)
    regions = listing_regions()
    by_region: dict[str, list[str]] = {}
    for b in bkts:
        by_region.setdefault(regions.get(b, REGION), []).append(b)
    jobs = []
    for region, rb in by_region.items():
        spec = listing_job_spec(date, rb, machine=machine, procs=procs, threads=threads, region=region, legacy=old)
        name = submit_job(spec, region=region)
        err(f"submitted {name} [{region}]: {len(rb)} bucket task(s) on {machine}")
        print(name)
        jobs.append((name, region))
    if wait:
        states = wait_jobs(jobs, log=err)
        if bad := {n: s for n, s in states.items() if s != "SUCCEEDED"}:
            raise SystemExit(f"listing job(s) failed: {bad}")


@job.command("submit-reviewed")
@option("-B", "--bucket-workers", default=6, type=int, help="Concurrent buckets, sharing the XML-worker pool")
@option("-a", "--after-run", default=None, help="Wait for this stopped executor to drain before starting XML")
@option("-b", "--bucket", "buckets", multiple=True, help="Subset of reviewed buckets")
@option("-c", "--pacing", type=Choice(["guided", "adaptive"]), default="guided", help="Per-bucket feedback policy (adaptive is the faster opt-in probe)")
@option("-i", "--image", required=True, help="Executor image pinned by @sha256 digest")
@option("-j", "--workers", default=32, type=int, help="Concurrent XML batches")
@option("-n", "--name", required=True, help="New Batch job ID")
@option("-o", "--out", required=True, help="New run output directory")
@option("-p", "--plan", required=True, help="Snapshot of current staged plan")
@option("-r", "--max-rate", default=8000, type=int, help="Per-bucket object/s ceiling")
@option("-t", "--template", required=True, type=Path, help="Described prior Batch job JSON; reuse deployment settings")
@option("-x", "--exclude-run", "exclude_runs", multiple=True, help="Exclude reconciled settled identities from a drained run")
@option("--for-real", is_flag=True, help="Actually delete reviewed generations (default validates only)")
@option("--submit", is_flag=True, help="Submit to Batch; default prints the spec for review")
@argument("source")
def job_submit_reviewed(
    bucket_workers: int,
    after_run: str | None,
    buckets: tuple[str, ...],
    pacing: str,
    image: str,
    workers: int,
    name: str,
    out: str,
    plan: str,
    max_rate: int,
    template: Path,
    exclude_runs: tuple[str, ...],
    for_real: bool,
    submit: bool,
    source: str,
) -> None:
    """Explicit XML dispatch; callers must stop any overlapping old executor."""
    from .batch import submit_job
    from .sweep_job import reviewed_job_spec

    spec = reviewed_job_spec(json.loads(template.read_text()), source, plan, out, image, workers, max_rate, for_real, buckets, after_run, bucket_workers=bucket_workers, exclude_runs=exclude_runs, pacing=pacing)
    if not re.fullmatch(r"gcs-sweep-(real|dry)-\d{8}-\d{6}z", name):
        raise UsageError("job name must use the site's gcs-sweep-(real|dry)-YYYYMMDD-HHMMSSz format")
    expected = f"gs://{spec['taskGroups'][0]['taskSpec']['environment']['variables']['DATA_BUCKET']}/sweep/runs/{name}"
    if out != expected:
        raise UsageError(f"output must match the job's canonical run directory: {expected}")
    if ("-real-" in name) != for_real:
        raise UsageError("job name mode must match --for-real")
    if submit:
        region = spec["allocationPolicy"]["location"]["allowedLocations"][0].removeprefix("regions/")
        print(submit_job(spec, name, region))
    else:
        print(json.dumps(spec, indent=2))


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


@sii.command("status")
@option("-b", "--bucket", "buckets", multiple=True, help="Bucket(s) to check [default: $SII_BUCKETS, space-separated]")
def sii_status(buckets: tuple[str, ...]) -> None:
    """Per-bucket SII health: report config, latest generated report, and which
    days' shards have actually landed in gs://<bucket>/inventory-reports/."""
    import re as _re
    from collections import defaultdict

    from google.cloud import storage

    from .gcp import sii_report_configs, sii_report_details

    from .deploy import words

    client = storage.Client()
    for b in buckets or words("SII_BUCKETS", what="the buckets with Storage Insights reports (or pass -b)"):
        # The report configs live per location (`us-east1`, `eu`, …): the bucket's own.
        location = client.get_bucket(b).location.lower()
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
@option("-t", "--token", default=None, help="Bearer token (default: $SITE_TOKEN)")
@option("-u", "--url", default=None, help="Site base URL (default: $SITE_URL)")
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
        raise SystemExit("export: no token (-t or $SITE_TOKEN)")
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
    """The A.3 gate (spec specs/done/mgu-scale-unification.md): DT's `import -e duckdb
    --label usr` dirs tier against mgu's path index for one bucket, joined on
    `(path, usr)` — rows only one side has, and per-column disagreements
    (`b`↔`size`, `o`↔`n_files`, `c2..c4`↔`sum_storage_class_id_*`,
    `wts/wb`↔`mtime_mean`). Exit 1 on any difference."""
    from .cascade_a2a import compare, render

    report = compare(bucket, index_path, dirs_tier, top)
    print(json.dumps(report, indent=1, default=str) if as_json else render(report))
    if not report["ok"]:
        raise SystemExit(1)


def _icons_dir(rel: str = "job/icons") -> Path:
    """``rel`` (e.g. `job/icons`) in both layouts: pip-installed in the job image
    (cwd=/app → /app/job/icons) or the repo checkout (…/parents[3]/job/icons)."""
    cands = (Path.cwd() / rel, Path(__file__).resolve().parents[3] / rel)
    return next((c for c in cands if c.exists()), cands[-1])


def _digest_options(template_default: str):
    """`digest`'s options; `cw-digest` is the same command defaulting to `-T cw`."""
    opts = [
        option("-b", "--bot-token", help="Discord bot token: opens the month's thread + resolves app emoji (default $DISCORD_BOT_TOKEN; with -P discord)"),
        option("-c", "--channel", help="Slack channel id (default $SLACK_CHANNEL)"),
        option("-C", "--config", "config_path", type=Path, help="YAML/JSON DigestConfig overlaid on the template's preset (title, site, root, state, plot host, buckets/quotas, prices…; see specs/digest-unification.md)"),
        option("-D", "--reply-delay", "reply_delay", default=0.0, type=float, help="Seconds to sleep between replies (e.g. 305 for a spaced Slack backfill so per-reply sender chrome survives; Discord needs none)"),
        option("-E", "--edit-replies", is_flag=True, help="Re-edit every already-posted reply to its current body (backfill after a format change; -P discord only)"),
        option("-F", "--for-real", is_flag=True, help="With --redo-replies: actually post the new replies and delete the old ones (default: print the plan)"),
        option("-H", "--reply-hour", type=int, default=None, help="cw template: UTC hour the sender variant's daily reply is taken from — the day's first scan at/after it (default 12 → the 12:01Z morning scan, 8:01 am ET; the day's other scans still feed the OP + plot, and with the config's `provisional: true` its earlier ones post a provisional reply)"),
        option("-i", "--icons-dir", type=Path, default=None, help="Where the plot PNG is written + deployed from (default the config's `icons_dir`: job/icons, job/icons-cw)"),
        option("-m", "--month", help="Month YYYY-MM (default: current UTC month)"),
        option("-n", "--dry-run", is_flag=True, help="Render the plot + print OP/replies; post & host nothing"),
        option("-P", "--platform", type=Choice(["slack", "discord"]), default="slack", help="Which thread to converge (default slack)"),
        option("-r", "--root", help="Snapshots root (default the config's: gs://$DATA_BUCKET/snapshots[/cw])"),
        option("-R", "--redo-replies", is_flag=True, help="Re-post the month's replies under the current day rule, then delete the old ones (Slack; dry-run unless --for-real)"),
        option("-t", "--token", help="Slack bot token (default $SLACK_BOT_TOKEN)"),
        option("-T", "--template", type=Choice(["gcs", "cw"]), default=template_default, help=f"Post style + preset config (default {template_default}): gcs = a reply per scan, $/mo by storage class, class mosaic; cw = a reply per day, % of quota per bucket, quota sparkline + diff treemap"),
        option("-u", "--url", "site_url", default=None, help="Site base for links (default the config's `site_url`, else $SITE_URL)"),
        option("-V", "--variant", type=Choice(["sender", "body"]), default=None, help="Reply style (cw template): headline as the sender name, posted once from the day's morning scan (sender, default) or bold in the body, edited as the day's scans land (body)"),
        option("-w", "--webhook", help="Discord webhook URL in the digest channel (default $<config discord_webhook_env>: gcs $DISCORD_GCS_USAGE_WEBHOOK, cw none; with -P discord)"),
    ]

    def deco(f):
        for o in reversed(opts):
            f = o(f)
        return f
    return deco


def _digest(
    bot_token: str | None,
    channel: str | None,
    config_path: Path | None,
    reply_delay: float,
    edit_replies: bool,
    for_real: bool,
    reply_hour: int | None,
    icons_dir: Path | None,
    month: str | None,
    dry_run: bool,
    platform: str,
    root: str | None,
    redo_replies: bool,
    token: str | None,
    template: str,
    site_url: str | None,
    variant: str | None,
    webhook: str | None,
) -> None:
    from dataclasses import replace

    from . import digest as dg

    cfg = dg.load_config(template, config_path)
    cfg = replace(cfg, site_url=site_url or cfg.site_url, reply_hour=cfg.reply_hour if reply_hour is None else reply_hour)
    tpl = dg.template(cfg)
    variant = variant or cfg.variant
    if variant not in tpl.variants:
        raise SystemExit(f"digest: the {cfg.template} template has no {variant!r} variant ({' | '.join(tpl.variants)})")
    m = (
        dt.datetime.strptime(month, "%Y-%m").date()
        if month
        else dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    )
    root = root or cfg.resolve_root()

    if dry_run:
        print(dg.dry_run(tpl, root, m, variant))
        return

    if platform == "discord":
        if redo_replies:
            raise SystemExit("digest: -R/--redo-replies is Slack-only")
        webhook = secret(webhook, cfg.discord_webhook_env) if cfg.discord_webhook_env else webhook
        bot_token = secret(bot_token, "DISCORD_BOT_TOKEN")
        if not (webhook and bot_token):
            raise SystemExit(f"digest: -P discord needs {cfg.discord_webhook_env or 'a webhook (-w)'} + DISCORD_BOT_TOKEN (or -w/-b)")
        dg.post_digest_discord(tpl, root, m, webhook, bot_token, edit_replies=edit_replies)
        err(f"digest: converged {m:%Y-%m} (discord)")
        return
    if edit_replies:
        raise SystemExit("digest: -E/--edit-replies is Discord-only (Slack replies are re-posted with -R/--redo-replies)")
    channel = channel or os.environ.get("SLACK_CHANNEL")
    token = secret(token, "SLACK_BOT_TOKEN")
    if not (channel and token):
        raise SystemExit("digest: need SLACK_BOT_TOKEN + SLACK_CHANNEL (or -t/-c)")
    from thrds.slack import SlackClient

    client = SlackClient(token, channel)
    icons = icons_dir or _icons_dir(cfg.icons_dir)

    def deploy(local: Path, name: str) -> str | None:
        # publish the icons dir (incl. the freshly-rendered plot) to the
        # config's Pages project + branch — cw's is a preview branch, never the
        # production branch whose root alias serves the arrow avatars
        return dg.pages_deploy(icons, cfg.plot_project, cfg.plot_branch)

    if redo_replies:
        # rule change: re-post every reply under the current day rule, then retire the old ones
        plan = dg.redo_replies(tpl, root, m, client, channel, variant, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay, for_real=for_real)
        if for_real:
            err(f"digest: re-threaded {m:%Y-%m} ({variant}): {len(plan.get('posted', {}))} replies" + (f", {len(plan['stale'])} old left undeleted" if plan.get("stale") else ""))
            return
        old = {day: e for day, e in plan["old"]}
        print(f"digest --redo-replies {m:%Y-%m} in {channel} ({variant}; dry-run — -F/--for-real applies):")
        print(f"  old replies to delete: {len(plan['old'])}")
        for day, e in plan["old"]:
            print(f"    {day}  {e['scan'] if isinstance(e, dict) else day}  ts={e['ts'] if isinstance(e, dict) else e}")
        print(f"  new replies to post: {len(plan['new'])}")
        for day, scan, head in plan["new"]:
            same = "  (same scan as the old reply)" if isinstance(old.get(day), dict) and old[day]["scan"] == scan else ""
            print(f"    {day}  {scan}  {head!r}{same}")
        return
    # the scheduled run (no -m) also closes the previous month's thread if its last day's provisional reply is still up
    converge = dg.converge_slack if month else dg.converge_slack_now
    converge(tpl, root, m, client, channel, variant, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay)
    err(f"digest: converged {m:%Y-%m} ({cfg.template}, {variant})")


@main.command()
@_digest_options("gcs")
def digest(**kw) -> None:
    """Converge the monthly digest thread: an OP (month-to-date headline,
    per-week bullets, plot) edited in place + one reply per scan (gcs template)
    or per UTC day (cw), in Slack (default) or its Discord twin (`-P discord`:
    webhook OP with the plot attached, bot-opened thread, webhook replies).
    State beside the snapshots, per the config's `state`/`discord_state`
    (gcs: digest/<YYYY-MM>.json; cw: digest/cw/<channel>/<variant>/<YYYY-MM>.json).
    See specs/digest-unification.md."""
    _digest(**kw)


@main.command("cw-digest")
@_digest_options("cw")
def cw_digest(**kw) -> None:
    """`digest -T cw` (kept so the cw job's invocation is unchanged)."""
    _digest(**kw)


if __name__ == "__main__":
    main()


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
@option("-a", "--all-gens", is_flag=True, help="Copy every `index/<gen>/` under the scan, pointed or not (no D1 read); default: only the generations a D1 `index_schema` row names")
@option("-b", "--bucket", "src_bucket", default=None, help="Source GCS scan store (default $DATA_BUCKET)")
@option("-l", "--layer2", default=None, help="Layer-2 dir template, `{scan}` = the scan id (default $LAYER2_PREFIX, else listing/{scan}/index/; cw: cw-l2/{scan}/)")
@option("-L", "--no-listings", is_flag=True, help="Leave the canonical per-bucket listings (`<layer-2 dir>/<bucket>.parquet`) in GCS only; copy the tiers + snapshot JSONs")
@option("-n", "--dry-run", is_flag=True, help="List the keys that would be copied; copy nothing")
@option("-p", "--prefix", "prefixes", multiple=True, help="Key prefix to publish (repeatable; default: the scan's served subset — snapshots/<subdir>/<scan>/ + the layer-2 dir)")
@option("-s", "--subdir", default=None, help="Snapshots subdir of this store (default $SNAPSHOTS_SUBDIR, else none)")
@option("-w", "--workers", default=8, type=int, help="Concurrent HEADs/uploads (default 8)")
@argument("scan")
def publish_r2(
    all_gens: bool,
    src_bucket: str | None,
    layer2: str | None,
    no_listings: bool,
    dry_run: bool,
    prefixes: tuple[str, ...],
    subdir: str | None,
    workers: int,
    scan: str,
) -> None:
    """Copy one scan's served artifacts GCS → R2.

    The final "publish to the serving cloud" stage of an ingest that builds
    against GCS: snapshot JSONs + the layer-2 dir (`index/<gen>/` tiers,
    `.groups.json` manifests, age pyramids; on cw also the canonical parquets).
    Idempotent — same size + md5 already in R2 is skipped — so it doubles as
    the backfill over old scans. R2 via the env: R2_ENDPOINT, R2_BUCKET,
    R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (`s3` extra).

    Only the generations D1 points at are copied (the site reads no other),
    so a reindex's superseded `index/<gen>/` never reaches R2; that reads D1
    (`D1_DB_ID` + `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID`). `-a`
    copies every generation without it.
    """
    from . import publish as pub

    pointed = None
    if not all_gens:
        from .index_footer import pointers

        pointed = {d for _, _, d in pointers()}
    pub.publish(
        scan,
        src_bucket=src_bucket or data_bucket(),
        prefixes=list(prefixes) or None,
        subdir=pub.SNAPSHOTS_SUBDIR if subdir is None else subdir,
        layer2=layer2 or pub.LAYER2_PREFIX,
        dry_run=dry_run,
        workers=workers,
        listings=not no_listings,
        pointed=pointed,
    )
