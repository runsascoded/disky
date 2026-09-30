// cw-s3's sweep dispatch bridge: the CoreWeave-scan Batch job spec and the
// constants it needs (image, data bucket, CAIOS bucket + endpoint). The GCP
// auth (`gcpToken`) and the project / region / jobs URL live in `gcp.ts`,
// which is shared verbatim with gcs; everything CoreWeave-specific is here.
// The CoreWeave bucket is single-region, so a sweep always runs in one Batch
// region (`BATCH_REGION`).
import { BATCH_REGION, GCP_PROJECT, batchJobsUrl } from "./gcp.js"

export const JOB_SA = `gcs-usage-job@${GCP_PROJECT}.iam.gserviceaccount.com`
// The CoreWeave-scan image (built by job/build.sh, tag `cw`); carries the marin
// CLI with `sweep manifest`/`execute` and the `.[s3]` (boto3) extras.
export const CW_IMAGE = `us-central1-docker.pkg.dev/${GCP_PROJECT}/cloud-run-source-deploy/gcs-usage-snapshot:cw`

// Storage: the shared data bucket (plan.json / manifest / logs land here, and
// it's FUSE-mounted into the Batch job at /gcs/<bucket>); the CoreWeave bucket
// + endpoint the executor deletes from.
export const DATA_BUCKET = "oa-gcs-usage-dvx"
// Every bucket the scan covers (specs/cw-multi-bucket.md §1; mirrors
// job/cw-run.sh `CW_BUCKETS`): the first is the primary — the 1 PB bucket,
// the sweep default. A plan is dispatched against exactly one of these.
export const CW_BUCKETS = ["marin-us-east-02a", "hero-checkpoints"] as const
export const CW_BUCKET: string = CW_BUCKETS[0]
export const CW_ENDPOINT = "https://cwobject.com"

export const secretRef = (name: string): string =>
  `projects/${GCP_PROJECT}/secrets/${name}/versions/latest`

/** The standard cw-sweep Batch job spec (mirrors job/cw-batch-submit.sh): the cw
 * image running `bash -c <script>`, the data bucket FUSE-mounted at /gcs/<bucket>,
 * and the CAIOS S3 creds from Secret Manager. `env` merges in per-job variables,
 * `secrets` per-job Secret Manager refs. */
export function sweepBatchSpec(script: string, env: Record<string, string> = {}, secrets: Record<string, string> = {}): unknown {
  return {
    taskGroups: [{
      taskCount: 1,
      taskSpec: {
        runnables: [{
          container: {
            imageUri: CW_IMAGE,
            entrypoint: "/bin/bash",
            commands: ["-c", script],
            volumes: [`/mnt/disks/gcs/${DATA_BUCKET}:/gcs/${DATA_BUCKET}:rw`],
          },
        }],
        computeResource: { cpuMilli: 8000, memoryMib: 16000 },
        maxRetryCount: 0,
        maxRunDuration: "86400s",
        volumes: [{
          gcs: { remotePath: DATA_BUCKET },
          mountPath: `/mnt/disks/gcs/${DATA_BUCKET}`,
          mountOptions: ["--implicit-dirs"],
        }],
        environment: {
          variables: {
            DATA_BUCKET, CW_BUCKET, CW_ENDPOINT,
            AWS_DEFAULT_REGION: "us-east-1",
            AWS_EC2_METADATA_DISABLED: "true",
            DT_S3_ADDRESSING_STYLE: "virtual",
            ...env,
          },
          secretVariables: {
            AWS_ACCESS_KEY_ID: secretRef("cw-s3-access-key-id"),
            AWS_SECRET_ACCESS_KEY: secretRef("cw-s3-secret-access-key"),
            ...secrets,
          },
        },
      },
    }],
    allocationPolicy: {
      instances: [{ policy: { machineType: "n2-standard-8", bootDisk: { type: "pd-balanced", sizeGb: "100" } } }],
      serviceAccount: { email: JOB_SA },
      location: { allowedLocations: [`regions/${BATCH_REGION}`] },
    },
    logsPolicy: { destination: "CLOUD_LOGGING" },
  }
}

/** Submit a Batch job; returns { ok, status, text } (caller maps errors). */
export async function submitBatch(token: string, jobId: string, spec: unknown): Promise<{ ok: boolean; status: number; text: string }> {
  const r = await fetch(`${batchJobsUrl(BATCH_REGION)}?job_id=${jobId}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(spec),
  })
  return { ok: r.ok, status: r.status, text: await r.text() }
}

/** Compact UTC stamp `YYYYMMDD-HHMMSS` for a job id. */
export const jobStamp = (): string =>
  new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15).toLowerCase()

/** The FUSE-mount path of a run dir inside the Batch job. */
export const runMountPath = (jobId: string): string => `/gcs/${DATA_BUCKET}/sweep/cw/runs/${jobId}`
export const runGsPath = (jobId: string): string => `gs://${DATA_BUCKET}/sweep/cw/runs/${jobId}`
