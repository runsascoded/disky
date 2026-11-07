"""`r2://` as a registered fsspec protocol, so any fsspec call site (`fsspec.open`, `url_to_fs`, pandas, pyarrow
through fsspec) reaches Cloudflare R2, not only `blobfs`'s own helpers.

It is `s3fs` with each bucket's endpoint and credential profile resolved the way `blobfs.fs_for` resolves them:
`DISKY_R2_ENDPOINT_URL`, else the bucket's (or `defaults`) `endpoint_url` in `buckets.yml`, and the bucket's
`profile` there (a deployment whose ambient `AWS_*` keys are another provider's names R2's by profile). fsspec builds a
filesystem from the URL (`_get_kwargs_from_urls`), so `r2://a/…` and `r2://b/…` each get their own bucket's settings.
Registered by `disk_tree`'s `fsspec.specs` entry point, and by importing `blobfs`.
"""
from __future__ import annotations

from urllib.parse import urlparse

from s3fs import S3FileSystem


class R2FileSystem(S3FileSystem):
    protocol = ("r2",)

    @classmethod
    def _get_kwargs_from_urls(cls, urlpath: str) -> dict:
        from . import blobfs

        out = super()._get_kwargs_from_urls(urlpath)
        bucket = urlparse(urlpath if "://" in urlpath else f"r2://{urlpath}").netloc
        ep = blobfs.r2_endpoint(bucket)
        if not ep:
            raise RuntimeError(f"r2://{bucket}: no endpoint — set {blobfs.R2_ENDPOINT_VAR}, or give the bucket an `endpoint_url` in buckets.yml")
        # R2 needs equal-sized non-trailing multipart parts (`blobfs._s3fs`'s note).
        out.update(client_kwargs={"endpoint_url": ep}, fixed_upload_size=True)
        if profile := blobfs.bucket_profile(bucket):
            out["profile"] = profile
        return out
