/** A scan run's outcome: the one rule the Slack post (its line and run strip) and the fleet page's strip, charts and tables
 *  share. A run still going is `running`; one stopped on purpose `cancelled` (its record's `status`, never a failure: it
 *  counts in no failure total and doesn't turn a line red); a failed one `failed`; one that finished with a step issue
 *  still open `issues`; anything else `ok`. No imports but the run's type: the page (`src/`) reads it too. */
import type { FleetRun } from './fleetTypes.js'

export type Outcome = 'running' | 'cancelled' | 'failed' | 'issues' | 'ok'

export const runOutcome = (r: Pick<FleetRun, 'status' | 'open_issues'>): Outcome =>
  r.status === 'running' ? 'running' : r.status === 'cancelled' ? 'cancelled' : r.status === 'failed' ? 'failed' : r.open_issues.length ? 'issues' : 'ok'
