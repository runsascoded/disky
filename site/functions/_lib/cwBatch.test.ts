import { describe, expect, it } from 'vitest'
import { batchConfig, type BatchConfig } from './batchConfig.js'
import { secretRef, sweepBatchSpec } from './cwBatch.js'

const config = (): BatchConfig => {
  const cfg = batchConfig({ GCP_PROJECT: 'p', BATCH_REGION: 'us-east4', DATA_BUCKET: 'data', SWEEP_IMAGE: 'img', SWEEP_S3_ENDPOINT: 'https://s3', DISKY_LABELS: 'app=disky,deployment=cw' }, [])
  if ('missing' in cfg) throw new Error(cfg.missing)
  return cfg
}

/** A plan-sweep job's spec as `planDispatch` / `undo` / `purge` submit it, byte for byte (key order included: the
 *  request body is `JSON.stringify(spec)`). */
const SWEEP_SPEC = {
  taskGroups: [{
    taskCount: 1,
    taskSpec: {
      runnables: [{
        container: {
          imageUri: 'img',
          entrypoint: '/bin/bash',
          commands: ['-c', 'run'],
          volumes: ['/mnt/disks/gcs/data:/gcs/data:rw'],
        },
      }],
      computeResource: { cpuMilli: 8000, memoryMib: 16000 },
      maxRetryCount: 0,
      maxRunDuration: '86400s',
      volumes: [{
        gcs: { remotePath: 'data' },
        mountPath: '/mnt/disks/gcs/data',
        mountOptions: ['--implicit-dirs'],
      }],
      environment: {
        variables: {
          DATA_BUCKET: 'data', SWEEP_BUCKET: 'bkt', SWEEP_S3_ENDPOINT: 'https://s3',
          AWS_DEFAULT_REGION: 'us-east-1',
          AWS_EC2_METADATA_DISABLED: 'true',
          DT_S3_ADDRESSING_STYLE: 'virtual',
          JOB_ID: 'j', SWEEP_DATE: '2026-11-22',
        },
        secretVariables: {
          AWS_ACCESS_KEY_ID: 'projects/p/secrets/cw-s3-access-key-id/versions/latest',
          AWS_SECRET_ACCESS_KEY: 'projects/p/secrets/cw-s3-secret-access-key/versions/latest',
          SITE_TOKEN: 'projects/p/secrets/cw-s3-job-grant/versions/latest',
        },
      },
    },
  }],
  allocationPolicy: {
    instances: [{ policy: { machineType: 'n2-standard-8', bootDisk: { type: 'pd-balanced', sizeGb: '100' } } }],
    serviceAccount: { email: 'job@p' },
    location: { allowedLocations: ['regions/us-east4'] },
    labels: { app: 'disky', deployment: 'cw', component: 'sweep-undo' },
  },
  logsPolicy: { destination: 'CLOUD_LOGGING' },
  labels: { app: 'disky', deployment: 'cw', component: 'sweep-undo' },
}

describe('sweepBatchSpec: the plan-sweep executor’s job', () => {
  it('is exactly the spec cw’s sweeps have submitted', () => {
    const cfg = config()
    const spec = sweepBatchSpec(cfg, 'job@p', 'run', 'bkt', { JOB_ID: 'j', SWEEP_DATE: '2026-11-22' }, { SITE_TOKEN: secretRef(cfg, 'cw-s3-job-grant') }, 'sweep-undo')
    expect(JSON.stringify(spec)).toBe(JSON.stringify(SWEEP_SPEC))
  })
})
