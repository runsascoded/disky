# Incremental R2 read-recency checkpoint

Status: implemented (`dt-cloud access r2-sync`, the `access-r2` scan step's opt-in `checkpoint`). A deployment's live validation and rollout record stay in its own branch's copy of this spec. No archive deletion or new retention policy is applied by this work.

The R2 access collector moves an hour entry into `frozen` once it ages out of its reverification window, so frozen windows are immutable; the previous consumer downloaded both the active and frozen sets on every scan.

## Read checkpoint

Add an opt-in consumer command that keeps one durable Parquet checkpoint in the deployment's existing data bucket. Closed history is one row per `(bucket, canonical path, operation)`: cumulative count, earliest and latest contributing timestamps, latest window and sampling flag. A cursor per bucket tracks incorporated frozen windows. Each update incorporates only newly frozen windows and reads the bounded active window set afresh, so a revised active window replaces its previous contribution rather than adding twice. The checkpoint is one atomically replaced object (GCS generation precondition, or local writer lock); its size follows distinct read keys rather than elapsed hours. It does not retain a growing list of processed window keys. Parquet allows the existing bounded DuckDB pipeline to read and write the checkpoint directly, without a second database engine.

The output explicitly identifies itself as a read summary, not a general hourly traffic series. It preserves the read lens's per-path counts, last-read times, sampling flags and observation bounds. The existing hourly aggregation command stays available. A historical request older than the closed-history cutoff must refuse rather than silently use later counts; historical scan views remain in the path interval store.

Fetch retries follow the existing superseded-aggregate protocol: re-read state on a missing active key, and publish neither checkpoint nor output from a partial read. Bootstrap validation compares the summary with the full historical aggregation projected onto the read lens's fields. Tests cover unchanged updates, active replacement, active-to-frozen transition, empty windows, sampling, multiple buckets, canonical paths, backwards cuts, failure atomicity and a fixed workload's bounded row count.

## Rollout and later retirement

Run the first real build on Batch with bounded memory, compare against the current full aggregation, and publish the checkpoint only after validation. Enable the primary scan profile after CI and the comparison pass, then verify a manual scan and its serving probe. This creates derived data in the existing deployment; it does not mint grants or change IAM.

Collector `frozen` metadata and old archive aggregates remain until a separate handoff is implemented: a verified checkpoint watermark, bounded collector bookkeeping and an exact retirement list reviewed by Ryan. Access-archive deletions remain held; the existing path-sort retirement policy continues. Age history and meta retirement adopt the existing shared implementations and receive parity plus cold/warm read benchmarks before any serving cutover.
