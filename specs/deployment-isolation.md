# OA deployment isolation

Status updated 2026-10-13: OA R2's owner now reports a live independent dashboard at `r2.oa.dev`, with its own dev/prod branches, scans and serving resources. CW's owner reports isolated capture/serving/append/compaction and a successful post-revocation smoke through `779dc5c5`; six obsolete grants removed. These are deployment-owner reports, not independently rerun root checks; see [root review queue]. The checklist below records the earlier migration design, not a claim that those dependencies are all still live. Remaining GCS-side cleanup must be verified by its owner. Infrastructure apply remains a human review gate.

Historical preparation evidence (2026-10-12): the independent OA R2 CF foundation's create-only real preview succeeded for 14 resources, including component/stack, with dedicated Pages/D1/KV/derived bucket and separate bucket-scoped source-reader/index-reader/publisher tokens. At that checkpoint, no resource migration, deployment, scan or apply had occurred.

## Target ownership

`gcs.oa.dev`, `cw-s3.oa.dev` and `r2.oa.dev` should have independent deployment branches, IaC stacks, serving D1 databases/R2 buckets, data and scratch buckets, service accounts, credentials, jobs/schedulers, artifact images and attributable cost labels. Sharing library code is desirable; sharing mutable operational resources is not. A common OA billing account/GCP project is compatible with this arrangement, but is not a hard project-level security boundary.

The new R2 site uses `/` for the whole account and `/marin-na` for the dominant bucket, without route aliases. All four source buckets belong to this one R2 deployment. Distinct source buckets within that deployment share its pipeline and serving infrastructure; different cloud deployments do not.

## Legacy CW dependencies — migration checklist

The October 12 CW audit found grants to GCS-owned `oa-gcs-usage-dvx` and `oa-gcs-usage-scratch`, shared `cf-pages-token` reads, transitional `legacyJobAccess` grants, image-repository coupling, and shared defaults/scripts. Those were the dependencies targeted by the migration; current owner reports say CW isolation/revocation succeeded. Re-audit any remaining consumers rather than assuming this historical list is current or changing only Pulumi grants.

## Additive migration gates

1. Inventory every CW-owned prefix/artifact and its readers/writers, including historical scan manifests, layer-2 blobs, static-name indexes, interval runs, logs and meta outputs. Establish ownership rather than copying entire GCS buckets. Inventory IAM, secret consumers and scheduled commands too.
2. Preview independent CW buckets, identities, narrowly scoped CF secrets and image namespace. Preserve existing resources until the new path is tested. Review creation/copy/class/retained-storage costs and cost labels before apply.
3. Copy only owned historical artifacts server-side; verify checksums and manifest counts, avoiding laptop downloads. Copying can alter storage class and modification timestamps; budget those effects explicitly. Treat index pointers/manifests as the publish boundary.
4. Switch all CW pipeline defaults and readers to the dedicated resources. Validate historical drill/diff queries and one complete scheduled scan/publish. Keep a defined rollback to previous manifests while checking the new path.
5. Remove legacy cross-deployment IAM and secret dependencies only after no consumers remain. Do not delete shared GCS buckets, GCS artifacts or old retained copies as an implicit cleanup step.

## Independent R2 first milestone

Implementation/handoff is in `wt/r2/specs/r2-launch.md`; preview/plan are in root ignored `tmp/oa-r2-preview.txt` and `tmp/oa-r2-preview-plan.json`. The new worktree reuses root dependency environments rather than making another multi-GB install. The shared CF helper now scopes newly created publisher tokens to their own derived bucket; existing live token policies are untouched and need explicit rotation to tighten them.

Create a private OA-scoped source reader for the four R2 buckets, independent remote daily scan job, dedicated scratch/data storage and serving resources. Reuse the existing bulk-list → path/bysize/search → footer publish pipeline; no ClickHouse dependency. First deliverable is a full `marin-na` listing, exact last-modified histogram and usable TM/drill, with root totals for the other buckets. Listing transfers are metadata-only but still incur Class A requests; run on a dev node/Batch, not the disk-constrained laptop.

Access logs are an independent later overlay: reference pilot and gates are in `r2-access-logging.md`. Neither the first scan nor missing logs establishes that objects are unread. Backfill and broader metadata surveys need explicit bounded jobs/cost estimates, not a blind all-history download.

## Table design follow-up

Reuse file-tree's RowsTable/custom RowGroup hooks where practical. Its stock `pathGroups` assumes lexical path order; a hierarchy with size-desc siblings needs explicit row ranges/groups. A global row budget bounds hierarchical descendant rendering. A flat “largest files” mode must query the complete backend `bysize` file population, not sort only the leaves already returned for the TM. Parent and descendant rows must not be summed together as independent storage.

[root review queue]: upstream-review-queue.md
