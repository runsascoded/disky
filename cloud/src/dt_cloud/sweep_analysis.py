"""Reconstruct legacy run review omissions without changing execution evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import file_digest
from pathlib import Path
import json

import duckdb
from click import argument, command, option

from .blobstore import Store
from .interval_export import store_gen
from .sweep import load_plan
from .sweep_source import review_source


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def classify_review(
    current: Path,
    reviewed: Path,
    manifest: Path,
    prefix: str,
) -> dict:
    """Verify the legacy mtime eligibility set, then classify its complement.

    The executor did not record names outside the manifest, so these are
    manifest-scan classifications, not reconstructed execution observations.
    """
    with duckdb.connect() as con:
        con.execute('CREATE TABLE current AS SELECT path, size, mtime FROM read_parquet(?) WHERE kind = \'file\' AND starts_with(path, ?)', [str(current), prefix])
        con.execute('CREATE TABLE reviewed AS SELECT path, mtime FROM read_parquet(?) WHERE kind = \'file\' AND starts_with(path, ?)', [str(reviewed), prefix])
        con.execute('CREATE TABLE manifest AS SELECT name AS path, size_bytes AS size, mtime FROM read_parquet(?) WHERE starts_with(name, ?)', [str(manifest), prefix])
        con.execute('''CREATE TABLE expected AS SELECT * FROM current c
            WHERE EXISTS (SELECT 1 FROM reviewed r WHERE r.path = c.path AND r.mtime = c.mtime)''')
        mismatch = con.execute('''SELECT count(*) FROM (
            (SELECT * FROM expected EXCEPT ALL SELECT * FROM manifest)
            UNION ALL (SELECT * FROM manifest EXCEPT ALL SELECT * FROM expected))''').fetchone()[0]
        if mismatch:
            raise ValueError(f'{prefix}: reconstructed eligibility differs from the original manifest ({mismatch} rows)')
        con.execute('''CREATE TABLE omissions AS SELECT c.*, CASE WHEN EXISTS
            (SELECT 1 FROM reviewed r WHERE r.path = c.path) THEN 'changed_since_review'
            ELSE 'new_since_review' END AS reason FROM current c
            WHERE NOT EXISTS (SELECT 1 FROM expected e WHERE e.path = c.path)''')
        counts = con.execute('''SELECT count(*), coalesce(sum(size), 0),
            count(*) FILTER (WHERE reason = 'new_since_review'),
            count(*) FILTER (WHERE reason = 'changed_since_review') FROM omissions''').fetchone()
        keys = con.execute('SELECT path, reason FROM omissions ORDER BY path').fetchall()
        return {'objects': counts[0], 'bytes': counts[1], 'new_objects': counts[2], 'changed_objects': counts[3],
                'keys': [{'name': name, 'reason': reason} for name, reason in keys]}


def analyze_preserved(
    run_root: str,
    scan: str,
    interval_store: str,
    out: Path,
) -> dict:
    """Write supplemental review analytics locally; source run artifacts stay intact."""
    if (out / 'preservation-analysis.json').exists():
        raise FileExistsError(out / 'preservation-analysis.json')
    run = Store(run_root)
    inputs = out / 'inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    for name in ('plan.json', 'plan-summary.json', 'deleted-summary.json'):
        run.get(name, inputs / name)
    plan = load_plan(inputs / 'plan.json')
    summary = json.loads((inputs / 'plan-summary.json').read_text())
    result = json.loads((inputs / 'deleted-summary.json').read_text())
    if result['mode'] != 'real' or result['delete_failed']:
        raise ValueError('backfill requires a completed real run with zero failed deletions')
    if plan.objects:
        raise ValueError('legacy backfill currently supports prefix plans only')
    manifest = inputs / 'manifest.parquet'
    run.get(f'manifest/{plan.bucket}.parquet', manifest)
    gen = store_gen(Store(interval_store), None)
    bands = [b for b in result['bands'] if b['drift_new']]
    prefixes = [b['prefix'] for b in bands]
    if any(p not in plan.sweep for p in prefixes):
        raise ValueError('result contains a preserved prefix outside the original plan')
    exports = {}
    for date in sorted({scan, *(plan.as_of.get(p, scan) for p in prefixes)}):
        path = inputs / f'{date}.parquet'
        review_source(interval_store, date, plan.bucket, prefixes, [], path, gen=gen)
        exports[date] = path
    analyzed = []
    for band in bands:
        prefix = band['prefix']
        cutoff = plan.as_of.get(prefix, scan)
        classification = classify_review(exports[scan], exports[cutoff], manifest, prefix)
        analyzed.append({'prefix': prefix, 'as_of': cutoff, **classification,
                         'execution_outside_review': band['drift_new']})
    total = {key: sum(b[key] for b in analyzed) for key in ('objects', 'bytes', 'new_objects', 'changed_objects')}
    expected = summary['skipped_after_as_of']
    if total['objects'] != expected['objects'] or total['bytes'] != expected['bytes']:
        raise ValueError('reconstructed omissions differ from the original plan-summary totals')
    doc = {
        'version': 1,
        'method': 'review-snapshots',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'run': run_root,
        'scan': scan,
        'bucket': plan.bucket,
        'interval_store': interval_store,
        'interval_generation': gen,
        'review_omissions': total,
        'prefixes': analyzed,
        'execution': {'outside_review': result['drift_new'], 'keys_recorded': False},
        'input_sha256': {p.name: digest(p) for p in sorted(inputs.iterdir()) if p.is_file()},
    }
    (out / 'preservation-analysis.json').write_text(json.dumps(doc, indent=2) + '\n')
    return doc


@command('analyze-preserved')
@option('-d', '--date', required=True, help='Original manifest scan id')
@option('-I', '--interval-store', required=True, help='Historical review source (read only)')
@option('-o', '--out', required=True, type=Path, help='New local analysis directory; original run artifacts are untouched')
@argument('run_root')
def analyze_preserved_cmd(date: str, interval_store: str, out: Path, run_root: str) -> None:
    """Backfill review omissions for a legacy real RUN_ROOT (local dir or URL)."""
    doc = analyze_preserved(run_root, date, interval_store, out)
    print(json.dumps(doc))
