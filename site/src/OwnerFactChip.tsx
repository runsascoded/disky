import { Avatar } from './Avatar'
import { Tooltip } from './Tooltip'
import type { Provenance } from './types'
import { SOURCE_LABELS } from './types'
import { UserChip, canonId, ghHandle, shortName } from './UserChip'

/** Short date for a ledger timestamp, e.g. "Aug 24, 2026". */
export const fmtDate = (ts: number): string =>
  new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })

/**
 * One ownership fact, with where it came from (gcs:specs/assignment-provenance.md
 * § Surfaces): the owner's chip plus a provenance mark — the assigner's
 * avatar for an assignment, a source glyph for a pipeline signal — and one
 * tooltip. Used wherever an owner is shown: the owner panel, the children
 * table, the treemap tooltip. A self-assignment carries no mark (the assigner's
 * avatar would repeat the owner's); its tooltip still says so.
 */
export function OwnerFactChip({ who, size = 15, assigned, inferred }: {
  who: string
  size?: number
  /** An assignment from the ledger: who made it and when. */
  assigned?: { by: string; ts: number; memo?: string | null } | null
  /** The pipeline's provenance for an inferred attribution. */
  inferred?: Provenance | null
}) {
  const chip = <UserChip who={who} size={size} />
  if (assigned) {
    return (
      <Tooltip content={
        <span className="prov-tip">
          <div>assigned by <Avatar github={ghHandle(assigned.by)} name={shortName(assigned.by)} size={13} /> <b>{shortName(assigned.by)}</b> · {fmtDate(assigned.ts)}</div>
          {assigned.memo && <div className="memo">“{assigned.memo}”</div>}
        </span>
      }>
        <span className="owner-fact">{chip}{canonId(assigned.by) !== canonId(who) && <span className="mark by"><Avatar github={ghHandle(assigned.by)} name={shortName(assigned.by)} size={11} /></span>}</span>
      </Tooltip>
    )
  }
  if (inferred) {
    const [source, evidence, prefix] = inferred
    const wandb = /^wandb|executor-wandb/.test(source) && evidence && /^[^/]+\/[^/]+\/[^/]+$/.test(evidence)
    return (
      <Tooltip content={
        <span className="prov-tip">
          <div>inferred from {SOURCE_LABELS[source] ?? source}</div>
          {evidence && (
            <div className="memo">
              {wandb
                ? <a href={`https://wandb.ai/${evidence.split('/')[0]}/${evidence.split('/')[1]}/runs/${evidence.split('/')[2]}`} target="_blank" rel="noreferrer">{evidence} ↗</a>
                : <code>{evidence}</code>}
            </div>
          )}
          <div>at <code>gs://{prefix}/</code></div>
        </span>
      }>
        <span className="owner-fact">{chip}<span className={`mark src src-${source}`} aria-label={source}>{WANDB_SOURCES.has(source) ? <WandbMark /> : GLYPH[source] ?? '∙'}</span></span>
      </Tooltip>
    )
  }
  return chip
}

const GLYPH: Record<string, string> = {
  'user-prefix': '/', 'artifact-record': '▤', rule: '§', manual: '§', 'iris-path': '⟁',
}

const WANDB_SOURCES = new Set(['wandb-run', 'wandb-config', 'executor-wandb'])

/** The Weights & Biases mark (simple-icons `weightsandbiases`, CC0), in the mark's color (`currentColor`). */
function WandbMark() {
  return (
    <svg viewBox="0 0 24 24" width="9" height="9" fill="currentColor" aria-hidden="true">
      <path d="M2.48 0a1.55 1.55 0 1 0 0 3.1 1.55 1.55 0 0 0 0-3.1zm19.04 0a1.55 1.55 0 1 0 0 3.101 1.55 1.55 0 0 0 0-3.101zM12 2.295a1.55 1.55 0 1 0 0 3.1 1.55 1.55 0 0 0 0-3.1zM2.48 5.272a2.48 2.48 0 1 0 0 4.96 2.48 2.48 0 0 0 0-4.96zm19.04 0a2.48 2.48 0 1 0 0 4.96 2.48 2.48 0 0 0 0-4.96zM12 8.496a1.55 1.55 0 1 0 0 3.1 1.55 1.55 0 0 0 0-3.1zm-9.52 3.907a1.55 1.55 0 1 0 0 3.1 1.55 1.55 0 0 0 0-3.1zm19.04 0a1.55 1.55 0 1 0 0 3.102 1.55 1.55 0 0 0 0-3.102zM12 13.767a2.48 2.48 0 1 0 0 4.962 2.48 2.48 0 0 0 0-4.962zm-9.52 3.907a2.48 2.48 0 1 0 .001 4.962 2.48 2.48 0 0 0 0-4.962zm19.04.93a1.55 1.55 0 1 0 0 3.102 1.55 1.55 0 0 0 0-3.101zM12 20.9a1.55 1.55 0 1 0 0 3.1 1.55 1.55 0 0 0 0-3.1Z" />
    </svg>
  )
}
