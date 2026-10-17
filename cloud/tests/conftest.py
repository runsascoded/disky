"""Every test runs as a deployment with a neutral data bucket and site
(`dt_cloud.deploy`): dt-cloud has no default of its own. A test of the unset
case removes them with `monkeypatch.delenv`. The layer-2 template and the
static-names profile start unset. No cost labels (`DISKY_LABELS`)
leak in from the shell: a test that wants them sets them.

A test against the D1 schema takes `d1_db`: a fresh SQLite holding the branch's
schema (`site/schema.sql`), foreign keys ON (as D1 enforces them).

The engine's root (`$DISKY_ROOT`) is a throwaway dir, set before anything imports `disk_tree` (its `config` binds
paths at import), so no test records a Scan or writes a blob in the user's `~/.config/disk-tree`."""
import sqlite3
from os import environ
from pathlib import Path
from tempfile import mkdtemp

import pytest

environ['DISKY_ROOT'] = mkdtemp(prefix='disky-test-root-')

from dt_cloud.d1_schema import replay, schema_file  # noqa: E402 (after the root is set)


@pytest.fixture
def d1_db() -> sqlite3.Connection:
    return replay([schema_file().read_text()])


@pytest.fixture(autouse=True)
def _deployment_env(monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", "my-data")
    monkeypatch.setenv("SITE_URL", "https://site.example.org")
    monkeypatch.delenv("DISKY_LABELS", raising=False)
    # Deployment config the generic code requires (no defaults): unset unless a test sets it.
    for v in ("LAYER2_PREFIX", "STATIC_NAMES_PROFILE", "STATIC_NAMES_GEN", "SCAN_RUN_ID", "SCAN_ID"):
        monkeypatch.delenv(v, raising=False)
    # the cw digest's scan triggers come from the site (`/api/scan-runs`): none recorded, unless a test says otherwise
    from dt_cloud import digest_cw

    monkeypatch.setattr(digest_cw, "site_triggers", lambda cfg: {})


@pytest.fixture
def fake_r2(monkeypatch, tmp_path) -> Path:
    """`r2://<bucket>/<key>` resolved by `disk_tree.blobfs` exactly as in a
    deployment (scheme → endpoint from `DISK_TREE_R2_ENDPOINT_URL` → its
    s3fs filesystem), with that filesystem swapped for a local dir: object
    `<bucket>/<key>` lives at `<returned dir>/<bucket>/<key>`. No network."""
    from fsspec.implementations.dirfs import DirFileSystem
    from fsspec.implementations.local import LocalFileSystem

    from disk_tree import blobfs

    root = tmp_path / "r2"
    root.mkdir()
    monkeypatch.setenv(blobfs.R2_ENDPOINT_VAR, "https://acct.r2.example")
    monkeypatch.setattr(blobfs, "_known", set())

    def fake_s3fs(endpoint_url: str, profile: str | None = None) -> DirFileSystem:
        assert endpoint_url == "https://acct.r2.example"
        return DirFileSystem(path=str(root), fs=LocalFileSystem(auto_mkdir=True))

    monkeypatch.setattr(blobfs, "_s3fs", fake_s3fs)
    return root
