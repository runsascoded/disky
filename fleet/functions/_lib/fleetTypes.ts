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
  /** The Slack permalink of the run's reply in `#<deployment>-admin`; absent or null when it has none (or the deployment's
   *  `/api/scan-runs` predates it). */
  thread_url?: string | null
  /** A manual run's starter's own Slack emoji (`:name:`), as the deployment's `/api/scan-runs?emoji=1` answers it (asked
   *  of OA's broker); absent when it has none, or for any other run. */
  trigger_emoji?: string
  /** A failed run's recorded error: one line, clipped, nothing Slack reads as markup (`fleet.ts` `oneLine`); absent when
   *  none was recorded, or for any other run. */
  error?: string
  /** The run's seconds per step family (`deployCard.ts` `familySeconds`, from the phases its site lists), summing to its
   *  duration; `''` is the time no step ran. Only on the newest `CARD_RUNS` runs (what the deployment card draws), and
   *  absent from a run whose site listed no phases. */
  families?: [string, number][]
  /** The families with a step that didn't finish clean, and how (beside `families`). */
  bad?: [string, StepState][]
}

/** How a step family's worst phase ended, when not clean. */
export type StepState = 'running' | 'cancelled' | 'failed'

export interface MetaPoint {
  /** The scan id, and its time (epoch s). */
  scan: string
  t: number
  bytes: number
}

/** A primary-store scan's object count and bytes (`/api/series`' root `objects`, `bytes`). */
export interface ObjPoint {
  scan: string
  t: number
  objects: number
  /** Null from a series that carried none. */
  bytes: number | null
}

export interface FleetMeta {
  /** The newest scan's bytes, and the newest at or before a day and a week before the document's `now`. */
  now: number | null
  d1: number | null
  d7: number | null
  /** The newest scan per UTC day, oldest first, over `HISTORY_DAYS`. */
  daily: MetaPoint[]
}

/** A UTC day (its 00:00, epoch s) and its scan runs' summed cost. */
export interface CostDay {
  t: number
  usd: number
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
  /** Its admin channel's Slack id (`FLEET_JSON`), when named. */
  channel?: string
  health: FleetHealth | null
  /** The newest scan run (maintenance runs aside). */
  last: FleetRun | null
  /** The scan runs started in the last `HISTORY_DAYS` (the newest always), at most `FLEET_RUNS`, newest first. */
  runs: FleetRun[]
  meta: FleetMeta | null
  /** The primary store's object count and bytes: the newest scan per UTC day over `HISTORY_DAYS`, oldest first. */
  objects: ObjPoint[] | null
  /** Each UTC day's summed cost of the scan runs that recorded one, oldest first, from the start of the previous UTC month
   *  (the month-over-month cost reply's data). From every run the deployment lists, so it reaches past `runs`'
   *  `HISTORY_DAYS`; absent when its runs couldn't be read. */
  cost_daily?: CostDay[]
  /** Why a part is missing: `health`, `runs`, `meta`, `objects`. */
  errors: Partial<Record<'health' | 'runs' | 'meta' | 'objects', string>>
}

export interface FleetDoc {
  /** Epoch s the document was read at. */
  now: number
  /** How many days back each deployment's runs and series reach (`HISTORY_DAYS`): the page's longest chart range. */
  days: number
  deployments: FleetDeployment[]
}

/** `/api/fleet`'s answer: the document, and the full card's signed URLs per theme (null with no `SESSION_SECRET`). */
export interface FleetResponse extends FleetDoc {
  card: { dark: string; light: string } | null
}
