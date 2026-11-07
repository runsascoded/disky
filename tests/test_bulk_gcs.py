from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass

from disk_tree.find.bulk import BlobRow
from disk_tree.find.bulk_gcs import BLOB_FIELDS, BLOB_FIELDS_CRC, GcsBulkLister


@dataclass
class Blob:
    name: str = "a/x"
    size: int = 7
    time_created: dt.datetime = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
    storage_class: str = "STANDARD"
    generation: str = "123456789"
    crc32c: str = "yZRlqg=="


class Client:
    calls = None

    def __init__(self):
        self.calls = []

    def list_blobs(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return [Blob()]


def test_stream_prefix_requests_and_retains_generation() -> None:
    client = Client()
    lister = GcsBulkLister()
    object.__setattr__(lister, "_local", threading.local())
    lister._local.client = client
    assert list(lister.stream_prefix("bucket", "a/", "a/0", "a/z")) == [
        BlobRow("a/x", 7, "2026-10-04T12:00:00Z", "STANDARD", 123456789),
    ]
    assert client.calls == [(('bucket',), {
        "prefix": "a/",
        "fields": "items(name,size,timeCreated,storageClass,generation),nextPageToken",
        "start_offset": "a/0",
        "end_offset": "a/z",
    })]
    assert BLOB_FIELDS == "items(name,size,timeCreated,storageClass,generation),nextPageToken"


def test_crc32c_lister_requests_and_decodes_it() -> None:
    """`GcsBulkLister(crc32c=True)` asks for `crc32c` and keeps it as the unsigned int; the default lister neither asks
    nor keeps it."""
    rows = {}
    for on in (False, True):
        client = Client()
        lister = GcsBulkLister(crc32c=on)
        object.__setattr__(lister, "_local", threading.local())
        lister._local.client = client
        rows[on] = (list(lister.stream_prefix("bucket", "a/", None, None)), client.calls[0][1]["fields"])
    assert rows == {
        False: ([BlobRow("a/x", 7, "2026-10-04T12:00:00Z", "STANDARD", 123456789)], BLOB_FIELDS),
        True: ([BlobRow("a/x", 7, "2026-10-04T12:00:00Z", "STANDARD", 123456789, 3381945770)], BLOB_FIELDS_CRC),
    }
    assert BLOB_FIELDS_CRC == "items(name,size,timeCreated,storageClass,generation,crc32c),nextPageToken"
