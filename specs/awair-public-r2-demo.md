# Awair data in the public R2 demo

Ryan suggested adding Awair's public R2 data to [the R2 demo]. Local Awair source confirms that the production `awair-serve` Worker binds R2 bucket `awair` and exposes a public file-tree reader restricted to `pyramid/`. The frontend uses [the serving Worker]; the repository README still emphasizes its older S3 storage and is not the current R2 inventory source.

Do not assume that the entire bucket is public merely because the Worker exposes one prefix. The suitable first integration is the already-public `pyramid/` prefix, under `awair/pyramid/…` in the demo, unless the Awair owner confirms whole-bucket publication.

The public `/files/list?prefix=pyramid%2F&limit=10` endpoint was verified live on 2026-10-13: four device directories and an invalidations JSON object. The public health inventory reports 822 shards with size statistics, totaling approximately 111,268,623 bytes (111 MB / 106 MiB). This is an inventory-derived estimate, not a complete bucket accounting: extra keys and the invalidations object are not necessarily included. It would be useful as a drillable demo source but essentially invisible in the fleet root treemap beside roughly 973 GiB.

The existing RAC demo read-only token returned `AccessDenied` on a bounded `ListObjectsV2(Bucket="awair", MaxKeys=1)` probe on 2026-10-13. The daily-ingest workflow currently scans `ctbk`, `crashes`, and `jc-taxes`; adding an unconditional Awair listing now would fail the job. No credentials were printed, no sensor object contents were read, and no data was published by this check.

Direction updated 2026-10-13: Ryan wants project-owned scans and infrastructure, with `r2.rbw.sh` only reading their exports. Do not broaden the aggregate's scan token or add Awair to its central daily listing job. Follow [the federated scan contract]: Awair's own job publishes complete immutable manifests and serving artifacts for its already-public `pyramid/` scope; the aggregate resolves its latest scan at or before a shared cutoff, alongside other independently scanned projects. Awair owns schedule, source credentials, publication storage and IaC; the demo owns only reader/cache resources. Its existing live shard inventory is a useful discovery input, not an atomic completed scan export.

The shared catalog/resolver/pin model and a three-picker development fixture are now implemented on Disky root; see [source catalog contract]. Owner handoff has been written to `/Users/ryan/c/awair/specs/disky-scan-export.md` as an untracked spec, with no Awair implementation, apply, source credential change or exposure change. There is no registered Awair agent on the connected Agent Mail server, so it has not been delivered to an active owner through mail. Next: Awair's own project session implements/reviews the producer and returns its completed catalog/read routing; Disky can then wire a real read-only source preview. No whole-account scanning or sensor-parquet downloads on the laptop.

[the R2 demo]: https://r2.rbw.sh/
[the serving Worker]: https://awair-serve.ryan-0dc.workers.dev
[the federated scan contract]: federated-scans.md
[source catalog contract]: federated-source-catalog.md
