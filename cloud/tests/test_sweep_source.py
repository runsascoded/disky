"""Retirement must preserve the historical eligibility decision for a staged path."""
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from dt_cloud.sweep_source import review_source
from test_interval_generation import B, OFF, SCANS, _add, _build, world  # noqa: F401


def test_retired_review_sources_match_original_objects_across_closed_runs(world, tmp_path: Path) -> None:  # noqa: F811
    store, work = tmp_path / 'store', tmp_path / 'work'
    _build(world, store, work / 'build', OFF, 1)
    _add(world, store, work / 'add', OFF, len(SCANS) - 1)
    con = duckdb.connect()
    for date in [SCANS[0], SCANS[3], SCANS[-1]]:
        source = review_source(str(store), date, B, ['d/sub/'], ['d/rew.bin', 'd/old.bin'],
                               tmp_path / date / 'review.parquet', mem='512MB', threads=1)
        actual = pq.read_table(source).to_pylist()
        expected = con.execute(f"""SELECT substr(path, 3) AS path, size, mtime, kind
            FROM read_parquet('{world / OFF.format(id=date)}')
            WHERE kind = 'file' AND (starts_with(path, 'b/d/sub/') OR path IN ('b/d/rew.bin', 'b/d/old.bin')) ORDER BY path""").to_arrow_table().to_pylist()
        assert actual == expected
    empty = review_source(str(store), SCANS[-1], B, ['missing/'], [], tmp_path / 'empty.parquet', mem='512MB', threads=1)
    assert pq.read_table(empty).to_pylist() == []


def test_manifest_cli_uses_explicit_review_store_and_only_that_cutoff(monkeypatch, tmp_path: Path) -> None:
    import json
    from click.testing import CliRunner
    from dt_cloud.cli import plan_sweep_manifest
    from dt_cloud import sweep, sweep_source

    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'name': 'review', 'bucket': 'b', 'sweep': ['old/', 'current/'],
                                'as_of': {'old/': '2026-10-05', 'current/': '2026-10-12'}}) + '\n')
    seen = []

    def reconstruct(store, scan, bucket, prefixes, exact, out):
        seen.append((store, scan, bucket, prefixes, exact))
        return str(out)

    def manifest(l2, plan, out, date, l2_for):
        assert l2 == 'current.parquet'
        assert l2_for('2026-10-05') == str(Path(out) / 'review/2026-10-05.parquet')
        return {'objects': 1, 'bytes': 10, 'manifest': 'manifest.parquet'}

    monkeypatch.setattr(sweep_source, 'review_source', reconstruct)
    monkeypatch.setattr(sweep, 'build_manifest', manifest)
    result = CliRunner().invoke(plan_sweep_manifest, ['-I', 'r2://review-index', '-l', 'current.parquet',
                                                    '-d', '2026-10-12', '-o', str(tmp_path / 'out'), str(plan)], catch_exceptions=False)
    assert result.exit_code == 0
    assert seen == [('r2://review-index', '2026-10-05', 'b', ['old/'], [])]
    assert json.loads(result.stdout) == {'objects': 1, 'bytes': 10, 'manifest': 'manifest.parquet'}
