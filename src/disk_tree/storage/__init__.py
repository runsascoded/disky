from .base import StorageBackend
from .parquet import ParquetBackend

__all__ = ['StorageBackend', 'ParquetBackend', 'get_backend', 'reset_backend']

_backend_instance: StorageBackend | None = None


def get_backend(backend_type: str | None = None) -> StorageBackend:
    """Get or create the storage backend singleton.

    Args:
        backend_type: 'parquet' or 'hybrid'. If None, uses DISK_TREE_BACKEND
                      env var or defaults to 'hybrid'.
    """
    global _backend_instance

    if _backend_instance is not None:
        return _backend_instance

    import os
    backend_type = backend_type or os.environ.get('DISK_TREE_BACKEND', 'hybrid')

    if backend_type == 'parquet':
        from .parquet import ParquetBackend
        _backend_instance = ParquetBackend()
    elif backend_type == 'hybrid':
        from .hybrid import HybridBackend
        _backend_instance = HybridBackend()
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")

    return _backend_instance


def reset_backend():
    """Reset the backend singleton (for testing)."""
    global _backend_instance
    _backend_instance = None


# `index --to` repoints the write dir; the backend must be rebuilt against it.
from .. import config as _config  # noqa: E402
_config.on_write_target_change(reset_backend)
