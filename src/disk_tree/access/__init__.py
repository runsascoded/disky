"""Access-log ingest + aggregation for object stores.

Layer-1a (raw): one canonical row per request (a parser's output, e.g.
:mod:`disk_tree.access.parsers.gcs`).
Layer-2a (aggregate): per-path per-day per-op rollups, reusing the same
path-tree machinery as size scans, so the treemap/diff/series widgets work
over ops and egress the way they already work over bytes-at-rest.

Design mirrors the size-scan split (:mod:`disk_tree.find`): scheme-generic
aggregation core, per-scheme parser plugins, storage as parquet.

See ``gcs:specs/access-logs-and-cost.md``.
"""
