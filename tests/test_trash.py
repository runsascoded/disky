"""Specs for `disk_tree.trash`: rename into a per-run trash dir, an exact
manifest, restore, empty, TTL expiry — over a tmp trash root."""
from __future__ import annotations

import json
import os

import pytest

from disk_tree import trash
from disk_tree.trash import Restored, TrashedRun


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("DISK_TREE_TRASH_DIR", str(tmp_path / "trash"))
    return tmp_path


def _mk(root, rel, content="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_local_path_strips_scheme_and_trailing_slash():
    assert [trash.local_path(u) for u in ["file:///Users/x/", "file:///Users/x", "/Users/x/", "/"]] == [
        "/Users/x", "/Users/x", "/Users/x", "/",
    ]


def test_trash_renames_into_the_run_dir_and_records_a_manifest(root):
    f = _mk(root, "home/a/b.txt", "hello")
    d = _mk(root, "home/a/dir/c.txt").parent

    where_f = trash.trash(f"file://{f}/", "laptop-real-1", now=lambda: 1000)
    where_d = trash.trash(str(d), "laptop-real-1", now=lambda: 1001)

    run = root / "trash" / "laptop-real-1"
    assert where_f == str(run / str(f).lstrip("/"))
    assert where_d == str(run / str(d).lstrip("/"))
    assert not f.exists() and not d.exists()
    assert (run / str(f).lstrip("/")).read_text() == "hello"
    assert (run / str(d).lstrip("/") / "c.txt").read_text() == "x"
    assert trash.manifest("laptop-real-1") == [
        {"path": str(f), "trashed": where_f, "ts": 1000},
        {"path": str(d), "trashed": where_d, "ts": 1001},
    ]


def test_trash_missing_path_raises(root):
    with pytest.raises(FileNotFoundError):
        trash.trash(str(root / "nope"), "r")


def test_run_dir_flattens_slashes_in_run_ids(root):
    assert trash.run_dir("plan-2026-09-29/20260929T2200") == root / "trash" / "plan-2026-09-29__20260929T2200"


def test_restore_puts_paths_back_and_skips_reoccupied_ones(root):
    f = _mk(root, "home/a/b.txt", "hello")
    g = _mk(root, "home/a/g.txt", "g")
    trash.trash(str(f), "r1", now=lambda: 1)
    trash.trash(str(g), "r1", now=lambda: 2)
    _mk(root, "home/a/g.txt", "new g")   # reoccupied since

    assert trash.restore("r1") == Restored(restored=[str(f)], skipped=[str(g)])
    assert f.read_text() == "hello"
    assert g.read_text() == "new g"
    # the skipped one stays in the trash, and stays in the manifest
    assert trash.manifest("r1") == [{"path": str(g), "trashed": str(root / "trash" / "r1" / str(g).lstrip("/")), "ts": 2}]
    assert trash.restore("r1") == Restored(restored=[], skipped=[str(g)])


def test_empty_frees_the_run_dir_and_reports_bytes_and_files(root):
    f = _mk(root, "home/a/b.txt", "hello")
    trash.trash(str(f), "r1")
    nbytes, files = trash.empty("r1")
    assert (files, nbytes > 0) == (1, True)
    assert not (root / "trash" / "r1").exists()
    assert trash.empty("r1") == (0, 0)
    assert trash.manifest("r1") == []


def test_runs_and_expired(root):
    a = _mk(root, "home/a.txt")
    b = _mk(root, "home/b.txt")
    trash.trash(str(a), "old", now=lambda: 100)
    trash.trash(str(b), "new", now=lambda: 900)
    rs = trash.runs()
    assert [(r.run_id, r.ts, r.files) for r in rs] == [("old", 100, 1), ("new", 900, 1)]
    assert [r.run_id for r in trash.expired(ttl_s=500, now=lambda: 1000)] == ["old"]
    assert trash.expired(ttl_s=500, now=lambda: 300) == []


def test_trash_root_default_is_the_finder_trash(monkeypatch):
    monkeypatch.delenv("DISK_TREE_TRASH_DIR", raising=False)
    assert str(trash.trash_root()) == os.path.expanduser("~/.Trash/disk-tree")
