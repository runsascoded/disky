/** The fleet document's shape (`fleet.ts` reads it, `/api/fleet` serves it), with no imports: the page (`src/`) reads
 *  it too. */

export interface FleetRun {
  run_id: string
  scan: string
  status: string
  /** What started it: `schedule` (the deployment's cron), `manual` (a hand submit), null when the deployment doesn't
   *  record triggers. */
  trigger: 'schedule' | 'manual' | null
  /** Epoch s the job started. */
  started: number | null
  secs: number | null
  /** The run's cost with its downstream jobs', null when none was recorded. */
  cost_usd: number | null
  failed_at: string | null
  /** The phases whose issue is still open: no later attempt or run did its work, and no later run's same check passed. */
  open_issues: string[]
}

export interface MetaPoint {
  /** The scan id, and its time (epoch s). */
  scan: string
  t: number
  bytes: number
}

/** A primary-store scan's object count (`/api/series`'s root `objects`). */
export interface ObjPoint {
  scan: string
  t: number
  objects: number
}

export interface FleetMeta {
  /** The newest scan's bytes, and the newest at or before a day and a week before the document's `now`. */
  now: number | null
  d1: number | null
  d7: number | null
  /** The newest scan per UTC day, oldest first, over `META_DAYS`. */
  daily: MetaPoint[]
}

export interface FleetHealth {
  newest_scan: string | null
  /** Seconds. */
  newest_scan_age: number | null
  /** Scans with a coverage gap, summed over the columns. */
  gaps: number
}

export interface FleetDeployment {
  name: string
  url: string
  color: string
  health: FleetHealth | null
  /** The newest scan run (maintenance runs aside). */
  last: FleetRun | null
  /** The scan runs started in the card's window (`WINDOW_DAYS`; the newest always), at most `FLEET_RUNS`, newest first. */
  runs: FleetRun[]
  meta: FleetMeta | null
  /** The primary store's object count: the newest scan per UTC day over `META_DAYS`, oldest first. */
  objects: ObjPoint[] | null
  /** Why a part is missing: `health`, `runs`, `meta`, `objects`. */
  errors: Partial<Record<'health' | 'runs' | 'meta' | 'objects', string>>
}

export interface FleetDoc {
  /** Epoch s the document was read at. */
  now: number
  deployments: FleetDeployment[]
}

/** `/api/fleet`'s answer: the document, and the full card's signed URLs per theme (null with no `SESSION_SECRET`). */
export interface FleetResponse extends FleetDoc {
  card: { dark: string; light: string } | null
}
