import { describe, expect, it } from 'vitest'
import { captureDir, handle, jobName, type R2Event, type Submit } from './index'

/** An in-memory stand-in for the R2 binding: the three calls `handle` makes. */
function bucket(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init))
  const b = {
    get: async (k: string) => (m.has(k) ? { json: async () => JSON.parse(m.get(k)!) } : null),
    put: async (k: string, v: string) => { m.set(k, v) },
    delete: async (k: string) => { m.delete(k) },
  }
  return { m, env: { BUCKET: b as unknown as R2Bucket, BUCKET_NAME: 'disk-tree' } }
}

const ev = (key: string, bucketName = 'disk-tree'): R2Event => ({ bucket: bucketName, action: 'PutObject', object: { key } })
const DIR = 'captures/m3/root/20261001T220000Z'
const NOW = () => new Date('2026-10-01T22:06:00Z')

/** Records each submit; returns `job-<n>`, or throws on the listed calls. */
function submitter(failOn: number[] = []) {
  const calls: [string, string][] = []
  const submit: Submit = async (uri, name) => {
    calls.push([uri, name])
    if (failOn.includes(calls.length)) throw new Error('SubmitJob 500: boom')
    return `job-${calls.length}`
  }
  return { calls, submit }
}

describe('captureDir', () => {
  it("is the dir of a capture's `_SUCCESS.json`, else null", () => {
    expect([
      `${DIR}/_SUCCESS.json`,
      `${DIR}/part-0000.parquet`,
      'captures/m3/_SUCCESS.json',
      'listing/laptop/2026-10-01/_SUCCESS.json',
      `${DIR}/_SUCCESS.json.tmp`,
    ].map(captureDir)).toEqual([DIR, null, null, null, null])
  })
})

describe('jobName', () => {
  it('is Batch-safe and bounded', () => {
    expect([jobName(DIR), jobName(`captures/m3/Users~ryan/${'x'.repeat(200)}`).length]).toEqual(['ingest-m3-root-20261001T220000Z', 128])
  })
})

describe('handle', () => {
  it('submits a new capture and records the job in `_INGEST.json`', async () => {
    const { m, env } = bucket()
    const s = submitter()
    expect(await handle(ev(`${DIR}/_SUCCESS.json`), env, s.submit, NOW)).toEqual({ kind: 'submitted', dir: DIR, jobId: 'job-1' })
    expect(s.calls).toEqual([[`r2://disk-tree/${DIR}`, 'ingest-m3-root-20261001T220000Z']])
    expect(JSON.parse(m.get(`${DIR}/_INGEST.json`)!)).toEqual({ state: 'submitted', uri: `r2://disk-tree/${DIR}`, job_id: 'job-1', submitted: '2026-10-01T22:06:00.000Z' })
  })

  it('a redelivered event skips: one submit per capture', async () => {
    const { env } = bucket()
    const s = submitter()
    await handle(ev(`${DIR}/_SUCCESS.json`), env, s.submit, NOW)
    expect(await handle(ev(`${DIR}/_SUCCESS.json`), env, s.submit, NOW)).toEqual({
      kind: 'skipped', dir: DIR,
      marker: { state: 'submitted', uri: `r2://disk-tree/${DIR}`, job_id: 'job-1', submitted: '2026-10-01T22:06:00.000Z' },
    })
    expect(s.calls.length).toBe(1)
  })

  it('a failed submit throws (→ retry) and takes its marker down, so the retry submits', async () => {
    const { m, env } = bucket()
    const s = submitter([1])
    await expect(handle(ev(`${DIR}/_SUCCESS.json`), env, s.submit, NOW)).rejects.toThrow('SubmitJob 500: boom')
    expect([...m.keys()]).toEqual([])
    expect(await handle(ev(`${DIR}/_SUCCESS.json`), env, s.submit, NOW)).toEqual({ kind: 'submitted', dir: DIR, jobId: 'job-2' })
  })

  it('ignores other keys and other buckets', async () => {
    const { m, env } = bucket()
    const s = submitter()
    expect([
      await handle(ev(`${DIR}/part-0000.parquet`), env, s.submit, NOW),
      await handle(ev(`${DIR}/_SUCCESS.json`, 'other'), env, s.submit, NOW),
    ]).toEqual([
      { kind: 'ignored', key: `${DIR}/part-0000.parquet` },
      { kind: 'ignored', key: `${DIR}/_SUCCESS.json` },
    ])
    expect([s.calls, [...m.keys()]]).toEqual([[], []])
  })
})
