// Scan runs (specs/done/scan-runs-ui.md): the D1 rows `dt-cloud scan-run record |
// backfill` writes, and the pure derivations `/scans` shows — each output's
// delta against the previous scan, a run's summary row, phase lanes, links.
// DOM-free: the Functions (`_lib/scanRuns.ts`) and the pages share it.
import { exactSlug, isScanId, scanTime } from './scanSlug'
import STEP_FAILURES from './scanSteps.json'

export type ScanRunStatus = 'running' | 'succeeded' | 'failed' | 'nop'

export interface ScanRun {
  run_id: string
  store: string
  scan: string
  kind: string
  parent: string | null
  status: ScanRunStatus
  failed_phase: string | null
  error: string | null
  started_ts: number | null
  finished_ts: number | null
  job_name: string | null
  region: string | null
  machine: string | null
  spot: number | null
  tasks: number | null
  image: string | null
  cost_usd: number | null
  gen: string | null
  source: 'job' | 'backfill'
  updated_ts: number
  /** What started it: the deployment's cron, or a hand submit / resume. Absent before migration `scan-run-trigger`;
   * null for runs recorded before it, and backfills. */
  trigger?: 'schedule' | 'manual' | null
}

export interface ScanRunPhase {
  run_id: string
  phase: string
  seq: number
  started_ts: number | null
  finished_ts: number | null
  status: 'running' | 'done' | 'failed'
  note: string | null
  /** The Batch job the phase ran in when not the run's own (a Nextflow step's task), and its machine */
  job_name?: string | null
  machine?: string | null
  /** The pipeline node that ran it: a Nextflow task's `<process> (<tag>)` */
  task?: string | null
}

export interface ScanRunOutput {
  run_id: string
  key: string
  uri: string
  bytes: number | null
  objects: number | null
  rows: number | null
  gen: string | null
}

/** An output beside the previous scan's same structure. */
export interface OutputDelta extends ScanRunOutput {
  prev_scan: string | null
  prev_bytes: number | null
  prev_rows: number | null
  delta_bytes: number | null
  delta_rows: number | null
}

/** The maintenance kinds (`dt_cloud.maint_runs`): a store's compaction and its merges, each its own Batch job and its own
 * row on /scans. Their `scan` is the newest scan they fold or carry (so they sort beside it) and their `gen` is
 * `<store>/<gen>` (a compaction's finish: `<store>/<gen> → <new gen>`); they are never a scan's run (`scanRunOf`, the
 * paging, /health's running scans skip them). */
/** Maintenance runs' kinds: a compaction, a race of one (`compact-probe -U`: built beside it, published only by a
 * compaction adopting it), a merge of deferred carries. */
export const MAINT_KINDS = ['compact', 'race', 'merge'] as const
export const isMaintenance = (kind: string): boolean => (MAINT_KINDS as readonly string[]).includes(kind)

/** Independent pollers are runs, but never published snapshots or scan-freshness evidence. */
export const isScanProducer = (kind: string): boolean => !isMaintenance(kind) && kind !== 'access-ingest'

/** A maintenance run's store (`interval-store`, `static-names`): its `gen`'s first segment; null for a scan's run. */
export const maintStore = (run: Pick<ScanRun, 'kind' | 'gen'>): string | null =>
  isMaintenance(run.kind) && run.gen?.includes('/') ? run.gen.split('/')[0] : null

/** Seconds without a write after which a running run reads as quiet: five missed heartbeats. A running run's record
 * is touched every 60 s (its job's `scan-run supervise -H`; each running phase's process, `$DT_HEARTBEAT_S`, with the
 * phase's live note; a maintenance job's recorder), however long its phase, so a quiet run's job has likely stalled or
 * died. */
export const QUIET_S = 300

/** Seconds without a write after which a running run reads as lost: thirty missed heartbeats, past any D1 or network
 * hiccup, so its job most likely died without its exit trap (Batch's maxRunDuration, a lost VM), and `scan-run
 * reconcile` (`RECONCILE_AFTER_S`, the same bound) marks it failed once Batch confirms. Until then its clock stops at
 * its last write (`liveUntil`), so it never stretches a duration chart. */
export const LOST_S = 1800

export const isLost = (run: Pick<ScanRun, 'status' | 'updated_ts'>, now: number): boolean =>
  run.status === 'running' && now - run.updated_ts > LOST_S

/** Where a run's clock runs to while its row says running: `now`, or its last write (`updated_ts`) once lost. */
export const liveUntil = (run: Pick<ScanRun, 'status' | 'updated_ts'>, now: number): number =>
  isLost(run, now) ? run.updated_ts : now

/** What a running run is doing: its running phase (the newest-started, if several), that phase's elapsed seconds and
 * its note (its heartbeat's live progress line; null when none runs), how long since the run last wrote (`updated_ts`:
 * its heartbeat) when that exceeds `QUIET_S`, and whether that exceeds `LOST_S`. Null for a run that isn't running. */
export function liveness(run: Pick<ScanRun, 'status' | 'updated_ts'>, spans: readonly PhaseSpan[], now: number): { phase: string | null; elapsed: number | null; note: string | null; quiet: number | null; lost: boolean } | null {
  if (run.status !== 'running') return null
  const cur = [...spans].filter(s => s.status === 'running').sort((a, b) => b.start - a.start)[0]
  const quiet = now - run.updated_ts
  return { phase: cur?.phase ?? null, elapsed: cur ? cur.end - cur.start : null, note: cur?.note ?? null, quiet: quiet > QUIET_S ? quiet : null, lost: isLost(run, now) }
}

/** A key's structure: the key with its own scan id abstracted, so a per-scan
 * key (`static/deltas/2026-10-08`) compares with the previous scan's. */
export const structureOf = (key: string, scan: string): string => key.split(scan).join('{scan}')

/** A key's top level (`listing/b1` → `listing`). */
export const topOf = (key: string): string => key.split('/')[0]

/** When a run started (epoch s), for ordering runs: its `started_ts`, else its scan id's own time (a date-only id is
 *  00:00 UTC), else its `updated_ts`. `started_ts` can be NULL on rows the job recorded before it knew its start (the
 *  in-job runs before `6842ed63`), and those must not sort below every older run. */
export function runTime(r: Pick<ScanRun, 'started_ts' | 'scan' | 'updated_ts'>): number {
  if (r.started_ts != null) return r.started_ts
  const t = scanTime(r.scan)
  return Number.isNaN(t) ? r.updated_ts : Math.floor(t / 1000)
}

/** Oldest first by `runTime`, then by run id. */
export const byRunTime = (a: ScanRun, b: ScanRun): number => runTime(a) - runTime(b) || a.run_id.localeCompare(b.run_id)

/** A run's elapsed seconds (while it runs, to `liveUntil`: a lost run's last write); null without a start. */
export function runSecs(r: Pick<ScanRun, 'started_ts' | 'finished_ts' | 'status' | 'updated_ts'>, now: number): number | null {
  if (r.started_ts == null) return null
  const end = r.finished_ts ?? (r.status === 'running' ? liveUntil(r, now) : null)
  return end == null ? null : Math.max(0, end - r.started_ts)
}

/** Per scan, per structure: the output of the scan's latest-finished
 * succeeded (or still running) run that has it — what the scan's structure is. */
export function outputIndex(runs: readonly ScanRun[], outputs: readonly ScanRunOutput[]): Map<string, Map<string, ScanRunOutput>> {
  const byId = new Map(runs.map(r => [r.run_id, r]))
  const best = new Map<string, Map<string, { o: ScanRunOutput; t: number }>>()
  for (const o of outputs) {
    const r = byId.get(o.run_id)
    if (!r || (r.status !== 'succeeded' && r.status !== 'running')) continue
    const t = r.finished_ts ?? r.started_ts ?? 0
    const m = best.get(r.scan) ?? new Map()
    best.set(r.scan, m)
    const s = structureOf(o.key, r.scan)
    const cur = m.get(s)
    if (!cur || t > cur.t) m.set(s, { o, t })
  }
  return new Map([...best].map(([scan, m]) => [scan, new Map([...m].map(([s, v]) => [s, v.o]))]))
}

/** `run`'s outputs, each beside the newest earlier scan holding the same
 * structure (null deltas when none does, or a side has no number). */
export function withDeltas(run: ScanRun, outputs: readonly ScanRunOutput[], index: Map<string, Map<string, ScanRunOutput>>): OutputDelta[] {
  const earlier = [...index.keys()].filter(s => s < run.scan).sort().reverse()
  return outputs.filter(o => o.run_id === run.run_id).map(o => {
    const s = structureOf(o.key, run.scan)
    const prevScan = earlier.find(sc => index.get(sc)!.has(s)) ?? null
    const p = prevScan ? index.get(prevScan)!.get(s)! : null
    const d = (a: number | null, b: number | null | undefined) => (a != null && b != null ? a - b : null)
    return {
      ...o, prev_scan: prevScan, prev_bytes: p?.bytes ?? null, prev_rows: p?.rows ?? null,
      delta_bytes: d(o.bytes, p?.bytes), delta_rows: d(o.rows, p?.rows),
    }
  })
}

/** A phase on the run's clock: seconds from the run's start. */
export interface PhaseSpan { phase: string; start: number; end: number; status: ScanRunPhase['status']; note: string | null; lane: number; machine?: string | null; task?: string | null }

/** Processing starts after a completed queue phase. A retry may wait days after capture before its job starts. */
export function processingSecs(total: number | null, spans: readonly PhaseSpan[]): number | null {
  const queued = spans.filter(s => s.phase === 'queue' && s.status === 'done')
  return total == null || !queued.length ? null : Math.max(0, total - Math.max(...queued.map(s => s.end)))
}

/** A phase's status, a closed one's note naming a nonfatal failure (`failed (exit N)`, legacy `wall, failed …`) over
 * its recorded `done`. A running phase stays running: its note is a heartbeat's live output line, whatever it says. */
export function phaseStatus(p: Pick<ScanRunPhase, 'status' | 'note'>): ScanRunPhase['status'] {
  return p.status !== 'running' && /(?:^|,\s*)failed\b/i.test(p.note ?? '') ? 'failed' : p.status
}

export function phaseOutcome(p: Pick<ScanRunPhase, 'status' | 'note'>): string {
  return phaseStatus(p) === 'failed' && /\bexit\s+124\b|\btimed?\s*out\b/i.test(p.note ?? '') ? 'timed out' : phaseStatus(p)
}

export const phaseFill = (p: Pick<PhaseSpan, 'phase' | 'status' | 'note'>, color: (phase: string) => string): string =>
  phaseStatus(p) === 'failed' ? 'var(--danger)' : color(p.phase)

/** Compact charts show the main lane, plus a failure stripe for otherwise hidden overlapping attempts. */
export const overlappingFailures = (spans: readonly PhaseSpan[]): PhaseSpan[] =>
  spans.filter(s => s.lane !== 0 && phaseStatus(s) === 'failed')

/** A closed phase's explicit skip: its note's `skipped[ (kind)]: reason` clause (at the start or after a comma, e.g.
 * `overlapped, skipped (busy): …`; `dt_cloud.scan_runs.phase_skip`), else null. The phase is done, its work is not.
 * A verdict's `2 skipped (…)` is no skip clause, nor is a running phase's heartbeat line. */
export function phaseSkip(p: Pick<ScanRunPhase, 'status' | 'note'>): string | null {
  if (p.status === 'running') return null
  return /(?:^|,\s*)(skipped(?: \([^)]*\))?: .+)$/.exec(p.note ?? '')?.[1] ?? null
}

export interface PhaseRecovery { run: ScanRun; phases: ScanRunPhase[]; secs: number | null }
/** A phase that left the run short: failed (`outcome` its `phaseOutcome`; `attempts` when more than one failed,
 * `detail` the last one's verdict, its note without the exit clause), skipped (`outcome` `skipped`, `reason` its note's
 * skip clause), or done with its search behind (`outcome` `search behind`, `reason` its note's `search behind:` clause:
 * the data is published, only the static name index lags it). `recovery`: the run that did its work since: a linked
 * attempt of this run's own (a `phase-recovery` child), else, for a step a later run converges, that run. `passing`,
 * on a check's issue only (`StepFailure.checks`): the first later run whose same check passed (null: none has, or
 * `recovery` re-checked this scan). That run checked the site as it then stood, not this scan again, so it is no
 * recovery. */
export interface PhaseIssue { phase: string; outcome: string; recovery: ScanRun | null; passing?: ScanRun | null; reason?: string; attempts?: number; detail?: string }

/** A soft step's failure as its step declares it (`dt_cloud.scan_steps.Step`): what it `leaves` short, whether it is a
 * `side` step (the scan's data and serving are whole without it), whether a later run's same step `converges` it
 * (does this run's work too), and whether it `checks` (a check, not work: nothing redoes it, a later run's passes or
 * doesn't). */
export interface StepFailure { leaves: string; side: boolean; converges: boolean; checks: boolean }

/** The failure fields of the step a phase is named for; undefined for a phase no soft step names (a core stage's
 * nonfatal command), which reads as leaving the scan short. The table is the registry's own: `scanSteps.json` is
 * `dt-cloud scan steps`' output, and `cloud/tests/test_scan_steps.py` fails when they differ. */
export const stepFailure = (phase: string): StepFailure | undefined =>
  (Object.hasOwn(STEP_FAILURES, phase) ? (STEP_FAILURES as Record<string, StepFailure>)[phase] : undefined)

/** Where an issue stands: `recovered` (a later attempt or run did its work), `passing` (a check failed, and the same
 * check has passed in a later run: the site passes since, which doesn't show this scan did), `behind` (only search lags
 * the scan), `side` (a side step failed and stays failed: the scan's data and serving are whole), or `incomplete` (the
 * scan's data or serving is short; a failed check no later run has passed). */
export type IssueState = 'recovered' | 'passing' | 'behind' | 'side' | 'incomplete'
export function issueState(i: Pick<PhaseIssue, 'phase' | 'outcome' | 'recovery' | 'passing'>): IssueState {
  return i.recovery ? 'recovered' : i.passing ? 'passing' : i.outcome === BEHIND ? 'behind' : stepFailure(i.phase)?.side ? 'side' : 'incomplete'
}

/** What happened to an issue's phase, in a few words: `failed`, `failed 2x`, `timed out`, `skipped: <reason>` (the
 * recorded reason, without its `skipped (kind):` head), `search behind`. */
export function issueWhat(i: Pick<PhaseIssue, 'outcome' | 'reason' | 'attempts'>): string {
  if (i.outcome === BEHIND) return BEHIND
  if (i.reason) return `skipped: ${i.reason.replace(/^skipped(?: \([^)]*\))?: /, '')}`
  return (i.attempts ?? 1) > 1 ? `${i.outcome} ${i.attempts}x` : i.outcome
}

/** What an open issue leaves short, as its step declares it; undefined once its work is done or its check has passed
 * since, for search behind (its reason says it) and for a phase no soft step names. */
export function issueLeaves(i: Pick<PhaseIssue, 'phase' | 'outcome' | 'recovery' | 'passing'>): string | undefined {
  return i.recovery || i.passing || i.outcome === BEHIND ? undefined : stepFailure(i.phase)?.leaves
}

/** Where an open issue goes from here: search catches up with the index; a converging step's work comes with a later
 * run's same step, unless its store's append passed the scan over (`PASSED_OVER`); a check is not redone, and reads
 * `passing since` once a later run's passes; any other is not retried. */
export function issueNext(i: Pick<PhaseIssue, 'phase' | 'outcome' | 'detail'>): string {
  if (i.outcome === BEHIND) return 'Search catches up when the static name index appends this scan.'
  if (PASSED_OVER.test(i.detail ?? '')) return 'Not retried: the store appends in scan order and is past this scan. A rebuild or backfill of the store adds it.'
  const step = stepFailure(i.phase)
  if (step?.converges) return `A later run's ${i.phase} step does this run's work too; none has yet.`
  return step?.checks ? `A check: no later run redoes it for this scan, and no later run's ${i.phase} has passed yet.` : 'Not retried: no later run redoes it.'
}

/** A failed append's `passed over: …` clause (`dt_cloud.scan_steps.PASSED_OVER`: the store's append refused the scan,
 * a newer one being in already), at the start of a note's verdict or after a comma. */
const PASSED_OVER = /(?:^|,\s*)passed over\b/

/** Whether a store's append passed the phase's scan over: no later run's append takes it, so it never converges. */
export const phasePassedOver = (p: Pick<ScanRunPhase, 'status' | 'note'>): boolean => phaseStatus(p) === 'failed' && PASSED_OVER.test(p.note ?? '')

/** A done phase's `search behind: …` clause (`dt_cloud.scan_steps.probe`: every failure was the search's
 * `scan-not-indexed` refusal), else null. */
export function phaseBehind(p: Pick<ScanRunPhase, 'status' | 'note'>): string | null {
  return p.status === 'done' ? /^search behind: (.+)$/.exec(p.note ?? '')?.[1] ?? null : null
}

export const BEHIND = 'search behind'
/** A run's outcome when its worst issue is a failed check that has passed since (`issueState`'s `passing`). */
export const PASSING = 'passing since'

/** What a run came to. A succeeded run with issues reads as its worst open one (`issueState`): `incomplete` (the scan's
 * data or serving is short, or a check failed and hasn't passed since: the one to look at), else `search behind`, else
 * `passing since` (a check failed; the same check has passed in a later run), else `partial` (only side steps failed),
 * else `recovered` (every issue's work was done since). Otherwise its status (`nop` as `no-op`). */
export function runOutcome(run: Pick<ScanRun, 'status'>, issues: readonly PhaseIssue[] = []): string {
  if (run.status === 'succeeded' && issues.length) {
    const states = new Set(issues.map(issueState))
    return states.has('incomplete') ? 'incomplete' : states.has('behind') ? BEHIND : states.has('passing') ? PASSING : states.has('side') ? 'partial' : 'recovered'
  }
  return run.status === 'nop' ? 'no-op' : run.status
}

/** The steps behind an outcome: an `incomplete` run's steps that left the scan short, a `passing since` run's failed
 * checks, a `partial` run's failed side steps, a `recovered` run's recovered ones; none for any other outcome. */
export function outcomeSteps(issues: readonly PhaseIssue[], outcome: string): string[] {
  const state: IssueState | null = outcome === 'incomplete' ? 'incomplete' : outcome === PASSING ? 'passing' : outcome === 'partial' ? 'side' : outcome === 'recovered' ? 'recovered' : null
  return state ? issues.filter(i => issueState(i) === state).map(i => i.phase) : []
}

/** The run a `passing since` run's failed checks have all passed by: the latest of their first passing runs. */
export function passingSince(issues: readonly PhaseIssue[]): ScanRun | null {
  return issues.filter(i => issueState(i) === 'passing').map(i => i.passing!).sort(byRunTime).at(-1) ?? null
}

/** A run's status in two pieces, the chip's heavy and light ink: `runOutcome` and its steps (`partial` / `warm-cache`,
 * `recovered` / `static-names, digest`; past two steps, the first and a count: `warm-cache +2`; no steps, no tail). A
 * `passing since` run reads as what failed and since when: `probe failed` / `passing since <scan>` (`passingSince`'s;
 * `search behind` when its probe only read that). */
export function outcomeParts(run: Pick<ScanRun, 'status'>, issues: readonly PhaseIssue[] = []): { head: string; tail: string | null } {
  const outcome = runOutcome(run, issues), steps = outcomeSteps(issues, outcome)
  const list = steps.length > 2 ? `${steps[0]} +${steps.length - 1}` : steps.join(', ')
  if (outcome !== PASSING) return { head: outcome, tail: steps.length ? list : null }
  const failed = issues.some(i => issueState(i) === 'passing' && i.outcome !== BEHIND)
  return { head: failed ? `${list} failed` : BEHIND, tail: `${PASSING} ${passingSince(issues)!.scan}` }
}

/** `outcomeParts` as one line: `partial · warm-cache`, `recovered · static-names, digest`, `partial · warm-cache +2`,
 * `probe failed · passing since 2026-10-25T0702`; `succeeded`. */
export function runOutcomeLabel(run: Pick<ScanRun, 'status'>, issues: readonly PhaseIssue[] = []): string {
  const { head, tail } = outcomeParts(run, issues)
  return tail ? `${head} · ${tail}` : head
}

/** Why a run is short, one line per open issue (not recovered, nor a check passing since): `<phase> <skip clause>`
 * for a skip (its recorded reason), `<phase> <outcome>` for a failure. */
export function partialReasons(issues: readonly PhaseIssue[]): string[] {
  return issues.filter(i => i.recovery == null && !i.passing).map(i => `${i.phase} ${i.reason ?? i.outcome}`)
}

/** `runOutcome`, a short one with its reasons: `incomplete: static-names skipped (busy): …`, `partial: warm-cache failed`;
 * a `passing since` one as its label (`probe failed · passing since <scan>`). */
export function runOutcomeLine(run: Pick<ScanRun, 'status'>, issues: readonly PhaseIssue[] = []): string {
  const outcome = runOutcome(run, issues)
  if (outcome === PASSING) return runOutcomeLabel(run, issues)
  return outcome === 'partial' || outcome === 'incomplete' || outcome === BEHIND ? `${outcome}: ${partialReasons(issues).join('; ')}` : outcome
}

/** /scans' status filter (`?st=<key>`): each choice's label and the outcomes it keeps. `attention` is the runs to look
 * at (failed, or published short), apart from a `partial` run, whose scan is whole, and a `passing since` one, whose
 * failed check has passed in a later run. */
export const STATUS_FILTERS: readonly { key: string; label: string; outcomes: readonly string[] }[] = [
  { key: 'attention', label: 'needs attention (failed, incomplete)', outcomes: ['failed', 'incomplete'] },
  { key: 'succeeded', label: 'succeeded', outcomes: ['succeeded'] },
  { key: 'partial', label: 'partial (a side step failed)', outcomes: ['partial'] },
  { key: 'passing', label: 'passing since (a check failed, and has passed since)', outcomes: [PASSING] },
  { key: 'incomplete', label: 'incomplete', outcomes: ['incomplete'] },
  { key: 'behind', label: BEHIND, outcomes: [BEHIND] },
  { key: 'recovered', label: 'recovered', outcomes: ['recovered'] },
  { key: 'failed', label: 'failed', outcomes: ['failed'] },
  { key: 'running', label: 'running', outcomes: ['running'] },
  { key: 'nop', label: 'no-op', outcomes: ['no-op'] },
]

/** Whether a run of `outcome` passes the status filter `key` (`''`: every run; an unknown key: none). */
export const statusMatches = (key: string, outcome: string): boolean =>
  !key || !!STATUS_FILTERS.find(f => f.key === key)?.outcomes.includes(outcome)

export function phaseRecoveries(run: ScanRun, runs: readonly ScanRun[], phases: readonly ScanRunPhase[], now: number): PhaseRecovery[] {
  return runs.filter(r => r.parent === run.run_id && r.kind === 'phase-recovery' && r.scan === run.scan && r.store === run.store)
    .sort(byRunTime).map(r => ({ run: r, phases: phases.filter(p => p.run_id === r.run_id), secs: runSecs(r, now) }))
}

/** A failed phase's verdict: its note without the `failed (exit N)` clause (`4 passed, 0 skipped, 2 failed`), else
 * undefined. */
function failureDetail(note: string | null): string | undefined {
  const rest = (note ?? '').replace(/^(?:[^,]*,\s*)?failed(?: \([^)]*\))?(?:,\s*|$)/, '').trim()
  return rest || undefined
}

/** A phase that did its work: closed, not failed, skipped or behind. */
const phaseDid = (p: ScanRunPhase): boolean =>
  p.finished_ts != null && phaseStatus(p) === 'done' && phaseSkip(p) == null && phaseBehind(p) == null

/** A check that passed in full: it did its work, and its verdict (`n passed, n skipped (reason), n failed`: the
 * probe's) skipped nothing. A pass that skipped requests (the search's, beside a skipped static-names) is no pass of
 * the check that failed on them. */
const checkPassed = (p: ScanRunPhase): boolean => phaseDid(p) && !/(?:^|,\s*)[1-9]\d* skipped\b/.test(p.note ?? '')

/** The first other run of `run`'s kind and store, for its scan or a later one, whose phase `p.phase` started after `p`
 * ended and `did` its work (`phaseDid`; a check: `checkPassed`); null when none has. */
function laterDone(run: ScanRun, p: ScanRunPhase, runs: readonly ScanRun[], phases: readonly ScanRunPhase[], did: (a: ScanRunPhase) => boolean = phaseDid): ScanRun | null {
  const end = p.finished_ts
  if (end == null) return null
  const peers = new Map(runs.filter(r => r.run_id !== run.run_id && !r.parent && r.kind === run.kind && r.store === run.store && r.scan >= run.scan).map(r => [r.run_id, r]))
  const first = phases.filter(a => a.phase === p.phase && peers.has(a.run_id) && a.started_ts != null && a.started_ts >= end && did(a))
    .sort((a, b) => a.finished_ts! - b.finished_ts!)[0]
  return first ? peers.get(first.run_id)! : null
}

/** The run that later did a converging step's work for `run` (`StepFailure.converges`: an append of every pending
 * scan, an ingest from the watermark, a thread converged, a pass over every scan): `laterDone`'s. Null for a step no
 * later run redoes, and for a scan its store's append passed over (`phasePassedOver`). */
const convergedBy = (run: ScanRun, p: ScanRunPhase, runs: readonly ScanRun[], phases: readonly ScanRunPhase[]): ScanRun | null =>
  (stepFailure(p.phase)?.converges && !phasePassedOver(p) ? laterDone(run, p, runs, phases) : null)

/** A check's `PhaseIssue.passing` (`StepFailure.checks`): the first later run whose same check passed in full
 * (`laterDone` by `checkPassed`), null once `recovery` re-checked this scan; nothing for a step that isn't a check. */
const passingBy = (run: ScanRun, p: ScanRunPhase, runs: readonly ScanRun[], phases: readonly ScanRunPhase[], recovery: ScanRun | null): { passing?: ScanRun | null } =>
  (stepFailure(p.phase)?.checks ? { passing: recovery ? null : laterDone(run, p, runs, phases, checkPassed) } : {})

/** The run's issues, one per phase (its last attempt's; `attempts` counts a phase's failed ones). */
export function phaseIssues(run: ScanRun, runs: readonly ScanRun[], phases: readonly ScanRunPhase[]): PhaseIssue[] {
  const own = phases.filter(p => p.run_id === run.run_id && (phaseStatus(p) === 'failed' || phaseSkip(p) != null || phaseBehind(p) != null))
  const last = new Map<string, ScanRunPhase>()
  for (const p of own) last.set(p.phase, p)
  return [...last.values()].map(p => {
    const retries = phaseRecoveries(run, runs, phases, 0).filter(r => r.run.status === 'succeeded' && r.phases.some(a =>
      a.phase === p.phase && phaseDid(a) && p.finished_ts != null && a.finished_ts! > p.finished_ts))
    const recovery = retries.at(-1)?.run ?? convergedBy(run, p, runs, phases)
    const passing = passingBy(run, p, runs, phases, recovery)
    if (phaseStatus(p) !== 'failed') {
      const skip = phaseSkip(p)
      return skip != null ? { phase: p.phase, outcome: 'skipped', recovery, ...passing, reason: skip } : { phase: p.phase, outcome: BEHIND, recovery, ...passing, reason: phaseBehind(p)! }
    }
    const attempts = own.filter(a => a.phase === p.phase && phaseStatus(a) === 'failed').length
    const detail = failureDetail(p.note)
    return { phase: p.phase, outcome: phaseOutcome(p), recovery, ...passing, ...(attempts > 1 ? { attempts } : {}), ...(detail ? { detail } : {}) }
  })
}

/** A run's phases as spans, in order, each on a lane: a phase overlapping the
 * one before it (an overlapped phase, `access-ingest` beside the listing)
 * takes the next free lane. */
/** A run's step tasks (its phases' own Batch jobs, a Nextflow run's): one per job, in start order. */
export interface RunTask { job_name: string; machine: string | null; phases: string[]; start: number | null; end: number | null; status: ScanRunPhase['status'] }

export function runTasks(phases: readonly ScanRunPhase[]): RunTask[] {
  const by = new Map<string, RunTask>()
  for (const p of [...phases].sort((a, b) => (a.started_ts ?? Infinity) - (b.started_ts ?? Infinity) || a.seq - b.seq)) {
    if (!p.job_name) continue
    const t = by.get(p.job_name) ?? { job_name: p.job_name, machine: null, phases: [], start: null, end: null, status: 'done' as const }
    t.machine ??= p.machine ?? null
    t.phases.push(p.phase)
    if (p.started_ts != null) t.start = t.start == null ? p.started_ts : Math.min(t.start, p.started_ts)
    if (p.finished_ts != null) t.end = t.end == null ? p.finished_ts : Math.max(t.end, p.finished_ts)
    const st = phaseStatus(p)
    if (st === 'failed' || (st === 'running' && t.status === 'done')) t.status = st
    by.set(p.job_name, t)
  }
  return [...by.values()]
}

export function phaseSpans(run: Pick<ScanRun, 'started_ts'> & Partial<Pick<ScanRun, 'status'>>, phases: readonly ScanRunPhase[], now?: number): PhaseSpan[] {
  const t0 = run.started_ts
  if (t0 == null) return []
  const laneEnds: number[] = []
  return [...phases].sort((a, b) => a.seq - b.seq).flatMap(p => {
    const until = p.finished_ts ?? (p.status === 'running' && run.status === 'running' ? now : undefined)
    if (p.started_ts == null || until == null) return []
    const start = Math.max(0, p.started_ts - t0), end = Math.max(start, until - t0)
    let lane = laneEnds.findIndex(e => e <= start)
    if (lane < 0) { lane = laneEnds.length; laneEnds.push(end) } else laneEnds[lane] = end
    return [{ phase: p.phase, start, end, status: phaseStatus(p), note: p.note, lane, ...(p.machine ? { machine: p.machine } : {}), ...(p.task ? { task: p.task } : {}) }]
  })
}

/** Where a failed run failed: its recorded phase, else after its last phase. */
export function failedAt(run: ScanRun, phases: readonly ScanRunPhase[]): string | null {
  if (run.status !== 'failed') return null
  if (run.failed_phase) return run.failed_phase
  const last = [...phases].filter(p => p.run_id === run.run_id).sort((a, b) => b.seq - a.seq)[0]
  return last ? `after ${last.phase}` : null
}

/** One `/scans` row: an orchestrating run with its downstream jobs folded in. */
export interface RunSummary {
  run: ScanRun
  secs: number | null
  spans: PhaseSpan[]
  /** The run's top-level outputs' bytes, and their sum of deltas vs the previous scan. */
  out_bytes: number | null
  delta_bytes: number | null
  /** The downstream jobs (listing fan-out, …) and their cost. */
  downstream: number
  cost_usd: number | null
  failed_at: string | null
  phase_issues: PhaseIssue[]
}

/** The run behind `scan` among `rows` (`summarize`'s, newest first): its newest succeeded run (a failed attempt's resume),
 *  else its newest; undefined when no run recorded it. */
/** The scans a hand submit started: a scan's trigger is its first run's (a manual resume of a scheduled scan stays
 *  scheduled), and a scan with no recorded trigger is neither. */
export function manualScans(rows: readonly RunSummary[]): Set<string> {
  const first = new Map<string, ScanRun>()
  for (const { run } of rows) {
    if (!isScanProducer(run.kind)) continue
    const f = first.get(run.scan)
    if (!f || (run.started_ts ?? Infinity) < (f.started_ts ?? Infinity)) first.set(run.scan, run)
  }
  return new Set([...first].filter(([, r]) => r.trigger === 'manual').map(([scan]) => scan))
}

/** How long a scan of `rows`' store takes, for "Run a scan"'s confirm: the median of its `n` newest succeeded scan runs
 *  (kind `scan`, its own runs, not a resume's sibling), in seconds; null when none has succeeded. */
export function typicalScanSecs(rows: readonly RunSummary[], n = 7): number | null {
  const secs = rows.filter(r => r.run.kind === 'scan' && !r.run.parent && r.run.status === 'succeeded' && r.secs != null)
    .sort((a, b) => byRunTime(b.run, a.run)).slice(0, n).map(r => r.secs!).sort((a, b) => a - b)
  if (!secs.length) return null
  const mid = secs.length >> 1
  return secs.length % 2 ? secs[mid] : (secs[mid - 1] + secs[mid]) / 2
}

/** "about N hours" (an hour or more), else "about N minutes": a duration read as an expectation, not a measurement. */
export function aboutDur(secs: number): string {
  const h = secs / 3600
  if (h >= 1) { const r = Math.round(h * 2) / 2; return `about ${r} hour${r === 1 ? '' : 's'}` }
  const m = Math.max(1, Math.round(secs / 60))
  return `about ${m} minute${m === 1 ? '' : 's'}`
}

export function scanRunOf(rows: readonly RunSummary[], scan: string): ScanRun | undefined {
  const runs = rows.map(r => r.run).filter(r => r.scan === scan && isScanProducer(r.kind))
  return runs.find(r => r.status === 'succeeded') ?? runs[0]
}

export function summarize(runs: readonly ScanRun[], phases: readonly ScanRunPhase[], outputs: readonly ScanRunOutput[], now: number): RunSummary[] {
  const index = outputIndex(runs, outputs)
  const kids = new Map<string, ScanRun[]>()
  for (const r of runs) if (r.parent) kids.set(r.parent, [...(kids.get(r.parent) ?? []), r])
  const byRun = new Map<string, ScanRunPhase[]>()
  for (const p of phases) byRun.set(p.run_id, [...(byRun.get(p.run_id) ?? []), p])
  return runs.filter(r => !r.parent || !runs.some(p => p.run_id === r.parent))
    .sort((a, b) => byRunTime(b, a))
    .map(run => {
      const tops = withDeltas(run, outputs, index).filter(o => !o.key.includes('/'))
      const sum = (xs: (number | null)[]) => (xs.some(x => x != null) ? xs.reduce<number>((a, x) => a + (x ?? 0), 0) : null)
      const ks = kids.get(run.run_id) ?? []
      const costs = [run.cost_usd, ...ks.map(k => k.cost_usd)]
      return {
        run, secs: runSecs(run, now), spans: phaseSpans(run, byRun.get(run.run_id) ?? [], liveUntil(run, now)),
        out_bytes: sum(tops.map(o => o.bytes)), delta_bytes: sum(tops.map(o => o.delta_bytes)),
        downstream: ks.length, cost_usd: sum(costs), failed_at: failedAt(run, byRun.get(run.run_id) ?? []),
        phase_issues: phaseIssues(run, runs, phases),
      }
    })
}

/** Phase names in their usual order (by mean seq), for a stable color per phase. */
export function phaseOrder(phases: readonly Pick<ScanRunPhase, 'phase' | 'seq'>[]): string[] {
  const acc = new Map<string, { n: number; s: number }>()
  for (const p of phases) { const a = acc.get(p.phase) ?? { n: 0, s: 0 }; a.n++; a.s += p.seq; acc.set(p.phase, a) }
  return [...acc].sort((a, b) => a[1].s / a[1].n - b[1].s / b[1].n || a[0].localeCompare(b[0])).map(([k]) => k)
}

/** The `/meta` tree's page for an output (`META_TREE_URL` = `<origin>/meta`):
 * a prefix opens its directory, an object (or a split part, `…/path-index`)
 * its parent directory. Null without a base or for a non-store URI. */
export function metaHref(base: string | null | undefined, uri: string): string | null {
  const m = base ? /^[a-z0-9]+:\/\/([^/]+)\/(.*)$/.exec(uri) : null
  if (!m) return null
  const key = m[2]
  const dir = key.endsWith('/') ? key.slice(0, -1) : key.split('/').slice(0, -1).join('/')
  return `${base!.replace(/\/$/, '')}/${m[1]}${dir ? `/${dir}` : ''}`
}

/** The map at a scan (`/?d=<slug>`): `slug` its canonical slug (`useMinSlug`),
 * else its minute (`exactSlug`; never the day slug, the day's latest scan). */
export const mapHref = (scan: string, storePath = '/', slug: (id: string) => string = exactSlug): string | null =>
  isScanId(scan) ? `${storePath.replace(/\/$/, '')}/?d=${slug(scan)}` : null

export const batchHref = (project: string, region: string, job: string, tab: 'details' | 'logs' = 'details'): string =>
  `https://console.cloud.google.com/batch/jobsDetail/regions/${region}/jobs/${job}/${tab}?project=${encodeURIComponent(project)}`

/** Logs Explorer's `;query=` is parsed as URL params: `(`/`)` must be escaped
 * too (`encodeURIComponent` leaves them), or the query is cut at the first `(`. */
export const logsQueryParam = (query: string): string => encodeURIComponent(query).replace(/\(/g, '%28').replace(/\)/g, '%29')

/** A step task's logs: Logs Explorer over its Batch job by name (`labels.task_group_name` = `…/jobs/<name>/taskGroups/…`).
 * Not the job's Logs tab: Nextflow deletes a task's job when it finishes, and its logs outlive it. */
export function taskLogsHref(project: string, job: string, since: number | null): string {
  const t = since != null ? new Date((since - 600) * 1000).toISOString() : null
  const query = `log_id("batch_task_logs")\nlabels.task_group_name:"/jobs/${job}/"${t ? `\ntimestamp >= "${t}"` : ''}`
  return `https://console.cloud.google.com/logs/query;query=${logsQueryParam(query)}?project=${encodeURIComponent(project)}`
}

/** The run's logs: its Batch job's Logs tab (task + agent logs, no query to
 * get wrong), else Logs Explorer over its task logs (`labels.job_uid` is the
 * run id). */
export function logsHref(project: string, run: Pick<ScanRun, 'run_id' | 'started_ts' | 'job_name' | 'region'>): string {
  if (run.job_name && run.region) return batchHref(project, run.region, run.job_name, 'logs')
  const since = run.started_ts != null ? new Date((run.started_ts - 600) * 1000).toISOString() : null
  const query = `labels.job_uid="${run.run_id}"\nlog_id("batch_task_logs")${since ? `\ntimestamp >= "${since}"` : ''}`
  return `https://console.cloud.google.com/logs/query;query=${logsQueryParam(query)}?project=${encodeURIComponent(project)}`
}
