/** Tests' requests abandoning isolate-shared work (`shared.ts`): Workers freeze a request's pending I/O once it is
 *  cancelled or ends, so a read it started never settles — for it, or for any later request awaiting it. */
import { JOIN, Req, STALLED, within } from './shared'
import type { Blobs } from './staticNames'

/** `b` with its first read matching `hang` (method, key) never settling; `hung()`: whether it was reached. */
export function hangFirst(b: Blobs, hang: (method: 'range' | 'suffix' | 'json' | 'list', key: string) => boolean): Blobs & { hung: () => boolean } {
  let hung = false
  const gate = <T>(method: 'range' | 'suffix' | 'json' | 'list', key: string, go: () => Promise<T>): Promise<T> => {
    if (hung || !hang(method, key)) return go()
    hung = true
    return new Promise<T>(() => {})
  }
  return {
    range: (key, offset, length) => gate('range', key, () => b.range(key, offset, length)),
    suffix: (key, n) => gate('suffix', key, () => b.suffix(key, n)),
    json: <T>(key: string) => gate('json', key, () => b.json<T>(key)),
    ...(b.list ? { list: (prefix: string) => gate('list', prefix, () => b.list!(prefix)) } : {}),
    hung: () => hung,
  }
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

/** The join bound these tests run under (every kind's: cold, min and max alike). */
export const BOUND = 200

/** Request A runs `op` until its read hangs (`hung`), then is cancelled (`cancel`) or just ends (`end`: nothing
 *  tells the isolate; its I/O is frozen); request B then runs `op`. B's result, or `STALLED` if it hasn't settled
 *  within `ms` (real time). Under `BOUND`, a cancel's B never waits on A's work, an end's waits out the bound once. */
export async function abandoned<T>(op: (req: Req) => Promise<T>, hung: () => boolean, how: 'cancel' | 'end', ms: number): Promise<T | typeof STALLED> {
  const join0 = { ...JOIN }
  Object.assign(JOIN, { min: BOUND, max: BOUND, cold: BOUND, budget: 10 * BOUND })
  try {
    const ctl = new AbortController()
    void op(new Req(ctl.signal)).catch(() => {})
    for (let t = 0; !hung(); t += 5) {
      if (t > 2000) throw new Error('request A never reached its hanging read')
      await sleep(5)
    }
    if (how === 'cancel') ctl.abort()
    return await within(op(new Req()), ms)
  } finally {
    Object.assign(JOIN, join0)
  }
}
