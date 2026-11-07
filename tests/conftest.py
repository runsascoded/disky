"""Every test runs under a throwaway root (`$DISKY_ROOT`, set before `disk_tree` is imported, since `config` binds
its paths at import): a test that records a Scan or writes a blob without re-rooting lands in a temp dir, never in
the user's `~/.config/disk-tree`. A subprocess test hands its child its own root under the same name."""
from os import environ
from tempfile import mkdtemp

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
