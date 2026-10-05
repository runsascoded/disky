import { Avatar } from './Avatar'
import { Tooltip } from './Tooltip'
import type { Provenance } from './types'
import { SOURCE_LABELS } from './types'
import { UserChip, ghHandle, shortName } from './UserChip'

/** Short date for a ledger timestamp, e.g. "Aug 24, 2026". */
export const fmtDate = (ts: number): string =>
  new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })

/**
 * One ownership fact, with where it came from (gcs:specs/assignment-provenance.md
 * § Surfaces): the owner's chip plus a provenance mark — the assigner's
 * avatar for an assignment, a source glyph for a pipeline signal — and one
 * tooltip. Used wherever an owner is shown: the owner panel, the children
 * table, the treemap tooltip.
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
        <span className="owner-fact">{chip}<span className="mark by"><Avatar github={ghHandle(assigned.by)} name={shortName(assigned.by)} size={11} /></span></span>
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
        <span className="owner-fact">{chip}<span className={`mark src src-${source}`} aria-label={source}>{GLYPH[source] ?? '∙'}</span></span>
      </Tooltip>
    )
  }
  return chip
}

const GLYPH: Record<string, string> = {
  'user-prefix': '/', 'artifact-record': '▤', rule: '§', manual: '§',
  'wandb-run': 'W', 'wandb-config': 'W', 'executor-wandb': 'W', 'iris-path': '⟁',
}
