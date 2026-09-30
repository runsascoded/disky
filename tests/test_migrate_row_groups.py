"""`disk-tree migrate-row-groups [DIR|URL]` rewrites blobs with oversized row
groups in place — remote dirs included, streaming, so an R2-only scan written
before the 64K-row-group fix (spec `scan-page-r2-latency.md`) gets the layout
without a rescan or a local copy."""
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from pandas.testing import assert_frame_equal

from disk_tree import blobfs
from disk_tree.cli import cli
from disk_tree.storage.base import BLOB_ROW_GROUP_SIZE

N = BLOB_ROW_GROUP_SIZE + 4464


def _df() -> pd.DataFrame:
    return pd.DataFrame({'path': [f'f{i:06d}' for i in range(N)], 'size': range(N), 'depth': [1] * N})


def _write_one_group(d: str, name: str) -> str:
    """A blob the old writer would have produced: one 70K-row group."""
    path = blobfs.join(d, name)
    blobfs.write_table(pa.Table.from_pandas(_df(), preserve_index=False), path, row_group_size=1 << 20)
    return path


def _groups(path: str) -> list[int]:
    if blobfs.is_url(path):
        fs, p = blobfs.fs_for(path)
        md = pq.ParquetFile(p, filesystem=fs).metadata
    else:
        md = pq.read_metadata(path)
    return [md.row_group(i).num_rows for i in range(md.num_row_groups)]


@pytest.fixture(params=['local', 'memory'])
def scans_dir(request, tmp_path) -> str:
    if request.param == 'local':
        return str(tmp_path)
    pytest.importorskip('fsspec')
    return f'memory://{uuid4()}'


def test_rewrites_oversized_groups_in_place(scans_dir, monkeypatch):
    # `utz.err` is bound to the real stderr at import (invisible to CliRunner
    # and capfd), so collect the command's lines directly
    lines: list[str] = []
    monkeypatch.setattr('disk_tree.cli.migrate.err', lambda *a: lines.append(' '.join(map(str, a))))

    def stderr() -> str:
        out, lines[:] = '\n'.join(lines), []
        return out

    blob = _write_one_group(scans_dir, 'a.parquet')
    small = blobfs.join(scans_dir, 'b.parquet')
    blobfs.write_table(pa.Table.from_pandas(_df().head(10), preserve_index=False), small, row_group_size=BLOB_ROW_GROUP_SIZE)
    sidecar = blobfs.join(scans_dir, 'a.shallow.parquet')
    blobfs.write_table(pa.Table.from_pandas(_df().head(3), preserve_index=False), sidecar, row_group_size=1 << 20)
    assert _groups(blob) == [N]
    from disk_tree.find.groups import groups_path, write_groups_sidecar
    import json
    assert write_groups_sidecar(blob).n_groups == 1

    dry = CliRunner().invoke(cli, ['migrate-row-groups', '-n', scans_dir])
    assert dry.exit_code == 0, dry.output
    assert stderr().split('\n') == [
        f'a.parquet: {N:,} rows in 1 group(s), max {N:,}',
        f'1 blob(s) would be rewritten to ≤{BLOB_ROW_GROUP_SIZE:,}-row groups',
    ]
    assert _groups(blob) == [N]

    run = CliRunner().invoke(cli, ['migrate-row-groups', scans_dir])
    assert run.exit_code == 0, run.output
    assert stderr().split('\n') == [
        f'a.parquet: {N:,} rows in 1 group(s), max {N:,}',
        f'1 blob(s) rewritten to ≤{BLOB_ROW_GROUP_SIZE:,}-row groups',
    ]
    assert _groups(blob) == [BLOB_ROW_GROUP_SIZE, 4464]
    assert_frame_equal(blobfs.read_parquet(blob), _df())
    # the small blob and the sidecar are left alone; no temp file remains
    assert _groups(small) == [10]
    assert _groups(sidecar) == [3]
    assert blobfs.list_parquets(scans_dir) == ['a.parquet', 'b.parquet']
    assert not blobfs.exists(blob + '.rg.tmp')
    # the edge reader's footer sidecar now describes the new groups
    assert len(json.loads(blobfs.read_text(groups_path(blob)))['groups']) == 2

    again = CliRunner().invoke(cli, ['migrate-row-groups', scans_dir])
    assert again.exit_code == 0, again.output
    assert stderr() == f'0 blob(s) rewritten to ≤{BLOB_ROW_GROUP_SIZE:,}-row groups'
