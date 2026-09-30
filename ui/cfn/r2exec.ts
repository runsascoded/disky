/**
 * The edge R2 CFN executor (spec `specs/staged-delete.md`, CP7): delete
 * same-account R2 objects directly from the Worker via an R2 binding, instead
 * of enqueueing for the laptop drainer. Small deletes only — above a threshold
 * the Worker's CPU/time budget won't finish, so the caller defers to the
 * drainer (the future "Batch" cell).
 *
 * A staged `r2://<bucket>/<key>` resolves to the binding `R2_<bucket>` (bucket
 * name sanitized to an identifier). No binding → not our bucket → drainer.
 */
import type { Env } from './env'

/** The bound bucket + object key for an `r2://<bucket>/<key>` URI, or null. */
export function r2Binding(env: Env, uri: string): { bucket: R2Bucket; key: string } | null {
  const m = /^r2:\/\/([^/]+)\/(.+)$/.exec(uri)
  if (!m) return null
  const [, name, key] = m
  const bindingName = `R2_${name.replace(/[^A-Za-z0-9_]/g, '_')}`
  const bucket = (env as unknown as Record<string, unknown>)[bindingName]
  return bucket ? { bucket: bucket as R2Bucket, key } : null
}

export interface KeySet {
  keys: { key: string; size: number }[]
  /** true = collection stopped early because it passed `limit` (too big for the CFN). */
  over: boolean
}

/**
 * The objects a staged URI names: the exact object at `key` (a file), plus
 * everything under `key/` (a directory prefix). Stops once it passes `limit`
 * (returns `over`), so an oversized subtree is cheap to reject.
 */
export async function collectKeys(bucket: R2Bucket, key: string, limit: number): Promise<KeySet> {
  const keys: { key: string; size: number }[] = []
  const head = await bucket.head(key)
  if (head) keys.push({ key, size: head.size })
  const prefix = key.endsWith('/') ? key : key + '/'
  let cursor: string | undefined
  do {
    const listed = await bucket.list({ prefix, cursor, limit: 1000 })
    for (const o of listed.objects) {
      keys.push({ key: o.key, size: o.size })
      if (keys.length > limit) return { keys, over: true }
    }
    cursor = listed.truncated ? listed.cursor : undefined
  } while (cursor)
  return { keys, over: false }
}

export interface UriPlan {
  uri: string
  bucket: R2Bucket
  keys: { key: string; size: number }[]
}

/**
 * Plan the inline deletion of `uris`: resolve each to a bound bucket and collect
 * its keys within a shared `limit` budget. Returns `null` if any URI isn't a
 * bound R2 bucket, or the cumulative scope exceeds `limit` — the caller then
 * enqueues the whole run for the drainer instead. Nothing is deleted here.
 */
export async function planR2Deletion(env: Env, uris: string[], limit: number): Promise<UriPlan[] | null> {
  const plans: UriPlan[] = []
  let total = 0
  for (const uri of uris) {
    const b = r2Binding(env, uri)
    if (!b) return null
    const { keys, over } = await collectKeys(b.bucket, b.key, limit - total)
    if (over) return null
    total += keys.length
    plans.push({ uri, bucket: b.bucket, keys })
  }
  return plans
}

/** Delete one URI's collected keys (bulk, ≤1000 per R2 call). Returns bytes freed. */
export async function deleteKeys(bucket: R2Bucket, keys: { key: string; size: number }[]): Promise<number> {
  for (let i = 0; i < keys.length; i += 1000) {
    await bucket.delete(keys.slice(i, i + 1000).map(k => k.key))
  }
  return keys.reduce((a, k) => a + k.size, 0)
}
