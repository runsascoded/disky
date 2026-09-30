"""End-to-end `disk-tree digest --dry-run`: config → scan source → profile →
plot + OP body, without posting.

Imports one bucket (``gcs://b1``) at three dates with TiB-scale totals
(3000 / 3030 / 3010), then a `digest:` block drives the reference bytes profile.
The dry run prints the OP body — asserted exactly — and renders the plot."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

TIB = 1024**4


def _run_dt(env_root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DISK_TREE_ROOT": str(env_root)}
    return subprocess.run(
        [sys.executable, "-m", "disk_tree.cli.main", *args],
        env=env, capture_output=True, text=True, check=False,
    )


def _import(env_root: Path, tmp_path: Path, tib: float, when: str) -> None:
    listing = tmp_path / f"l-{when[:10]}.parquet"
    pd.DataFrame([
        {"bucket": "b1", "name": "f", "size_bytes": round(tib * TIB), "created": pd.Timestamp(when), "storage_class_id": 1},
    ]).to_parquet(listing)
    r = _run_dt(env_root, "import", "-l", str(listing), "-b", "b1", "-t", when)
    assert r.returncode == 0, r.stderr


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    """A root with three dated scans of gcs://b1 + a `digest:` block."""
    env_root = tmp_path / "root"
    env_root.mkdir()
    # July 31 lead-in, then two August scans (+30, −20)
    _import(env_root, tmp_path, 3000, "2026-07-31T12:00:00+00:00")
    _import(env_root, tmp_path, 3030, "2026-08-03T12:00:00+00:00")
    _import(env_root, tmp_path, 3010, "2026-08-04T12:00:00+00:00")
    (env_root / "buckets.yml").write_text(
        "buckets:\n"
        "  - uri: gcs://b1\n"
        "    digest:\n"
        "      profile: bytes\n"
        "      period: month\n"
        "      site_url: https://disk-tree.example\n"
        "      icons_base: https://disk-tree.example/icons\n"
    )
    return env_root


def test_digest_dry_run_prints_op_body(configured: Path):
    r = _run_dt(configured, "digest", "gcs://b1", "-m", "2026-08", "-n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.rstrip("\n").split("\n") == [
        ":arrow_deg20: **+10.0 TiB** month-to-date · [dashboard](https://disk-tree.example/)",
        "",
        "*Weekly summaries*",
        ":arrow_deg0: [wk of 8/3](https://disk-tree.example/?d=260804-2d#over-time) _(partial)_ — **3,010 TiB** (+10.0, 0.3%)",
    ]


def test_digest_dry_run_no_scans_exits_nonzero(configured: Path):
    r = _run_dt(configured, "digest", "gcs://b1", "-m", "2026-05", "-n")
    assert r.returncode != 0
    assert "no scans" in r.stderr


def test_digest_unknown_bucket_exits_nonzero(configured: Path):
    r = _run_dt(configured, "digest", "nope", "-n")
    assert r.returncode != 0
    assert "matched 0 configured buckets" in r.stderr
