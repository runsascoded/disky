// The plan-sweep executor's Batch job spec (cw-s3's bridge) and its run
// paths. Everything deployment-specific comes from the batch config
// (`batchConfig.ts`: image, data bucket, S3 endpoint, project, region); the
// GCP auth lives in `gcp.ts`. A plan-sweep run deletes from one bucket, so it
// always runs in the default Batch region.
import type { BatchConfig } from "./batchConfig.js"
import { batchJobsUrl } from "./gcp.js"

export const secretRef = (cfg: BatchConfig, name: string): string =>
  `projects/${cfg.project}/secrets/${name}/versions/latest`

/** The standard plan-sweep Batch job spec: the image running `bash -c <script>`
 * as `jobSa`, the data bucket FUSE-mounted at /gcs/<bucket>, and the S3 creds
 * from Secret Manager. `bucket` is the bucket the executor acts on
 * (`SWEEP_BUCKET`); `env` merges in per-job variables, `secrets` per-job Secret
 * Manager refs. */
export function sweepBatchSpec(cfg: BatchConfig, jobSa: string, script: string, bucket: string, env: Record<string, string> = {}, secrets: Record<string, string> = {}): unknown {
  const DATA_BUCKET = cfg.dataBucket
  return {
    taskGroups: [{
      taskCount: 1,
      taskSpec: {
        runnables: [{
          container: {
            imageUri: cfg.image,
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
            DATA_BUCKET, SWEEP_BUCKET: bucket, SWEEP_S3_ENDPOINT: cfg.s3Endpoint,
            AWS_DEFAULT_REGION: "us-east-1",
            AWS_EC2_METADATA_DISABLED: "true",
            DT_S3_ADDRESSING_STYLE: "virtual",
            ...env,
          },
          secretVariables: {
            AWS_ACCESS_KEY_ID: secretRef(cfg, "cw-s3-access-key-id"),
            AWS_SECRET_ACCESS_KEY: secretRef(cfg, "cw-s3-secret-access-key"),
            ...secrets,
          },
        },
      },
    }],
    allocationPolicy: {
      instances: [{ policy: { machineType: "n2-standard-8", bootDisk: { type: "pd-balanced", sizeGb: "100" } } }],
      serviceAccount: { email: jobSa },
      location: { allowedLocations: [`regions/${cfg.region}`] },
    },
    logsPolicy: { destination: "CLOUD_LOGGING" },
  }
}

/** Submit a Batch job; returns { ok, status, text } (caller maps errors). */
export async function submitBatch(cfg: BatchConfig, token: string, jobId: string, spec: unknown): Promise<{ ok: boolean; status: number; text: string }> {
  const r = await fetch(`${batchJobsUrl(cfg, cfg.region)}?job_id=${jobId}`, {
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
export const runMountPath = (cfg: BatchConfig, jobId: string): string => `/gcs/${cfg.dataBucket}/sweep/cw/runs/${jobId}`
export const runGsPath = (cfg: BatchConfig, jobId: string): string => `gs://${cfg.dataBucket}/sweep/cw/runs/${jobId}`
