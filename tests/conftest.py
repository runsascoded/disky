"""Every test runs under a throwaway root (`$DISKY_ROOT`, set before `disk_tree` is imported, since `config` binds
its paths at import): a test that records a Scan or writes a blob without re-rooting lands in a temp dir, never in
the user's `~/.config/disk-tree`. A subprocess test hands its child its own root under the same name."""
from os import environ
from tempfile import mkdtemp

environ['DISKY_ROOT'] = mkdtemp(prefix='disky-test-root-')
