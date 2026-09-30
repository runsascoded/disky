from .base import Backend, ErrorCollector, ProgressCallback
from .local import LocalBackend
from .s3 import S3Backend
from .url import ParsedUrl, canonical, parse_url, url_parent


class UnsupportedBackend(Backend):
    """A cloud scheme disk-tree can *hold scans for* (imported via `pull` /
    `import`) but cannot list live. Exists so `backend_for` never falls through
    to the local filesystem for a `gcs://` URL — which used to "succeed" with an
    empty scan — and so callers that only ask `is_local` keep working."""

    def __init__(self, scheme: str):
        self._scheme = scheme

    @property
    def scheme(self) -> str:
        return self._scheme

    def _refuse(self, verb: str) -> NotImplementedError:
        return NotImplementedError(
            f"{verb} of {self._scheme}:// isn't implemented; import a listing instead "
            "(`disk-tree pull` with the bucket in buckets.yml, or `disk-tree import -l <listing>`)"
        )

    def list(self, url: str, **kwargs):
        raise self._refuse('live scanning')

    def delete(self, url: str) -> None:
        raise self._refuse('delete')

    def exists(self, url: str) -> bool:
        raise self._refuse('existence check')


def backend_for(url: str) -> Backend:
    """Return a Backend instance for the given URL's scheme.

    `r2://` is S3-compatible: it lists through the bucket's Cloudflare endpoint
    (`DISK_TREE_R2_ENDPOINT_URL`, else the bucket's `endpoint_url` in
    buckets.yml — resolved here, missing reported at list time). `gcs://` has
    no live lister (see `UnsupportedBackend`).
    """
    parsed = parse_url(url)
    if parsed.scheme == 's3':
        from urllib.parse import urlparse
        from disk_tree.blobfs import bucket_profile
        return S3Backend(profile=bucket_profile(urlparse(url).netloc))
    if parsed.scheme == 'r2':
        from urllib.parse import urlparse
        from disk_tree.blobfs import bucket_profile, r2_endpoint
        netloc = urlparse(url).netloc
        return S3Backend(endpoint_url=r2_endpoint(netloc), profile=bucket_profile(netloc), scheme='r2')
    if parsed.scheme == 'gcs':
        return UnsupportedBackend('gcs')
    if parsed.scheme == 'ssh':
        from .ssh import SshBackend
        return SshBackend()
    return LocalBackend()


__all__ = [
    'Backend',
    'ErrorCollector',
    'LocalBackend',
    'ParsedUrl',
    'ProgressCallback',
    'S3Backend',
    'UnsupportedBackend',
    'backend_for',
    'canonical',
    'parse_url',
    'url_parent',
]
