/** `/api/session-log/latency`'s wire shape (`sessionLatency.ts`), shared with `/admin/latency` (`src/LatencyPage.tsx`).
 *  Pure: no Workers or DOM types. */

/** One logged request. */
export interface Req {
  sid: string
  email: string | null
  t: number
  route: string
  /** The URL as logged (redacted by the client). */
  u: string
  s: number
  ms: number
  /** The response's `x-cache` (`hit`, `kv`, `miss`, …), `-` when it had none. */
  c: string
}

export interface Stat { n: number; p50: number; p90: number; p99: number; max: number; errors: number }

export interface LatencySummary {
  since: number
  requests: number
  /** Batches read, and whether the read stopped at `MAX_BATCHES` (the oldest left out). */
  batches: number
  capped: boolean
  routes: ({ route: string; cache: string } & Stat)[]
  users: ({ email: string } & Stat)[]
  slowest: Req[]
}
