"""Every test runs as a deployment with a neutral data bucket and site
(`dt_cloud.deploy`): dt-cloud has no default of its own. A test of the unset
case removes them with `monkeypatch.delenv`. The layer-2 template and the
static-names profile start unset. Cost labels are required (specs/done/cost-labels-required.md):
every test runs as deployment `test` (`DISKY_LABELS=app=disky,deployment=test`), never the shell's;
a test of the missing case removes or replaces it.

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


def _cap_threads_per_worker() -> None:
    """Under pytest-xdist, each worker's DuckDB connections and pyarrow pools get the cores ÷ workers (≥ 2), so
    `-n 16` doesn't run 16 × cores threads. A connection given `threads` (a config or `SET threads`) keeps it."""
    n_workers = int(environ.get('PYTEST_XDIST_WORKER_COUNT') or 0)
    if not n_workers:
        return
    from os import cpu_count

    import duckdb
    import pyarrow

    threads = max(2, (cpu_count() or 1) // n_workers)
    pyarrow.set_cpu_count(threads)
    pyarrow.set_io_thread_count(max(threads, 2))
    connect = duckdb.connect

    def capped(database=':memory:', read_only=False, config=None, **kw):
        config = dict(config or {})
        config.setdefault('threads', threads)
        return connect(database, read_only, config, **kw)

    duckdb.connect = capped
    duckdb.execute(f'SET threads = {threads}')


_cap_threads_per_worker()

from dt_cloud.d1_schema import replay, schema_file  # noqa: E402 (after the root is set)


#: Tests per xdist group sharing one build of a module-scoped fixture (`pytest_collection_modifyitems`); `slow` ones
#: (minutes each) in pairs, apart from the rest.
XDIST_CHUNK = 16
XDIST_CHUNK_SLOW = 2


@pytest.hookimpl(tryfirst=True)  # before xdist's own, which reads the groups into the node ids
def pytest_collection_modifyitems(config, items):
    """Under `--dist loadgroup`, the tests that share a module-scoped fixture (and its params) go to one worker in
    groups of `XDIST_CHUNK`: each worker would otherwise build the fixture for itself (`test_static_drill.py`'s `world`
    takes a minute per config, and 16 workers built it 32 times); in chunks, it is built a few times, in parallel. A
    `slow` test takes minutes: those go in pairs, so no chunk holds a long run of them."""
    if not config.pluginmanager.hasplugin("xdist"):
        return
    seen: dict[tuple, int] = {}
    for item in items:
        defs = getattr(item, "_fixtureinfo", None)
        shared = sorted(n for n, fd in (defs.name2fixturedefs.items() if defs else ()) if fd and fd[-1].scope == "module")
        if not shared:
            continue
        slow = item.get_closest_marker("slow") is not None
        params = getattr(item, "callspec", None)
        key = (item.path.name, *(f"{n}={params.params[n]!r}" for n in shared if params and n in params.params), *(["slow"] if slow else []))
        i = seen[key] = seen.get(key, -1) + 1
        # no `]` in the name: xdist takes a node id's `@<group>` only after its last `]`
        name = ":".join(key).replace("[", "(").replace("]", ")")
        item.add_marker(pytest.mark.xdist_group(f"{name}#{i // (XDIST_CHUNK_SLOW if slow else XDIST_CHUNK)}"))


@pytest.fixture
def d1_db() -> sqlite3.Connection:
    return replay([schema_file().read_text()])


@pytest.fixture(autouse=True)
def _deployment_env(monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", "my-data")
    monkeypatch.setenv("SITE_URL", "https://site.example.org")
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=test")
    # Deployment config the generic code requires (no defaults): unset unless a test sets it.
    for v in ("LAYER2_PREFIX", "STATIC_NAMES_PROFILE", "STATIC_NAMES_GEN", "SCAN_RUN_ID", "SCAN_ID"):
        monkeypatch.delenv(v, raising=False)
    # the cw digest's scan triggers come from the site (`/api/scan-runs`): none recorded, unless a test says otherwise
    from dt_cloud import digest_cw

    monkeypatch.setattr(digest_cw, "site_triggers", lambda cfg: {})
    # …and its live settings (`/api/digest-settings`): none, so the config file's stand
    from dt_cloud import digest

    monkeypatch.setattr(digest, "live_settings", lambda cfg: None)


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


def thrds_fake(real: type):
    """A class decorator for a test's fake of a thrds client (`real`): every method the fake defines must exist on
    `real`, and every call to it must bind to `real`'s signature for that method, so a keyword thrds renames or drops
    fails the test instead of production (thrds 0.6.0's `files=` → `images=` passed CI against a fake that still took
    `files=`)."""
    import functools
    import inspect

    def wrap(fake: type) -> type:
        for name, fn in list(vars(fake).items()):
            if name.startswith("_") or not callable(fn):
                continue
            if not hasattr(real, name):
                raise TypeError(f"{fake.__name__}.{name}: {real.__name__} has no {name}")
            sig = inspect.signature(getattr(real, name))

            def checked(self, *args, __fn=fn, __sig=sig, __name=name, **kw):
                try:
                    __sig.bind(self, *args, **kw)
                except TypeError as e:
                    raise TypeError(f"{real.__name__}.{__name}{__sig}: {e}") from None
                return __fn(self, *args, **kw)

            setattr(fake, name, functools.wraps(fn)(checked))
        return fake

    return wrap

