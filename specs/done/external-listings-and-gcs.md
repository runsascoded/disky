# External-listing import + GCS support — SUPERSEDED

This draft (2026-07-21) has been folded into **`gcs-backend-and-snapshot-diff.md`** (2026-08-03 rewrite), which reframes the work: not "add a GCS backend + diff table for one consumer," but "disk-tree as the shared scalable storage-situational-awareness engine + reusable widgets, across cloud/local stores, with thin domain-wrapper consumers."

Its three asks live on there:
- **#1 import a pre-made listing** (object-listing parquet + GCS Storage Insights / S3 Inventory) → "Work item A".
- **#2 GCS backend** (+ r2/s3 sharded listers) → "Work item A".
- **#3 deep links** → "Work item E".

Read `gcs-backend-and-snapshot-diff.md`; delete this file once that's picked up.
