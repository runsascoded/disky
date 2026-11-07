"""Stubbed-botocore tests for `S3BulkLister` start semantics.

Locks the fix for the adaptive-listing launch crash: the seed range starts at
`''` (keyspace origin), which is *not* a real key — S3 keys have min length 1,
so `head_object(Key='')` is a botocore `ParamValidationError`. Origin starts
must issue neither the HEAD compensation nor `StartAfter`.

`botocore.stub.Stubber` enforces the exact operation sequence and request
params, so an accidental HEAD (or a stray `StartAfter=''`) fails loudly.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import Stubber

from disk_tree.find.bulk import BlobRow
from disk_tree.find.bulk_s3 import S3BulkLister


_LM = datetime(2026, 8, 1, tzinfo=timezone.utc)
_LM_STR = '2026-08-01T00:00:00Z'


def _stubbed_lister():
    client = boto3.client('s3', region_name='us-east-1')
    stubber = Stubber(client)
    lister = S3BulkLister()
    object.__setattr__(lister, '_local', threading.local())
    lister._local.client = client
    return lister, stubber


def _obj(key: str, size: int, storage_class: str = 'STANDARD') -> dict:
    return {'Key': key, 'Size': size, 'LastModified': _LM, 'StorageClass': storage_class}


@pytest.mark.parametrize('start', [None, ''], ids=['none', 'empty'])
def test_stream_pages_origin_start(start):
    """Origin start: exactly one LIST, no HEAD, no `StartAfter`."""
    lister, stubber = _stubbed_lister()
    stubber.add_response(
        'list_objects_v2',
        {'Contents': [_obj('a.txt', 3), _obj('b.txt', 5, 'NEARLINE')], 'IsTruncated': False},
        {'Bucket': 'b1'},
    )
    with stubber:
        rows = [r for page in lister.stream_pages('b1', prefix=None, start=start, end_hint=None) for r in page]
        stubber.assert_no_pending_responses()
    assert rows == [
        BlobRow(name='a.txt', size=3, created=_LM_STR, storage_class='STANDARD'),
        BlobRow(name='b.txt', size=5, created=_LM_STR, storage_class='NEARLINE'),
    ]


def test_stream_pages_real_start():
    """Non-empty start: HEAD-compensated inclusive start, then `StartAfter`."""
    lister, stubber = _stubbed_lister()
    stubber.add_response(
        'head_object',
        {'ContentLength': 7, 'LastModified': _LM, 'StorageClass': 'NEARLINE'},
        {'Bucket': 'b1', 'Key': 'k1'},
    )
    stubber.add_response(
        'list_objects_v2',
        {'Contents': [_obj('k2', 11)], 'IsTruncated': False},
        {'Bucket': 'b1', 'StartAfter': 'k1'},
    )
    with stubber:
        rows = [r for page in lister.stream_pages('b1', prefix=None, start='k1', end_hint=None) for r in page]
        stubber.assert_no_pending_responses()
    assert rows == [
        BlobRow(name='k1', size=7, created=_LM_STR, storage_class='NEARLINE'),
        BlobRow(name='k2', size=11, created=_LM_STR, storage_class='STANDARD'),
    ]


def test_stream_prefix_origin_start():
    """`stream_prefix` origin start: LIST carries only Bucket+Prefix; the
    client-side `end` bound truncates the page."""
    lister, stubber = _stubbed_lister()
    stubber.add_response(
        'list_objects_v2',
        {'Contents': [_obj('p/a', 2), _obj('p/z', 4)], 'IsTruncated': False},
        {'Bucket': 'b1', 'Prefix': 'p/'},
    )
    with stubber:
        rows = list(lister.stream_prefix('b1', prefix='p/', start='', end='p/z'))
        stubber.assert_no_pending_responses()
    assert rows == [
        BlobRow(name='p/a', size=2, created=_LM_STR, storage_class='STANDARD'),
    ]


def test_client_retry_config(monkeypatch):
    """The lazily-built client must carry adaptive throttling retries (the
    2026-09-01 CoreWeave `SlowDown` fix) — with and without the addressing-
    style env override, which must merge rather than replace it."""
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'x')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'y')
    monkeypatch.delenv('DT_S3_ADDRESSING_STYLE', raising=False)
    cfg = S3BulkLister()._client().meta.config
    # botocore normalizes max_attempts=10 to initial-try + 10 retries
    assert cfg.retries == {'mode': 'adaptive', 'total_max_attempts': 11}
    assert cfg.s3 is None

    monkeypatch.setenv('DT_S3_ADDRESSING_STYLE', 'virtual')
    monkeypatch.setenv('DT_S3_MAX_ATTEMPTS', '4')
    cfg = S3BulkLister(endpoint_url='https://cwobject.com')._client().meta.config
    assert cfg.retries == {'mode': 'adaptive', 'total_max_attempts': 5}
    assert cfg.s3 == {'addressing_style': 'virtual'}
