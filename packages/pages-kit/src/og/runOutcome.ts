/** A scan run's outcome: the one rule the fleet's Slack post (its line and run strip), the fleet page's strip, charts and
 *  tables, and the deployment status card (`deployCard.ts`) share. A run still going is `running`; one stopped on purpose
 *  `cancelled` (its record's `status`, never a failure: it counts in no failure total and doesn't turn a line red); a
 *  failed one `failed`; one that finished with a step issue still open `issues`; anything else `ok`. No imports: the
 *  fleet page (`fleet/src/`) reads it too. */

export type Outcome = 'running' | 'cancelled' | 'failed' | 'issues' | 'ok'

export const runOutcome = (r: { status: string; open_issues: readonly string[] }): Outcome =>
  r.status === 'running' ? 'running' : r.status === 'cancelled' ? 'cancelled' : r.status === 'failed' ? 'failed' : r.open_issues.length ? 'issues' : 'ok'
