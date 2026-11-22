// The plan-sweep executor's Batch job spec (cw-s3's bridge) and its run
// paths. Everything deployment-specific comes from the batch config
// (`batchConfig.ts`: image, data bucket, S3 endpoint, project, region); the
// GCP auth lives in `gcp.ts`. A plan-sweep run deletes from one bucket, so it
// always runs in the deployment's Batch region (`BATCH_REGION`), which its
// callers require: the job mounts the data bucket, so that is its region.
import type { BatchConfig } from "./batchConfig.js"
import { labelBatchSpec } from "./costLabels.js"
import { batchJobsUrl } from "./gcp.js"

export const secretRef = (cfg: BatchConfig, name: string): string =>
  `projects/${cfg.project}/secrets/${name}/versions/latest`

/** The `GCP_PROJECT` … `SWEEP_S3_ENDPOINT` a plan-sweep dispatch (`planDispatch`, `plan-sweep/undo`, `plan-sweep/purge`)
 *  needs before it submits: a deployment that leaves any unset has no plan-sweep executor. */
export const PLAN_SWEEP_NEEDS = ['GCP_PROJECT', 'BATCH_REGION', 'DATA_BUCKET', 'SWEEP_IMAGE', 'SWEEP_S3_ENDPOINT'] as const

/** A Batch job running `bash -c <script>` on `image` as `jobSa`, in the deployment's Batch region, the data bucket
 *  FUSE-mounted at /gcs/<bucket>, with `DATA_BUCKET` and exactly the `env` and Secret Manager refs (`secrets`) given: no
 *  store credentials of its own, so a job that reads a store names its keys. `component` is its cost-attribution label
 *  (with `cfg.labels`). */
export function batchTaskSpec(cfg: BatchConfig, jobSa: string, image: string, script: string, env: Record<string, string>, secrets: Record<string, string>, component: string): unknown {
  const DATA_BUCKET = cfg.dataBucket
  return labelBatchSpec({
    taskGroups: [{
      taskCount: 1,
      taskSpec: {
        runnables: [{
          container: {
            imageUri: image,
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
          variables: { DATA_BUCKET, ...env },
          secretVariables: { ...secrets },
        },
      },
    }],
    allocationPolicy: {
      instances: [{ policy: { machineType: "n2-standard-8", bootDisk: { type: "pd-balanced", sizeGb: "100" } } }],
      serviceAccount: { email: jobSa },
      location: { allowedLocations: [`regions/${cfg.region}`] },
    },
    logsPolicy: { destination: "CLOUD_LOGGING" },
  }, cfg.labels, component)
}

/** The plan-sweep executor's job (cw-s3's bridge): `batchTaskSpec` on `SWEEP_IMAGE`, with the executor's S3 env and
 *  cw-s3's source keys. `bucket` is the bucket the executor acts on (`SWEEP_BUCKET`); `env` merges in per-job
 *  variables, `secrets` per-job Secret Manager refs; `component` the jobs' cost-attribution label. */
export function sweepBatchSpec(cfg: BatchConfig, jobSa: string, script: string, bucket: string, env: Record<string, string> = {}, secrets: Record<string, string> = {}, component = "sweep"): unknown {
  return batchTaskSpec(cfg, jobSa, cfg.image, script, {
    SWEEP_BUCKET: bucket, SWEEP_S3_ENDPOINT: cfg.s3Endpoint,
    AWS_DEFAULT_REGION: "us-east-1",
    AWS_EC2_METADATA_DISABLED: "true",
    DT_S3_ADDRESSING_STYLE: "virtual",
    ...env,
  }, {
    AWS_ACCESS_KEY_ID: secretRef(cfg, "cw-s3-access-key-id"),
    AWS_SECRET_ACCESS_KEY: secretRef(cfg, "cw-s3-secret-access-key"),
    ...secrets,
  }, component)
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
