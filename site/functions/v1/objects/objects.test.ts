import { describe, expect, it } from 'vitest'
import type { Env } from '../../_lib/auth'
import { objectBuckets, onRequest } from './[[path]]'

// No store creds: a request that passes every check stops at the 503, so the
// status says which check it landed on without touching a bucket. A localhost
// request is the full-scope dev identity (`identify`).
const APP: Env = { BASE_SCOPE: 'gcs', OBJECT_BUCKETS: 'marin-a, marin-b' } as Env
const call = async (env: Env, host: string, bucket: string) => {
  const r = await onRequest({ request: new Request(`https://${host}/v1/objects/${bucket}/get?path=x/y.parquet`), env })
  return [r.status, (await r.text()).trim()]
}

describe('/v1/objects', () => {
  it('parses OBJECT_BUCKETS', () => {
    expect(objectBuckets(APP)).toEqual(['marin-a', 'marin-b'])
    expect(objectBuckets({} as Env)).toEqual([])
  })
  it('refuses an anonymous request', async () => {
    expect(await call(APP, 'deploy.example.test', 'marin-a')).toEqual([401, '{"error":"unauthenticated"}'])
  })
  it('refuses a public deploy\'s anonymous identity', async () => {
    expect(await call({ ...APP, PUBLIC_READ: '1' } as Env, 'deploy.example.test', 'marin-a')).toEqual([401, '{"error":"unauthenticated"}'])
  })
  it('serves only the listed buckets', async () => {
    expect(await call(APP, 'localhost', 'other-bucket')).toEqual([404, '{"error":"no such object bucket"}'])
    expect(await call({ BASE_SCOPE: 'gcs' } as Env, 'localhost', 'marin-a')).toEqual([404, '{"error":"no such object bucket"}'])
  })
  it('lets a member through to the proxy', async () => {
    expect(await call(APP, 'localhost', 'marin-b')).toEqual([503, 'object proxy not configured (missing store creds)'])
  })
})
