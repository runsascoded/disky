/**
 * `CfnDashboard` — the disk-tree Vite + Cloudflare-Pages-Functions reference
 * deploy, as a Pulumi component (spec `specs/staged-delete.md` CP8; design in
 * marin-gcs-usage `specs/cf-iac.md`). One component stands up the edge that the
 * R2 / S3 / GCS deployments share: the Pages project (with its bindings + vars),
 * the D1 auth database, the R2 data bucket, and — for the CP7 CFN executor —
 * one R2 binding per bucket the edge may delete from.
 *
 * Feed it the config `disk-tree iac config` emits from `buckets.yml`, so the
 * executor bindings and delete policy have a single source of truth.
 *
 * NOTE: this is the declarative artifact; it is typechecked + applied where the
 * Pulumi SDK and CF credentials live (e.g. OA's `~/c/oa/ops`), not in this repo
 * (which ships no `@pulumi` deps). `pnpm install && pnpm typecheck` there;
 * provider field names track `@pulumi/cloudflare` v5.
 */
import * as pulumi from '@pulumi/pulumi'
import * as cloudflare from '@pulumi/cloudflare'

/** One CP7 executor binding: the Worker binds `binding` to same-account `bucket`. */
export interface ExecutorBucket {
  binding: string
  bucket: string
}

/** The large-scope executor (spec CP8, next cell): AWS Batch / GCP Batch that the
 *  drainer submits oversized deletions to. Declared here as the component's
 *  extension point; the resources themselves land with the S3/GCS cells. */
export interface BatchConfig {
  provider: 'aws' | 'gcp'
  /** Object-count above which a deletion goes to Batch instead of the CFN/inline path. */
  threshold: number
}

export interface CfnDashboardArgs {
  accountId: pulumi.Input<string>
  /** Pages project name (also the `disk-tree iac config` `project`). */
  project: string
  productionBranch?: string
  /** R2 bucket of reduced scans — the read path's `SCANS` binding. */
  dataBucket: string
  scansPrefix?: string
  /** CP7: max objects the edge CFN deletes inline before deferring to the drainer. */
  deleteThreshold?: number
  /** CP7 R2 CFN bindings (from `disk-tree iac config` `executorBuckets`). */
  executorBuckets?: ExecutorBucket[]
  /** Zero Trust Access gate (SSO allowlist). Omit for an open/public demo. */
  access?: { domain: pulumi.Input<string> }
  /** CP8 next cell — the Batch executor for oversized deletions. */
  batch?: BatchConfig
}

export class CfnDashboard extends pulumi.ComponentResource {
  readonly pagesProject: cloudflare.PagesProject
  readonly authDb: cloudflare.D1Database
  readonly dataBucket: cloudflare.R2Bucket

  constructor(name: string, args: CfnDashboardArgs, opts?: pulumi.ComponentResourceOptions) {
    super('disk-tree:iac:CfnDashboard', name, {}, opts)
    const parent = this

    const authDb = new cloudflare.D1Database(
      `${name}-auth`,
      { accountId: args.accountId, name: `${args.project}-auth` },
      { parent },
    )

    const dataBucket = new cloudflare.R2Bucket(
      `${name}-data`,
      { accountId: args.accountId, name: args.dataBucket },
      { parent },
    )

    // CP7: one R2 bucket resource per executor binding, so the Worker can delete
    // from it directly (same account, no external creds).
    const executors = (args.executorBuckets ?? []).map(
      b => new cloudflare.R2Bucket(`${name}-${b.binding}`, { accountId: args.accountId, name: b.bucket }, { parent }),
    )

    const r2Buckets: Record<string, string> = { SCANS: args.dataBucket }
    for (const b of args.executorBuckets ?? []) r2Buckets[b.binding] = b.bucket

    const pagesProject = new cloudflare.PagesProject(
      `${name}-pages`,
      {
        accountId: args.accountId,
        name: args.project,
        productionBranch: args.productionBranch ?? 'main',
        deploymentConfigs: {
          production: {
            d1Databases: { DB: authDb.id },
            r2Buckets,
            environmentVariables: {
              SCANS_PREFIX: args.scansPrefix ?? 'scans/',
              DELETE_THRESHOLD: String(args.deleteThreshold ?? 1000),
            },
          },
        },
      },
      { parent, dependsOn: [authDb, dataBucket, ...executors] },
    )

    // Secrets (SESSION_SECRET, ALLOWED_EMAILS, ACCESS_*) stay Pulumi secrets set
    // per stack, not in this component — identity-bearing config isn't code here.

    this.pagesProject = pagesProject
    this.authDb = authDb
    this.dataBucket = dataBucket
    this.registerOutputs({ pagesProjectId: pagesProject.id, authDbId: authDb.id })
  }
}
