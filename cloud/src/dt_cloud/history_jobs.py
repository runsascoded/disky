"""Immutable scan-thinning previews and candidate builds; no live pointer writes.

The serving store holds requests/status under history/jobs. Each build uses an
exact source fingerprint and a reviewed selection. Only new generation keys are
written. Development and production adoption are separate CAS operations.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Callable

import pyarrow.parquet as pq
from click import argument, group, option

from . import append_runner as ar
from . import interval_compact as ic
from . import interval_store as ist
from . import static_names as sn
from .blobstore import Store, upload_new
from .interval_backfill import mirror_source
from .scan_id import check_order, scan_epoch
from .scan_thinning import ChangeMetric, plan_geometric, plan_low_change, plan_manual, removal_candidates

PREFIX = 'history/jobs'
ID_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
RECORD = 'thinning.json'


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value: object) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def check_job_id(value: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError('invalid history job id')
    return value


def fingerprint(store: Store, prefix: str) -> dict:
    """Pin the base plus newest immutable manifest, not merely the CURRENT name."""
    gen = ar.current(store, prefix)
    if not gen:
        raise ValueError(f'{store.root}/{prefix}: no current generation')
    root = f'{prefix}/{gen}'
    scans = store.read_json(f'{root}/scans.json')
    manifest, runs = ar.store_newest_runs(store, root)
    manifest_doc = store.read_json(f'{root}/{manifest}') if manifest else None
    ids = check_order([s['id'] for s in scans['scans']] + ar.run_scans(runs))
    return {'gen': gen, 'base': digest(scans), 'manifest': manifest,
            'manifest_digest': digest(manifest_doc), 'scans': ids}


def source_state(data: Store, serving: Store) -> dict:
    return {'interval': fingerprint(serving, ist.PREFIX), 'static': fingerprint(data, sn.PREFIX)}


def assert_source(expected: dict, data: Store, serving: Store) -> None:
    if source_state(data, serving) != expected:
        raise ValueError('source changed since preview; request a new preview')


def transition_pins(entries: list[dict]) -> list[str]:
    """Preserve format boundaries and their preceding observation, including attribution coverage."""
    pins = set()
    for before, after in zip(entries, entries[1:]):
        if (before.get('version', 1), bool(before.get(ist.ATTRIBUTION_SRC))) != (after.get('version', 1), bool(after.get(ist.ATTRIBUTION_SRC))):
            pins.update((before['id'], after['id']))
    from .interval_churn import churn_from
    if entries:
        pins.add(churn_from(entries)['id'])
    return sorted(pins)


class ObjectChanges:
    """Exact object-state differences; source is a private, folded pv relation.

    Initial adjacent metrics come from one pass of version transitions. Greedy
    merged intervals compare endpoint states only for paths changing within the
    interval. Read time, owner-only changes and directory rollups do not score.
    """

    def __init__(self, con, files: list[str], scans: list[str]):
        self.con = con
        self.scans = scans
        names = pq.read_schema(files[0]).names
        self.columns = ['size', 'mtime', *ist.CLS, *[c for c in ('generation', 'version') if c in names]]
        cols = ', '.join(['depth', 'path', 'vf', 'vt', *self.columns])
        paths = '[' + ','.join(sn.q(f) for f in files) + ']'
        con.execute(f"CREATE OR REPLACE TABLE history_objects AS SELECT {cols} FROM read_parquet({paths}) WHERE kind = 'file' ORDER BY depth, path, vf")
        self.adjacent = self._adjacent()

    def _adjacent(self) -> dict[tuple[str, str], ChangeMetric]:
        state = ','.join(self.columns)
        prev = ','.join(f'lag({c}) OVER w' for c in self.columns)
        sql = f'''WITH versions AS (
          SELECT *, lag(vt) OVER w AS prev_end, lead(vf) OVER w AS next_start,
            row({prev}) AS prev_state, lag(size) OVER w AS prev_size
          FROM history_objects WINDOW w AS (PARTITION BY depth,path ORDER BY vf)
        ), events AS (
          SELECT vf AS t, 1 AS n,
            CASE WHEN prev_end = vf THEN abs(size - prev_size) ELSE size END AS bytes
          FROM versions WHERE prev_end IS DISTINCT FROM vf OR prev_state IS DISTINCT FROM row({state})
          UNION ALL
          SELECT vt, 1, size FROM versions WHERE next_start IS DISTINCT FROM vt
        ) SELECT t, sum(n)::BIGINT, sum(bytes)::BIGINT FROM events GROUP BY t'''
        counts = {int(t): ChangeMetric(int(n), int(b)) for t, n, b in self.con.execute(sql).fetchall()}
        return {(a, b): counts.get(scan_epoch(b), ChangeMetric(0, 0)) for a, b in zip(self.scans, self.scans[1:])}

    def __call__(self, before: str, after: str) -> ChangeMetric:
        if (before, after) in self.adjacent:
            return self.adjacent[before, after]
        a, b = scan_epoch(before), scan_epoch(after)
        state = lambda alias: 'row(' + ','.join(f'{alias}.{c}' for c in self.columns) + ')'
        sql = f'''WITH keys AS (
          SELECT DISTINCT depth,path FROM history_objects WHERE (vf > {a} AND vf <= {b}) OR (vt > {a} AND vt <= {b})
        ), a AS (
          SELECT o.* FROM history_objects o JOIN keys USING(depth,path) WHERE vf <= {a} AND {a} < vt
        ), b AS (
          SELECT o.* FROM history_objects o JOIN keys USING(depth,path) WHERE vf <= {b} AND {b} < vt
        ) SELECT count(*)::BIGINT, coalesce(sum(abs(coalesce(b.size,0)-coalesce(a.size,0))),0)::BIGINT
          FROM a FULL OUTER JOIN b USING(depth,path)
          WHERE a.path IS NULL OR b.path IS NULL OR {state('a')} IS DISTINCT FROM {state('b')}'''
        count, nbytes = self.con.execute(sql).fetchone()
        return ChangeMetric(int(count), int(nbytes))


def fold_for_plan(gen_dir: Path, out: Path, work: Path, *, threads: int, mem: str, progress: Callable[[str], None]) -> tuple[list[dict], list[str]]:
    """Fold canonical state for exact metrics/row estimates, without derived sorts."""
    base = json.loads((gen_dir / 'scans.json').read_text())
    _, runs = ar.newest_runs(gen_dir)
    entries = [*base['scans'], *ic.run_scans(gen_dir, '', runs)]
    ranges = json.loads((gen_dir / 'ranges.json').read_text())
    attributed = ist.attributed(base) or any(ist.ATTRIBUTION in json.loads((gen_dir / run['key'] / 'meta.json').read_text()) for run in runs)
    con = sn.connect(threads, mem, work / 'duckdb')
    try:
        for r in ranges['ranges']:
            name = f"r{r['i']:04d}"
            ic.compact_range(con, str(gen_dir), [str(gen_dir / r['key']) for r in runs], name, out,
                             sliced=ist.sliced(base), generation=ist.has_generation(base), attributed=attributed, churn=False)
            progress(f"folded object states {r['i'] + 1}/{ranges['k']}")
    finally:
        con.close()
    return entries, [str(f) for f in sorted((out / 'pv').glob('*.parquet'))]


def estimate_retention(con, folded: Path, retained: list[str], *, progress: Callable[[str], None] = sn.err) -> dict:
    """Exact projected canonical row counts; deliberately rough compressed bytes.

    This measures private folded interval files without churn, not the live
    generations' complete physical footprint. Keep each file's source metadata
    overhead fixed and scale its compressed data bytes by projected row count.
    Compression and layout can change; full candidate inventories are measured
    separately after a build. No derived/static/mirror savings are inferred.
    """
    from .interval_thinning import TABLES, project_sql

    stamps = [scan_epoch(scan) for scan in check_order(retained)]
    tables = {}
    for table in TABLES:
        files = sorted((folded / table).glob('r*.parquet'))
        if not files:
            continue
        totals = {'ranges': len(files), 'source_rows': 0, 'retained_rows': 0, 'source_bytes': 0, 'estimated_retained_bytes': 0, 'estimated_saved_bytes': 0}
        for path in files:
            progress(f'estimating retained canonical rows {table}/{path.name}')
            footer = pq.read_metadata(path)
            schema = footer.schema.to_arrow_schema()
            columns = [name for name in schema.names if name not in ('churn_cum', 'churn_end')]
            sql = project_sql(f'SELECT * FROM read_parquet({sn.q(str(path))})', columns, stamps,
                              key=('depth', 'path', 'usr') if 'usr' in columns else ('depth', 'path'))
            kept = int(con.execute(f'SELECT count(*) FROM ({sql})').fetchone()[0])
            before, size = footer.num_rows, path.stat().st_size
            if kept > before:
                raise ValueError(f'{path}: projection increased canonical row count')
            compressed = min(size, sum(footer.row_group(i).column(j).total_compressed_size
                                       for i in range(footer.num_row_groups) for j in range(footer.num_columns)))
            after_bytes = size - compressed + (compressed * kept + before // 2) // before if before else size
            totals['source_rows'] += before
            totals['retained_rows'] += kept
            totals['source_bytes'] += size
            totals['estimated_retained_bytes'] += after_bytes
        totals['estimated_saved_bytes'] = totals['source_bytes'] - totals['estimated_retained_bytes']
        tables[table] = totals
    if not tables:
        raise ValueError('no folded canonical range files to estimate')
    return {
        'method': 'projected-rows-proportional-compressed-data',
        'scope': 'folded-interval-canonical-tables',
        'basis': 'private folded canonical files without churn',
        **{field: sum(table[field] for table in tables.values()) for field in (
            'source_rows', 'retained_rows', 'source_bytes', 'estimated_retained_bytes', 'estimated_saved_bytes',
        )},
        'exact_rows': True,
        'estimated_bytes': True,
        'set_level': True,
        'note': 'Estimate applies to this whole retained set. Per-scan savings are not additive; payload-change bytes are not stored-index bytes.',
        'tables': tables,
        'exclusions': [
            'Static name/search generations and their derived artifacts.',
            'Interval served sorts, sidecars and generation metadata.',
            'Churn recomputation and its additional interval splits.',
            'Mirrored copies, retained predecessor generations and job working space.',
            'Changes to compression, footer size and physical layout after rebuilding.',
        ],
    }


def make_plan(request: dict, data: Store, serving: Store, work: Path, *, threads: int = 8, mem: str = '12GB',
              progress: Callable[[str], None] = sn.err) -> dict:
    state = source_state(data, serving)
    scans = state['interval']['scans']
    if scans != state['static']['scans']:
        raise ValueError('interval and static scan rosters differ; catch up the stores before thinning')
    gen = state['interval']['gen']
    local = mirror_source(serving, gen, f'{gen}.history-preview', work / 'source')
    entries, files = fold_for_plan(local, work / 'folded', work, threads=threads, mem=mem, progress=progress)
    required = sorted(set(request.get('required_pins', [])) | set(transition_pins(entries)))
    pins = sorted(set(request.get('pinned', [])) | set(required))
    policy = request['policy']
    con = sn.connect(threads, mem, work / 'duckdb-plan')
    try:
        oracle = ObjectChanges(con, files, scans)
        if policy['kind'] == 'low-change':
            plan = plan_low_change(scans, oracle, drop_count=policy['drop_count'], pinned=pins)
        elif policy['kind'] == 'manual':
            plan = plan_manual(scans, oracle, remove=policy['remove'], pinned=pins)
        elif policy['kind'] == 'geometric':
            plan = plan_geometric(scans, recent_seconds=policy['recent_seconds'], growth=policy.get('growth', 2),
                                  samples_per_band=policy.get('samples_per_band', 1), pinned=pins)
        else:
            raise ValueError('unknown thinning policy')
        candidates = [asdict(candidate) for candidate in removal_candidates(scans, oracle, pinned=pins)]
        for candidate in candidates:
            if candidate['scan'] in required:
                candidate['reasons'] = (*candidate['reasons'], 'required-pin')
        estimate = estimate_retention(con, work / 'folded', list(plan.retained), progress=progress)
    finally:
        con.close()
    assert_source(state, data, serving)
    result = {**asdict(plan), 'policy_options': policy, 'source': state, 'required_pins': required,
              'candidates': candidates, 'estimate': estimate,
              'removed': list(plan.removed), 'churn': 'retained-snapshots',
              'metric': {'count': 'objects added, removed or edited (size, mtime, class, known identity)',
                         'bytes': 'sum of absolute endpoint size differences; same-size edits count once'}}
    result = json.loads(canonical(result))
    result['digest'] = digest(result)
    return result


def validate_plan(plan: dict, required: list[str]) -> None:
    if not isinstance(plan, dict) or plan.get('digest') != digest({k: v for k, v in plan.items() if k != 'digest'}):
        raise ValueError('plan digest mismatch')
    scans, retained = check_order(plan['scans']), check_order(plan['retained'])
    if len(scans) != len(plan['scans']) or len(retained) != len(plan['retained']) or not retained:
        raise ValueError('invalid plan roster')
    if not set(retained) <= set(scans) or not {scans[0], scans[-1], *required, *plan['required_pins'], *plan.get('pinned', [])} <= set(retained):
        raise ValueError('plan removes a protected scan')
    if set(plan['removed']) != set(scans) - set(retained):
        raise ValueError('plan removed/retained partition mismatch')


def _strip_churn(con, path: Path) -> None:
    from .interval_churn import CHURN, CHURN_END
    schema = pq.read_schema(path)
    cols = [field for field in schema if field.name not in (CHURN, CHURN_END)]
    if len(cols) == len(schema):
        return
    import pyarrow as pa
    schema = pa.schema(cols, metadata=schema.metadata)
    tmp = path.with_suffix('.without-churn.parquet')
    sql = f"SELECT {', '.join(schema.names)} FROM read_parquet({sn.q(str(path))}) ORDER BY depth,path,vf"
    sn.write_sorted(sn._batches(con, sql), tmp, schema, ist.RANGE_RG, dictionary=['kind', 'us'])
    tmp.replace(path)


def inventory(local: Path) -> dict[str, int]:
    return {p.relative_to(local).as_posix(): p.stat().st_size for p in sorted(local.rglob('*')) if p.is_file()}


def storage_report(sources: dict[str, dict], candidates: dict[str, dict]) -> dict:
    """Measured generation inventories; a reader switch does not release sources."""
    if set(sources) != set(candidates):
        raise ValueError('source and candidate physical-copy inventories differ')
    copies = {}
    for copy, source in sources.items():
        candidate = candidates[copy]
        copies[copy] = {
            'source_files': source['files'],
            'candidate_files': candidate['files'],
            'source_bytes': source['bytes'],
            'candidate_bytes': candidate['bytes'],
            'additional_generation_bytes_before_gc': candidate['bytes'],
            'net_reduction_after_source_gc_bytes': source['bytes'] - candidate['bytes'],
        }
    before = sum(source['bytes'] for source in sources.values())
    after = sum(candidate['bytes'] for candidate in candidates.values())
    return {
        'method': 'measured-generation-inventories',
        'scope': 'source-and-candidate-generation-copies',
        'source_bytes': before,
        'candidate_bytes': after,
        'additional_generation_bytes_before_gc': after,
        'net_reduction_after_source_gc_bytes': before - after,
        'potential_source_reclaim_bytes': before,
        'copies': copies,
        'reclaim_condition': 'No space is freed by building or reader adoption. Sources remain until writer references, reader/version pins and retention rules release them and GC actually deletes them. Reader adoption does not switch the writer or retire its source.',
        'exclusions': ['Job working space and temporary files.', 'History job/version metadata outside generation prefixes.', 'Other generations and storage-system retention beyond these measured copies.'],
    }


def upload_candidate(store: Store, local: Path, prefix: str, order, tier) -> dict:
    """A single-writer reserved candidate, data before its liveness markers."""
    existing = store.listing(prefix)
    if set(existing) != {f'{prefix}/{ar.CANDIDATE}'}:
        raise FileExistsError(f'{prefix}: candidate reservation changed')
    upload_new(store, local, prefix, {}, order, tier=tier)
    expected = {f'{prefix}/{k}': n for k, n in inventory(local).items()}
    expected[f'{prefix}/{ar.CANDIDATE}'] = existing[f'{prefix}/{ar.CANDIDATE}']
    actual = store.listing(prefix)
    if actual != expected:
        raise ValueError(f'{prefix}: uploaded candidate inventory mismatch')
    return {'files': len(expected), 'bytes': sum(expected.values()), 'inventory_digest': digest(expected)}


def build_candidate(plan: dict, data: Store, serving: Store, work: Path, *, job_id: str, threads: int = 8,
                    mem: str = '12GB', progress: Callable[[str], None] = sn.err) -> dict:
    """Build immutable bases and a release descriptor, never CURRENT/successor.

    The GCS create-only job reservation gives one writer ownership of the new
    keys. Interrupted output is deliberately not overwritten; a fresh build job
    receives fresh generation names. All source-state comparisons happen again
    before publishing the ready descriptor.
    """
    from .blobstore import mirror
    from .history_verify import verify_ranges, verify_static
    from .interval_churn import churn_from, churn_gen, has_churn, record_churn
    from .interval_local import publish_order as iv_order, publish_tier as iv_tier
    from .interval_thinning import TABLES, project_ranges
    from .static_local import publish_order as st_order, publish_tier as st_tier
    from .static_thinning import fold_thinned

    validate_plan(plan, [])
    check_job_id(job_id)
    assert_source(plan['source'], data, serving)
    data.create(f'history/builds/{job_id}.json', canonical({'plan_digest': plan['digest'], 'id': job_id}) + '\n')
    iv_old, st_old = (plan['source'][kind]['gen'] for kind in ('interval', 'static'))
    iv_new, st_new = f'{iv_old}.thin-{job_id}', f'{st_old}.thin-{job_id}'
    iv_prefix, st_prefix = f'{ist.PREFIX}/{iv_new}', f'{sn.PREFIX}/{st_new}'
    source_copies = {}
    for copy, store, prefix in (
        ('interval_serving', serving, f'{ist.PREFIX}/{iv_old}'),
        ('static_data', data, f'{sn.PREFIX}/{st_old}'),
        ('static_serving', serving, f'{sn.PREFIX}/{st_old}'),
    ):
        existing = store.listing(prefix)
        source_copies[copy] = {'files': len(existing), 'bytes': sum(existing.values())}
    for store, prefix in ((serving, iv_prefix), (serving, st_prefix), (data, st_prefix)):
        if store.listing(prefix):
            raise FileExistsError(f'{prefix}: immutable candidate already exists')
    progress('reading source interval ranges')
    iv_source = mirror_source(serving, iv_old, iv_new, work / 'iv-source')
    progress('reading source static canonical intervals and settings')
    st_source = work / 'static-source'
    # The static rebuild reads canonical deltas and settings, never old derived
    # suffix/catalog/drill data. Keep settings and per-scan docs only.
    def static_inputs(rel: str) -> bool:
        if rel.startswith(('sx/', 'names/', 'anchors/', 'drill/')):
            return rel.endswith('meta.json')
        return not rel.endswith('.parquet') or rel.split('/')[0] in ('cintervals', 'cdelta') or '/cdelta/' in rel
    mirror(data, f'{sn.PREFIX}/{st_old}', st_source, keep=static_inputs)
    assert_source(plan['source'], data, serving)
    for store, prefix, old in ((serving, iv_prefix, iv_old), (serving, st_prefix, st_old), (data, st_prefix, st_old)):
        store.create(f'{prefix}/{ar.CANDIDATE}', canonical({'from': old, 'history_job': job_id, 'plan_digest': plan['digest']}) + '\n')
    iv_out, st_out = work / 'candidate-interval', work / 'candidate-static'
    reference = work / 'reference-interval'
    retained = set(plan['retained'])
    projection = {}

    def before_cut(con, folded: Path, doc: dict) -> None:
        con.execute('SET parquet_metadata_cache = false')
        con.execute('SET enable_external_file_cache = false')
        # Hard links preserve the original private fold while projected writers
        # replace their paths atomically. No source generation is touched.
        for table in TABLES:
            for path in sorted((folded / table).glob('r*.parquet')):
                dest = reference / table / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.link(path, dest)
        doc['scans'] = [entry for entry in doc['scans'] if entry['id'] in retained]
        for path in sorted((folded / 'pvl').glob('r*.parquet')):
            _strip_churn(con, path)
        progress('projecting interval bounds onto retained observations')
        projection.update(project_ranges(con, folded, [entry['ts'] for entry in doc['scans']], rg_rows=ist.RANGE_RG))
        if has_churn(doc):
            progress('recalculating churn over retained observations')
            k = json.loads((folded / 'ranges.json').read_text())['k']
            churn_gen(con, folded, k, churn_from(doc['scans'])['ts'], work / 'resampled-churn')
            doc.update(record_churn(doc))
            doc['churn_basis'] = 'retained-observations'
        doc['history'] = {'version': job_id, 'plan_digest': plan['digest'], 'removed': plan['removed']}
        progress('verifying exact retained interval snapshots')
        projection['verification'] = verify_ranges(con, reference, folded, plan['retained'])

    progress('folding interval generation')
    _, iv_runs = ar.newest_runs(iv_source)
    doc, rows, sorts, stats = ic.fold_gen(iv_source, iv_out, work / 'iv-fold', gen=iv_old, runs=iv_runs,
                                          threads=threads, mem=mem, before_cut=before_cut)
    rows = {table: sum(pq.read_metadata(path).num_rows for path in (iv_out / table).glob('r*.parquet')) for table in rows}
    progress('rebuilding static search generation')
    _, st_runs = ar.newest_runs(st_source)
    static_work = work / 'static-fold'
    static = fold_thinned(st_source, st_out, static_work, gen=st_old, new_gen=st_new, runs=st_runs,
                          retained=plan['retained'], threads=threads, mem=mem)
    con = sn.connect(threads, mem, work / 'duckdb-verify-static')
    try:
        progress('verifying exact retained static snapshots')
        static_check = verify_static(con, static_work / 'static-thinning' / 'folded', st_out, plan['retained'])
    finally:
        con.close()
    assert_source(plan['source'], data, serving)
    rec = {'from': iv_old, 'source': plan['source']['interval'], 'retained': plan['retained'], 'removed': plan['removed'],
           'plan_digest': plan['digest'], 'churn_basis': 'retained-observations', 'projection': projection}
    (iv_out / RECORD).write_text(json.dumps(rec, indent=1) + '\n')
    progress('publishing new immutable candidate generations')
    copies = {
        'interval_serving': upload_candidate(serving, iv_out, iv_prefix, iv_order, iv_tier),
        'static_data': upload_candidate(data, st_out, st_prefix, st_order, st_tier),
        'static_serving': upload_candidate(serving, st_out, st_prefix, st_order, st_tier),
    }
    storage = storage_report(source_copies, copies)
    assert_source(plan['source'], data, serving)
    version = {'id': job_id, 'store': 'primary', 'interval_gen': iv_new, 'static_gen': st_new,
               'retained': plan['retained'], 'removed': sorted(plan['removed']), 'plan_digest': plan['digest'],
               'source': plan['source'], 'created_at': time(), 'churn_basis': 'retained-observations',
               'verified': {'interval': True, 'static': True, 'inventory': True},
               'verification': {'interval': projection['verification'], 'static': static_check, 'copies': copies},
               'storage': storage,
               'immutable_snapshot': True}
    serving.create(f'history/versions/{job_id}.json', json.dumps(version, indent=1) + '\n')
    return {'version': job_id, 'descriptor': version, 'interval': {'rows': rows, 'sorts': sorts, 'stats': stats}, 'static': static, 'storage': storage}


def run_job(serving: Store, job_id: str, work: Path, *, threads: int = 8, mem: str = '12GB') -> dict:
    key = f'{PREFIX}/{check_job_id(job_id)}'
    req = serving.read_json(f'{key}/request.json')
    if serving.exists(f'{key}/status.json'):
        old = serving.read_json(f'{key}/status.json')
        if old.get('state') in ('planned', 'built'):
            return old
    data = Store(os.environ['HISTORY_DATA_STORE'])
    started = time()
    work.mkdir(parents=True, exist_ok=True)

    def status(state: str, **extra) -> dict:
        doc = {'id': job_id, 'state': state, 'started': started, 'updated': time(), **extra}
        serving.write_text(f'{key}/status.json', json.dumps(doc, indent=1) + '\n')
        return doc

    def progress(note: str) -> None:
        sn.err(note)
        status('running', phase=note)

    try:
        if req['action'] == 'plan':
            progress('planning: reading source versions')
            plan = make_plan(req, data, serving, work, threads=threads, mem=mem, progress=progress)
            return status('planned', plan=plan)
        if req['action'] == 'build':
            plan = req['plan']
            validate_plan(plan, req.get('required_pins', []))
            assert_source(plan['source'], data, serving)
            candidate = build_candidate(plan, data, serving, work, job_id=job_id, threads=threads, mem=mem, progress=progress)
            return status('built', plan=plan, candidate=candidate)
        raise ValueError('unknown history action')
    except BaseException as exc:
        status('failed', error=f'{type(exc).__name__}: {exc}')
        raise


@group('history')
def cli() -> None:
    """Preview scan thinning and build immutable candidates. Never moves CURRENT."""


@cli.command('run')
@option('-i', '--job-id', required=True)
@option('-m', '--mem', default='12GB', show_default=True)
@option('-r', '--serving-store', required=True)
@option('-t', '--threads', default=8, type=int, show_default=True)
@option('-w', '--work', required=True, type=Path)
def run_cmd(job_id: str, mem: str, serving_store: str, threads: int, work: Path) -> None:
    print(json.dumps(run_job(Store(serving_store), job_id, work, threads=threads, mem=mem), indent=1))
