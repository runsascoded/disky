/**
 * SigV4 query-string presigning (WebCrypto, no dependency): a URL anyone holding it may `method` until it expires,
 * signing the `host` header only and an unsigned payload — what the device API hands the app for capture uploads
 * (specs/device-api.md). Path-style or virtual-host URLs alike: the caller passes the object's full URL.
 */

export interface PresignOpts {
  method: 'GET' | 'PUT'
  /** The object's URL, unsigned (`https://<endpoint>/<bucket>/<key>`). */
  url: string
  accessKeyId: string
  secretAccessKey: string
  region: string
  /** Seconds the URL stays valid (1 – 604800). */
  expires: number
  now: Date
  service?: string
}

const enc = new TextEncoder()
const hex = (b: ArrayBuffer): string => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('')
const sha256 = async (s: string): Promise<string> => hex(await crypto.subtle.digest('SHA-256', enc.encode(s)))
async function hmac(key: ArrayBuffer | Uint8Array, msg: string): Promise<ArrayBuffer> {
  const k = await crypto.subtle.importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  return crypto.subtle.sign('HMAC', k, enc.encode(msg))
}

/** RFC 3986 percent-encoding, as SigV4 wants it (`encodeURIComponent` leaves `!'()*`). */
export const uriEncode = (s: string): string =>
  encodeURIComponent(s).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)

export async function presign(o: PresignOpts): Promise<string> {
  if (!(o.expires >= 1 && o.expires <= 604800)) throw new Error(`presign: expires ${o.expires} out of range`)
  const u = new URL(o.url)
  const service = o.service ?? 's3'
  const amzDate = o.now.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '')
  const day = amzDate.slice(0, 8)
  const scope = `${day}/${o.region}/${service}/aws4_request`
  const path = u.pathname.split('/').map(seg => uriEncode(decodeURIComponent(seg))).join('/')
  const params: [string, string][] = [
    ['X-Amz-Algorithm', 'AWS4-HMAC-SHA256'],
    ['X-Amz-Credential', `${o.accessKeyId}/${scope}`],
    ['X-Amz-Date', amzDate],
    ['X-Amz-Expires', String(o.expires)],
    ['X-Amz-SignedHeaders', 'host'],
  ]
  const query = params.map(([k, v]) => `${uriEncode(k)}=${uriEncode(v)}`).sort().join('&')
  const canonical = [o.method, path, query, `host:${u.host}`, '', 'host', 'UNSIGNED-PAYLOAD'].join('\n')
  const toSign = ['AWS4-HMAC-SHA256', amzDate, scope, await sha256(canonical)].join('\n')
  let key: ArrayBuffer = await hmac(enc.encode(`AWS4${o.secretAccessKey}`), day)
  for (const part of [o.region, service, 'aws4_request']) key = await hmac(key, part)
  const signature = hex(await hmac(key, toSign))
  return `${u.origin}${path}?${query}&X-Amz-Signature=${signature}`
}
