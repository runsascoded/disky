/** Per-isolate memos of promises shared across requests — with a watchdog.
 *
 * Workers freeze a cancelled request's pending promises (a viewer navigating
 * away mid-compute): they never settle and never reject, so a plain
 * `Map<key, Promise>` can hold an entry nothing will ever resolve, and every
 * later request that awaits it is cut off by the runtime as "hung" until the
 * entry is evicted. Each caller therefore races the shared promise against a
 * timer of its own (a pending timer is progress, so the runtime waits); on
 * timeout it evicts the entry and computes afresh in its own context, where a
 * second stall is a real failure. Holders of a real stake (a lock, a slot)
 * must never be shared this way at all — there is no release to wait for. */
export async function shared<T>(map: Map<string, Promise<T>>, key: string, make: () => Promise<T>, waitMs: number): Promise<T> {
  const start = (): Promise<T> => {
    const q = make()
    map.set(key, q)
    q.catch(() => { if (map.get(key) === q) map.delete(key) })
    return q
  }
  const p = map.get(key) ?? start()
  const race = (q: Promise<T>, ms: number): Promise<T> => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const stall = new Promise<never>((_, rej) => { timer = setTimeout(() => rej(new Stall(key, ms)), ms) })
    return Promise.race([q, stall]).finally(() => clearTimeout(timer))
  }
  try {
    return await race(p, waitMs)
  } catch (e) {
    if (!(e instanceof Stall)) throw e
    if (map.get(key) === p) map.delete(key)
    const q = start()
    try {
      return await race(q, waitMs)
    } catch (e2) {
      if (e2 instanceof Stall && map.get(key) === q) map.delete(key)
      throw e2
    }
  }
}

class Stall extends Error {
  constructor(key: string, ms: number) {
    super(`shared ${key}: no result after ${ms} ms`)
  }
}

/** Where this deployment's snapshots live in the data bucket: `snapshots/`
 * for the default (GCS) store, `snapshots/<SNAPSHOTS_SUBDIR>/` for a store
 * published under a named subdir (cw-s3: `cw`). Every Function that lists or
 * reads snapshot dirs goes through this, so a deployment can't list another
 * store's scans (the 2026-09-16 preview charted the GCS fleet on cw). */
export const snapshotsPrefix = (env: { SNAPSHOTS_SUBDIR?: string }): string => {
  const sub = (env.SNAPSHOTS_SUBDIR ?? '').replace(/^\/+|\/+$/g, '')
  return sub ? `snapshots/${sub}/` : 'snapshots/'
}
