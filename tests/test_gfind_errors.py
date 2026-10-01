"""`PERMISSION_DENIED_RE`: the stderr lines a walk's error collector recognizes."""
from __future__ import annotations

import pytest

from disk_tree.backends.gfind import PERMISSION_DENIED_RE


@pytest.mark.parametrize("line, path", [
    ("gfind: '/Users/x/Library/Mail': Permission denied", "/Users/x/Library/Mail"),
    ("find: ‘/private/var/db’: Permission denied", "/private/var/db"),
    ("dt-walker: '/Users/x/Library/Safari': Permission denied", "/Users/x/Library/Safari"),
    ("gfind: '/x': No such file or directory", None),
    ("walker: '/x': Permission denied", None),
])
def test_permission_denied_lines(line: str, path: str | None):
    """gfind, find and the native `dt-walker` report a denied path the same
    way (straight or curly quotes); anything else isn't a permission error."""
    m = PERMISSION_DENIED_RE.match(line)
    assert (m.group(1) if m else None) == path
