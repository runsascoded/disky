from os.path import expanduser

from disk_tree import config


def test_the_suite_never_touches_the_users_root():
    """`conftest.py` roots every test under a temp dir before `disk_tree` is imported: a test that writes Scan rows
    or blobs without re-rooting (e.g. `init()` after a late `setenv`) lands there, not in `~/.config/disk-tree`."""
    assert not config.ROOT_DIR.startswith(expanduser('~/.config/'))
