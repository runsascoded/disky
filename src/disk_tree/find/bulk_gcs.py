"""GCS-specific plug-in for :mod:`disk_tree.find.bulk`.

Requires the ``[gcs]`` extra (``gcsfs`` for discovery, ``google-cloud-storage``
for the streaming client — its :meth:`~google.cloud.storage.Client.list_blobs`
accepts ``start_offset`` / ``end_offset`` byte-range cursors, which is what
lets us shard hot prefixes).
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Iterable, Optional

from disk_tree.find.bulk import BlobRow, generic_discover

if TYPE_CHECKING:
    import fsspec  # noqa: F401


err = partial(print, file=sys.stderr)

BLOB_FIELDS = "items(name,size,timeCreated,storageClass,generation),nextPageToken"


@dataclass(frozen=True)
class GcsBulkLister:
    """GCS implementation of :class:`~disk_tree.find.bulk.BulkLister`.

    The client is created lazily inside worker processes (see thread-local
    below) so the instance itself stays pickle-safe.
    """

    scheme: str = "gcs"

    def stream_prefix(
        self,
        bucket: str,
        prefix: str,
        start: Optional[str],
        end: Optional[str],
    ) -> Iterable[BlobRow]:
        from google.cloud import storage

        # Cache one client per thread to avoid the ~50ms auth handshake on
        # every prefix.
        local = getattr(self, "_local", None)
        if local is None:
            # Frozen dataclass — bypass __setattr__ once.
            object.__setattr__(self, "_local", threading.local())
            local = self._local
        client = getattr(local, "client", None)
        if client is None:
            client = local.client = storage.Client()
        for blob in client.list_blobs(
            bucket, prefix=prefix, fields=BLOB_FIELDS,
            start_offset=start, end_offset=end,
        ):
            created = None
            if blob.time_created is not None:
                created = blob.time_created.isoformat().replace("+00:00", "Z")
            yield BlobRow(
                name=blob.name,
                size=int(blob.size or 0),
                created=created,
                storage_class=blob.storage_class,
                generation=int(blob.generation) if blob.generation is not None else None,
            )

    def stream_pages(
        self,
        bucket: str,
        prefix: Optional[str],
        start: Optional[str],
        end_hint: Optional[str],
    ) -> "Iterable[list[BlobRow]]":
        """Page-granular variant for adaptive listing (bulk_adaptive.PagedLister).

        ``end_hint`` maps to GCS's native (server-side) ``end_offset``; the
        adaptive worker still enforces its dynamically-shrunken end
        client-side, so passing the take-time end here is purely an
        optimization. ``start_offset`` is inclusive — no compensation needed.
        """
        from google.cloud import storage

        local = getattr(self, "_local", None)
        if local is None:
            object.__setattr__(self, "_local", threading.local())
            local = self._local
        client = getattr(local, "client", None)
        if client is None:
            client = local.client = storage.Client()
        it = client.list_blobs(
            bucket, prefix=prefix or None, fields=BLOB_FIELDS,
            start_offset=start, end_offset=end_hint,
        )
        for page in it.pages:
            rows = []
            for blob in page:
                created = None
                if blob.time_created is not None:
                    created = blob.time_created.isoformat().replace("+00:00", "Z")
                rows.append(BlobRow(
                    name=blob.name,
                    size=int(blob.size or 0),
                    created=created,
                    storage_class=blob.storage_class,
                    generation=int(blob.generation) if blob.generation is not None else None,
                ))
            yield rows

    def discover_prefixes(self, fs: "fsspec.AbstractFileSystem", root: str):
        # GCS uses fsspec (gcsfs) semantics; the generic walk already handles
        # its self-dir placeholder quirk (that's what motivated the quirk in
        # the first place).
        return generic_discover(fs, root)

    def placeholder_rows(
        self,
        bucket: str,
        self_dirs: list[str],
    ) -> "list[tuple]":
        """Fetch the zero-byte placeholder objects behind self-dir entries.

        These only match a streamed prefix when their whole depth-1 dir
        streams wholesale, so with children streaming individually they must
        be counted explicitly (marin's central2 alone had 5 — e.g. an object
        literally named ``tmp/``).
        """
        from google.cloud import storage

        bkt = storage.Client().bucket(bucket)
        rows: "list[tuple]" = []
        for d in self_dirs:
            blob = bkt.get_blob(d)
            if blob is None:
                err(f"WARN: no placeholder object behind self entry {d!r} in {bucket}")
                continue
            created = None
            if blob.time_created is not None:
                created = blob.time_created.isoformat().replace("+00:00", "Z")
            rows.append((
                blob.name,
                int(blob.size or 0),
                created,
                blob.storage_class,
                int(blob.generation) if blob.generation is not None else None,
            ))
        return rows


def list_gcs_bucket_to_parquet(
    bucket: str,
    out_dir: str,
    procs: int = 6,
    threads: int = 8,
    prefix: Optional[str] = None,
    exists: str = "error",
    weights_from: Optional[str] = None,
) -> int:
    """Convenience wrapper mirroring marin's `list_bucket_to_parquet`.

    Uses :class:`GcsBulkLister` + gcsfs as the discovery filesystem. This is
    the exact signature marin's Batch job invokes today — a drop-in for
    ``gcs_usage.bucket_list.list_bucket_to_parquet``.
    """
    import gcsfs

    from disk_tree.find.bulk import list_bucket_to_parquet

    lister = GcsBulkLister()
    fs = gcsfs.GCSFileSystem(use_listings_cache=False)
    return list_bucket_to_parquet(
        lister=lister,
        bucket=bucket,
        out_dir=out_dir,
        fs=fs,
        procs=procs,
        threads=threads,
        prefix=prefix,
        exists=exists,
        weights_from=weights_from,
        discover=lister.discover_prefixes,
    )
