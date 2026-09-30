import { describe, expect, it } from 'vitest'
import type { Env } from '../../_lib/auth'
import { onRequest } from './[[path]]'

// No store creds: a request that passes the gate stops at the 503 — so the
// status says which side of the gate it landed on without touching a bucket.
const APP: Env = { BASE_SCOPE: 'cw' } as Env
const call = (env: Env) => onRequest({ request: new Request('https://deploy.example.test/v1/files/list?prefix=snapshots/'), env })

describe('/v1/files is gated like /data', () => {
  it('an anonymous request to an app-auth deploy is refused', async () => {
    expect((await call(APP)).status).toBe(401)
  })
  it('a public deploy (PUBLIC_READ) lets it through to the proxy', async () => {
    const r = await call({ ...APP, PUBLIC_READ: '1' } as Env)
    expect([r.status, await r.text()]).toEqual([503, 'scan-browser proxy not configured (missing store creds)'])
  })
})
