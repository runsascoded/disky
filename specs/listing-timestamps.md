# Listing timestamp semantics

Status: 2026-10-11. Provider-correct site labels implemented; listing schema migration not implemented. Existing numeric values remain unchanged.

The canonical layer-1 `created` field is overloaded: GCS scanners populate `timeCreated`, while S3/R2 scanners populate `LastModified` and local scans use filesystem modification time. The layer-2 `mtime` and served `d` date inherit that basis. The site now labels GCS values “created” and S3/R2/local values “modified,” including tables, chart descriptions and object explanations. This corrects presentation without silently reinterpreting historical values.

GCS exposes both `timeCreated` and `updated`; the latter changes with metadata updates, including some storage lifecycle operations, so it is not a pure content-write timestamp. S3/R2 LastModified describes the current object/version's modification time, not a guaranteed original creation time for a path. Directory dates are derived aggregates, not actual directory birth/modification events.

## Future schema change

Preserve distinct nullable `created` and `modified` fields and explicit provider/date-basis metadata. Do not fabricate a creation time from LastModified. Choose the date lens explicitly, retain generation identity, and record the selected basis in derived artifacts/scan metadata. Propagate that basis to UI and diff readers rather than inferring it forever from URI scheme.

Legacy readers can interpret old scans only from their recorded producer/provider contract. Mixed-provider unions or already ambiguous artifacts need explicit metadata; they must not guess unknown timestamps as zero or silently swap the lens. Migration tests should cover GCS creation versus metadata updates, replacement versions, null original creation for S3/R2, directories and scans produced before both columns existed.

[GCS object resource][gcs], [S3 system metadata][s3].

[gcs]: https://docs.cloud.google.com/storage/docs/json_api/v1/objects
[s3]: https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingMetadata.html
