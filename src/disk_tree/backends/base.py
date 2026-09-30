from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterator


# Progress callback: (items_found, items_per_sec, error_count) -> None
ProgressCallback = Callable[[int, float | None, int], None]


@dataclass
class ErrorCollector:
    """Mutable container to collect errors during iteration."""
    count: int = 0
    paths: list[str] = field(default_factory=list)
    max_paths: int = 100  # Only store first N paths to avoid memory issues

    def add(self, path: str):
        self.count += 1
        if len(self.paths) < self.max_paths:
            self.paths.append(path)


class Backend(ABC):
    """Scan-source backend: where disk-tree reads from."""

    @property
    @abstractmethod
    def scheme(self) -> str:
        ...

    @property
    def is_local(self) -> bool:
        """True if the backend points at a local filesystem (enables Finder reveal, etc)."""
        return False

    @property
    def supports_sudo(self) -> bool:
        return False

    @abstractmethod
    def list(
        self,
        url: str,
        *,
        errors: ErrorCollector | None = None,
        excludes: list[str] | None = None,
        sudo: bool = False,
        progress: bool = True,
    ) -> Iterator[dict]:
        """Recursive bulk listing.

        Yields dicts with keys: path, size, mtime, kind, parent, uri.
        `path` is relative to scan root (empty string for the root entry);
        `uri` is fully qualified for this backend's scheme.
        """
        ...

    def delete(self, url: str) -> None:
        """Delete a single path. Recursive for directories."""
        raise NotImplementedError(f"{type(self).__name__}.delete not implemented")

    def restore(self, url: str) -> int:
        """Undo a `delete` of `url` where the store makes that possible (spec
        `specs/staged-delete.md` CP5) — e.g. remove the delete-markers a
        versioned bucket added. Returns the number of objects restored. The
        default has no undo (a plain delete is permanent)."""
        raise NotImplementedError(f"{type(self).__name__}.restore not implemented (no undo for this store)")

    def exists(self, url: str) -> bool:
        """Cheap existence check (used for navigation fallbacks)."""
        raise NotImplementedError(f"{type(self).__name__}.exists not implemented")
