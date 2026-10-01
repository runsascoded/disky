"""URL parsing and normalization for scan sources.

Supported inputs:
- Bare path: `/Users/ryan`  → `file` scheme, absolute path
- `file://` URL
- `s3://bucket/prefix` (and `r2://`, `gcs://`, `az://`)
"""

from dataclasses import dataclass
from os.path import abspath, expanduser
from urllib.parse import urlparse


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    path: str
    user: str | None = None
    host: str | None = None
    port: int | None = None
    # Preserve any extra netloc bits (e.g. R2 account identifier)
    raw: str = ''


def parse_url(url: str) -> ParsedUrl:
    if '://' not in url:
        # Bare local path
        return ParsedUrl(
            scheme='file',
            path=abspath(expanduser(url)),
            raw=url,
        )

    parsed = urlparse(url)
    if parsed.scheme in ('s3', 'r2', 'gcs', 'az'):
        # Object-storage URLs: netloc is the bucket; path keeps the key prefix.
        return ParsedUrl(
            scheme=parsed.scheme,
            host=parsed.netloc,
            path=parsed.path,
            raw=url,
        )
    return ParsedUrl(
        scheme=parsed.scheme,
        user=parsed.username,
        host=parsed.hostname,
        port=parsed.port,
        path=parsed.path,
        raw=url,
    )


def url_parent(url: str) -> str | None:
    """Return the parent URL (one path segment up), or `None` if already at root."""
    p = parse_url(url)
    if p.scheme == 'file':
        if p.path == '/':
            return None
        parent = p.path.rsplit('/', 1)[0] or '/'
        return parent
    # Scheme with host component
    if p.path in ('', '/'):
        return None
    parent_path = p.path.rsplit('/', 1)[0] or '/'
    userpart = f'{p.user}@' if p.user else ''
    portpart = f':{p.port}' if p.port else ''
    host_part = f'{userpart}{p.host}{portpart}'
    if parent_path == '/':
        # Strip the trailing slash so the bucket root matches what `canonical()`
        # stores (e.g. `s3://bucket`, not `s3://bucket/`).
        return f'{p.scheme}://{host_part}'
    return f'{p.scheme}://{host_part}{parent_path}'


def canonical(url: str) -> str:
    """Return the canonical form of `url` (what gets stored in the DB)."""
    p = parse_url(url)
    if p.scheme == 'file':
        return p.path.rstrip('/') or '/'
    if p.scheme in ('s3', 'r2', 'gcs', 'az'):
        return f'{p.scheme}://{p.host}{p.path}'.rstrip('/')
    return url
