import { type ReactNode, useState } from 'react'
import type { SourceMarkInfo } from './federatedPickerModel'
import { Tooltip } from './Tooltip'
import './federatedScans.scss'

/** A source's mark (a union member, a federated project): its icon, named by its label (`aria-label`) with the
 *  label in a hover tooltip; with no icon, the label itself. Icons come from the deployment's config (a union
 *  view's `sources`, a catalog's `icon`), never a default here.
 *
 *  `tip` replaces the tooltip's content (the label, by default; a label-only mark gets a tooltip only with one),
 *  and `name` the accessible name. `inline`: icon and label side by side, no tooltip — for inside a tooltip. An
 *  icon that fails to load falls back to the label. */
export function SourceMark({ mark, tip, name, inline }: { mark: SourceMarkInfo; tip?: ReactNode; name?: string; inline?: boolean }) {
  const [broken, setBroken] = useState(false)
  if (broken) mark = { source: mark.source, label: mark.label }
  const img = mark.icon && <img src={mark.icon} alt="" aria-hidden draggable={false} onError={() => setBroken(true)} />
  if (inline) return <span className="src-mark src-mark-inline">{img}<span>{mark.label}</span></span>
  const el = mark.icon
    ? <span className="src-mark src-mark-icon" role="img" aria-label={name ?? mark.label}>{img}</span>
    : <span className="src-mark src-mark-label" {...(name ? { 'aria-label': name } : {})}>{mark.label}</span>
  if (!mark.icon && tip === undefined) return el
  return <Tooltip content={tip ?? mark.label}>{el}</Tooltip>
}
