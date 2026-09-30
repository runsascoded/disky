// GCP auth for the sweep console's dispatch bridge: the `GCP_SA_KEY` Pages
// secret (a dedicated SA — Batch-submit + actAs the job SA, nothing else)
// self-signs a JWT that is exchanged for a cloud-platform access token. No
// SDK — Workers-compatible WebCrypto only. Tokens are memoized per isolate
// (50 min of their 60) via `shared`, so the console's polls don't re-mint.
import { shared } from './shared.js'

export const GCP_PROJECT = 'oa-internal-450019'
export const BATCH_REGION = 'us-central1'
export const batchJobsUrl = (region: string): string => `https://batch.googleapis.com/v1/projects/${GCP_PROJECT}/locations/${region}/jobs`
export const BATCH_JOBS = batchJobsUrl(BATCH_REGION)
/** Each marin bucket's region. A sweep executor runs *there*: every listing
 * page and every delete sub-request is a round trip to the bucket, and the
 * 2026-09-11 eu-west4 run from us-central1 crawled at a ninth of the
 * colocated buckets' rate. */
export const BUCKET_REGION: Record<string, string> = {
  'marin-us-east5': 'us-east5',
  'marin-us-central1': 'us-central1',
  // The bucket sits in us-central2, but Batch has no us-central2 location
  // (`GET /v1/projects/…/locations` omits it, and the 2026-09-11 17:20Z
  // dispatch there failed): its nearest Batch region, where the first
  // central2 run reached the bucket's write ceiling anyway.
  'marin-us-central2': 'us-central1',
  'marin-eu-west4': 'europe-west4',
  'marin-us-west4': 'us-west4',
  'marin-us-east1': 'us-east1',
}
/** Where a sweep job runs: the one region its bucket cut lives in, else the
 * default (an uncut run touches several regions; nowhere is right for all). */
export const batchRegionFor = (buckets: readonly string[]): string => {
  const regions = new Set(buckets.map(b => BUCKET_REGION[b]).filter(Boolean))
  return regions.size === 1 ? [...regions][0] : BATCH_REGION
}
/** Every region a sweep job may have been dispatched to. */
export const BATCH_REGIONS: readonly string[] = [...new Set([BATCH_REGION, ...Object.values(BUCKET_REGION)])]

const b64url = (buf: ArrayBuffer | Uint8Array): string => {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  let s = ''
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function mint(saKey: string): Promise<{ token: string; exp: number }> {
  const sa = JSON.parse(saKey) as { client_email: string; private_key: string }
  const pem = sa.private_key.replace(/-----[A-Z ]+-----|\s/g, '')
  const der = Uint8Array.from(atob(pem), c => c.charCodeAt(0))
  const key = await crypto.subtle.importKey(
    'pkcs8', der, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['sign'],
  )
  const now = Math.floor(Date.now() / 1000)
  const enc = new TextEncoder()
  const unsigned = `${b64url(enc.encode(JSON.stringify({ alg: 'RS256', typ: 'JWT' })))}.${b64url(enc.encode(JSON.stringify({
    iss: sa.client_email,
    scope: 'https://www.googleapis.com/auth/cloud-platform',
    aud: 'https://oauth2.googleapis.com/token',
    iat: now,
    exp: now + 3600,
  })))}`
  const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, enc.encode(unsigned))
  const r = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion: `${unsigned}.${b64url(sig)}`,
    }),
  })
  if (!r.ok) throw new Error(`token exchange: ${r.status} ${await r.text()}`)
  return { token: ((await r.json()) as { access_token: string }).access_token, exp: now + 3000 }
}

const memo = new Map<string, Promise<{ token: string; exp: number }>>()

/** Service-account JWT → OAuth access token (cloud-platform scope), memoized. */
export async function gcpToken(saKey: string): Promise<string> {
  const k = saKey.slice(-32) // key material never leaves the isolate; the memo key is a tail
  const got = await shared(memo, k, () => mint(saKey), 10_000)
  if (got.exp > Date.now() / 1000) return got.token
  memo.delete(k)
  return (await shared(memo, k, () => mint(saKey), 10_000)).token
}
