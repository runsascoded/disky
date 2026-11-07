"""In-memory reference implementation of the adaptive-listing page protocol.

Serves two purposes: an executable spec of
:class:`~disk_tree.find.bulk_adaptive.PagedLister` semantics (S3-flavored:
exclusive start compensated by a head-lookup, no server-side end), and a
picklable lister for tests/benchmarks of :func:`list_bucket_adaptive` that
exercise the real multiprocessing path without any cloud SDK. Lives in
``src`` (not tests) so spawned worker processes can import it by module path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from disk_tree.find.bulk import BlobRow


def _row(key: str, crc32c: bool = False) -> BlobRow:
    """Deterministic size/created/class per key, so any listing of the same
    keys is comparable regardless of which worker/range — or *process* —
    produced it (built-in ``hash`` is salt-randomized per process). With
    ``crc32c``, the key's zlib CRC32 stands in for its contents' CRC32C."""
    from zlib import crc32
    h = crc32(key.encode()) & 0x7FFFFFFF
    return BlobRow(
        name=key,
        size=(h % 10_000) + 1,
        created=f"2026-08-{(h % 28) + 1:02d}T00:00:00Z",
        storage_class=("STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE")[h % 4],
        crc32c=crc32(key.encode()) if crc32c else None,
    )


@dataclass(frozen=True)
class FakeLister:
    """PagedLister over a fixed key list; S3 semantics (exclusive start +
    head-compensation, ``end_hint`` ignored)."""

    keys: "tuple[str, ...]"  # will be sorted at stream time
    page_size: int = 5
    scheme: str = "s3"
    # Simulated per-page RTT. The real donation dynamic depends on pages
    # *taking time* (peers go idle while a worker paginates); an instant fake
    # can drain the whole keyspace before other workers even start.
    page_delay_s: float = 0.0
    #: Rows carry a CRC32C (`bulk-list -c`'s shape).
    crc32c: bool = False

    def stream_pages(
        self,
        bucket: str,
        prefix: Optional[str],
        start: Optional[str],
        end_hint: Optional[str],
    ) -> "Iterable[list[BlobRow]]":
        ks = sorted(self.keys)
        if prefix:
            ks = [k for k in ks if k.startswith(prefix)]
        if start is not None:
            head = [k for k in ks if k == start]  # inclusive-start compensation
            ks = [k for k in ks if k > start]
            if head:
                yield [_row(head[0], self.crc32c)]
        for i in range(0, len(ks), self.page_size):
            if self.page_delay_s:
                from time import sleep
                sleep(self.page_delay_s)
            yield [_row(k, self.crc32c) for k in ks[i:i + self.page_size]]
