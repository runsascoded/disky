/**
 * The capture trigger (specs/capture-ingest-trigger.md): a finished capture's
 * `_SUCCESS.json` lands in R2 → an event on the captures queue (`CaptureTrigger`) →
 * here → AWS Batch `SubmitJob` for the deployment's ingest (`BatchIngest`). The laptop then needs only
 * R2 write; nothing on it holds AWS keys.
 *
 * Queues deliver at least once, so each capture dir carries an `_INGEST.json`
 * marker: written as `submitting` before the call, completed with the job id
 * after. A marker in any state skips the capture, so a redelivery never submits
 * twice; a `submitting` marker left by a crash between the two writes is the
 * one case to re-ingest by hand (`aws/submit`).
 */
import { AwsClient } from 'aws4fetch'

export interface Env {
  BUCKET: R2Bucket
  BUCKET_NAME: string
  AWS_REGION: string
  AWS_ACCESS_KEY_ID: string
  AWS_SECRET_ACCESS_KEY: string
  JOB_QUEUE: string
  JOB_DEFINITION: string
}

/** The R2 event notification's message body (the fields used here). */
export interface R2Event {
  bucket: string
  action: string
  object: { key: string }
}

const SUCCESS = '_SUCCESS.json'
export const MARKER = '_INGEST.json'

/** `captures/<host>/<root>/<stamp>` for a capture's `_SUCCESS.json` key, else null. */
export function captureDir(key: string): string | null {
  if (!key.startsWith('captures/') || !key.endsWith(`/${SUCCESS}`)) return null
  const dir = key.slice(0, -(SUCCESS.length + 1))
  return dir.split('/').length >= 4 ? dir : null
}

/** A Batch job name for a capture dir: `[A-Za-z0-9_-]`, ≤ 128 chars. */
export const jobName = (dir: string): string =>
  `ingest-${dir.replace(/^captures\//, '').replace(/[^A-Za-z0-9_-]+/g, '-')}`.slice(0, 128)

export type Submit = (uri: string, name: string) => Promise<string>

export type Outcome =
  | { kind: 'ignored'; key: string }
  | { kind: 'skipped'; dir: string; marker: unknown }
  | { kind: 'submitted'; dir: string; jobId: string }

/** One event → its outcome. Throws (→ retry) on any failure before the job id
 *  is recorded; a failed submit takes its `submitting` marker back down. */
export async function handle(ev: R2Event, env: Pick<Env, 'BUCKET' | 'BUCKET_NAME'>, submit: Submit, now = () => new Date()): Promise<Outcome> {
  const dir = ev.bucket === env.BUCKET_NAME ? captureDir(ev.object.key) : null
  if (!dir) return { kind: 'ignored', key: ev.object.key }
  const markerKey = `${dir}/${MARKER}`
  const existing = await env.BUCKET.get(markerKey)
  if (existing) return { kind: 'skipped', dir, marker: await existing.json() }
  const uri = `r2://${env.BUCKET_NAME}/${dir}`
  await env.BUCKET.put(markerKey, JSON.stringify({ state: 'submitting', uri, at: now().toISOString() }))
  let jobId: string
  try {
    jobId = await submit(uri, jobName(dir))
  } catch (e) {
    await env.BUCKET.delete(markerKey)
    throw e
  }
  await env.BUCKET.put(markerKey, JSON.stringify({ state: 'submitted', uri, job_id: jobId, submitted: now().toISOString() }))
  return { kind: 'submitted', dir, jobId }
}

/** Batch `SubmitJob` (REST, SigV4 via aws4fetch) → the job id. */
export function batchSubmit(env: Env): Submit {
  const aws = new AwsClient({ accessKeyId: env.AWS_ACCESS_KEY_ID, secretAccessKey: env.AWS_SECRET_ACCESS_KEY, region: env.AWS_REGION, service: 'batch' })
  return async (uri, name) => {
    const res = await aws.fetch(`https://batch.${env.AWS_REGION}.amazonaws.com/v1/submitjob`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ jobName: name, jobQueue: env.JOB_QUEUE, jobDefinition: env.JOB_DEFINITION, containerOverrides: { command: [uri] } }),
    })
    if (!res.ok) throw new Error(`SubmitJob ${res.status}: ${(await res.text()).slice(0, 300)}`)
    return (await res.json() as { jobId: string }).jobId
  }
}

export default {
  async queue(batch: MessageBatch<R2Event>, env: Env): Promise<void> {
    const submit = batchSubmit(env)
    for (const msg of batch.messages) {
      try {
        const out = await handle(msg.body, env, submit)
        console.log(JSON.stringify(out))
        msg.ack()
      } catch (e) {
        console.error(`capture-trigger: ${msg.body?.object?.key}: ${(e as Error).message}`)
        msg.retry({ delaySeconds: 60 })
      }
    }
  },
} satisfies ExportedHandler<Env, R2Event>
