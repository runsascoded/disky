# cw

Storage usage of Marin's CoreWeave (CAIOS) S3 buckets, served at [cw.oa.dev]: treemap, sizes over time, diffs between scans, bucket lifecycle rules, and a plan-first sweep console.

This is the `cw` deployment branch of [disky]. It merges the shared `cloud` base (the `disk-tree` engine, the `dt-cloud` CLI and the viewer site) and adds the CoreWeave store on top: the scan job, the cw store's site config, sweep plans, and the `#cw-usage` Slack digest.

The deployment was renamed from `cw-s3` on 2026-10-17 (`specs/done/rename-cw.md`): cw-s3.oa.dev redirects to cw.oa.dev, and cloud resource ids keep the old name (Pages project `oa-cw-s3-usage`, D1 `oa-cw-s3-usage-db`, the `oa-cw-s3-usage-*` buckets, Secret Manager `cw-s3-*`).

## How it runs

- **Scan job** (GCP Batch, every 6 h on the `:latest` image): Cloud Scheduler runs the Cloud Run job `cw-usage-snapshot-submit` ([`job/cw-scan-submit.sh`]), which submits the scan as Batch job `cw-usage-snapshot-YYYYMMDD-HH`, retrying transient failures and alerting `#cw-admin` if it can't. The scan lists each bucket over the CAIOS S3 endpoint, builds the path index and interval store the site reads, publishes them to R2, pulls lifecycle rules, warms the cache and updates the digest.
- **Site** ([`site/`], Cloudflare Pages project `oa-cw-s3-usage`): the viewer reads the index through its Pages Functions; sweep plans, runs and sign-in state live in D1 (`oa-cw-s3-usage-db`, schema in `site/schema.sql`). Sign-in is the app's own (Google OIDC or emailed codes). A dev stack runs at dev.cw.oa.dev.
- **Digest**: one Slack thread per month in `#cw-usage`, with a reply per scan and an OP image (size vs quota, plus a "what changed" diff treemap). Alerts and admin threads go to `#cw-admin`.

## Development

See [`AGENTS.md`] (also `CLAUDE.md`).

[cw.oa.dev]: https://cw.oa.dev
[disky]: https://github.com/Open-Athena/disky
[`job/cw-scan-submit.sh`]: job/cw-scan-submit.sh
[`site/`]: site
[`AGENTS.md`]: AGENTS.md
