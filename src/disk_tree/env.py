"""The engine's environment settings, by suffix: ``get('ROOT')`` reads ``$DISKY_ROOT``, else the pre-rename
``$DISK_TREE_ROOT`` (``specs/disky-rename.md`` step 3; the fallback goes once every deployment sets ``DISKY_*``).
A child process is handed the new name (:func:`name`), so a parent's ``DISKY_X`` can't shadow what it set."""
from collections.abc import Mapping
from os import environ

PREFIX = 'DISKY_'
OLD_PREFIX = 'DISK_TREE_'


def name(suffix: str) -> str:
    """The variable a setting is written under: ``DISKY_<suffix>``."""
    return f'{PREFIX}{suffix}'


def get(suffix: str, default: str | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """``$DISKY_<suffix>``, else ``$DISK_TREE_<suffix>``, else ``default``."""
    e = environ if env is None else env
    for var in (f'{PREFIX}{suffix}', f'{OLD_PREFIX}{suffix}'):
        if var in e:
            return e[var]
    return default
