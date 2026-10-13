# Awair data in the public R2 demo

Ryan suggested adding Awair's public R2 data to [the R2 demo]. Local Awair source confirms that the production `awair-serve` Worker binds R2 bucket `awair` and exposes a public file-tree reader restricted to `pyramid/`. The frontend uses [the serving Worker]; the repository README still emphasizes its older S3 storage and is not the current R2 inventory source.

Do not assume that the entire bucket is public merely because the Worker exposes one prefix. The suitable first integration is the already-public `pyramid/` prefix, under `awair/pyramid/…` in the demo, unless the Awair owner confirms whole-bucket publication.

The public `/files/list?prefix=pyramid%2F&limit=10` endpoint was verified live on 2026-10-13: four device directories and an invalidations JSON object. The public health inventory reports 822 shards with size statistics, totaling approximately 111,268,623 bytes (111 MB / 106 MiB). This is an inventory-derived estimate, not a complete bucket accounting: extra keys and the invalidations object are not necessarily included. It would be useful as a drillable demo source but essentially invisible in the fleet root treemap beside roughly 973 GiB.

The existing RAC demo read-only token returned `AccessDenied` on a bounded `ListObjectsV2(Bucket="awair", MaxKeys=1)` probe on 2026-10-13. The daily-ingest workflow currently scans `ctbk`, `crashes`, and `jc-taxes`; adding an unconditional Awair listing now would fail the job. No credentials were printed, no sensor object contents were read, and no data was published by this check.

Next: confirm the public prefix with the Awair maintainer, grant the existing scan token read/list access to `awair` (or provision a separate bucket-scoped RO token), update the workflow's source allowlist with prefix filtering, and run a bounded cloud/GHA scan. Reuse the current index bucket/D1 and daily refresh; a separate deployment is unnecessary for another intentionally public demo source. Do not scan the entire account or download sensor parquet contents on the laptop.

[the R2 demo]: https://r2.rbw.sh/
[the serving Worker]: https://awair-serve.ryan-0dc.workers.dev
