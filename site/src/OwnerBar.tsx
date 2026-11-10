import { useState } from 'react'
import { Avatar } from './Avatar'
import { userColor } from './colors'
import type { UserIndexEntry } from './colors'
import { ClassMixTip, Tooltip } from './Tooltip'
import type { TreeNode } from './types'
import { CLASS_COLORS, CLASS_NAMES, classMix, unownedBytes } from './types'
import { ghHandle, shortName } from './UserChip'
import { useUnits } from './units'

/** Who holds a node's bytes, sorted by share (the scan's attribution). */
export const ownerShares = (node: TreeNode): [string, number][] =>
  node.owners && node.bytes > 0 ? [...node.owners].sort((x, y) => y[1] - x[1]) : []

const TOP = 8

/**
 * Attribution as a distribution: a stacked bar of who holds the bytes under
 * a node, in the color-by-owner palette. Hover lists names / bytes / shares;
 * click PINS the list so it can be used: "… and N more" expands it, and a
 * person's row applies the page's owner filter (`onPickUser`). Replaces any
 * single "top user 35%" — one person's share is not an owner.
 */
export function OwnerBar({ node, userIdx, width = 110, note, onPickUser }: {
  node: TreeNode
  userIdx?: Map<string, UserIndexEntry>
  width?: number
  /** First line of the tooltip (context: assigned or not). */
  note?: string
  onPickUser?: (u: string) => void
}) {
  const { fmtBytes } = useUnits()
  const [all, setAll] = useState(false)
  const shares = ownerShares(node)
  if (shares.length === 0 && !node.unknown_owner_bytes) return null
  const unowned = unownedBytes(node)
  const color = (u: string) => (userIdx ? userColor(u, userIdx) : 'var(--ink-3)')
  const pct = (b: number) => Math.round((100 * b) / node.bytes)
  const rows = all ? shares : shares.slice(0, TOP)
  const tip = (
    <span className="own-tip">
      <div>{note ?? 'The scan attributes the bytes here to:'}</div>
      {rows.map(([u, b]) => (
        <div className={`row${onPickUser ? ' pick' : ''}`} key={u} onClick={onPickUser ? () => onPickUser(u) : undefined} title={onPickUser ? 'show only this person' : undefined}>
          <i style={{ background: color(u) }} />
          <Avatar github={ghHandle(u)} name={shortName(u)} size={13} /> {shortName(u)}
          <span className="n">{fmtBytes(b)} · {pct(b)}%</span>
        </div>
      ))}
      {shares.length > TOP && !all && <div className="more" onClick={() => setAll(true)}>… and {shares.length - TOP} more</div>}
      {unowned > 0 && (
        <div className="row"><i style={{ background: 'var(--t-unattr)' }} />unattributed<span className="n">{fmtBytes(unowned)} · {pct(unowned)}%</span></div>
      )}
      {node.unknown_owner_bytes ? <div className="row"><i style={{ background: 'var(--other)' }} />owner detail unavailable<span className="n">{fmtBytes(node.unknown_owner_bytes)} · {pct(node.unknown_owner_bytes)}%</span></div> : null}
      <div className="how">Attribution comes from paths, W&B run metadata and sidecar files; assigning overrides it.{onPickUser ? ' Click a person to show only their data.' : ''}</div>
    </span>
  )
  return (
    <Tooltip content={tip} pinnable>
      <span className="own-bar" style={{ width }} aria-label="ownership distribution">
        {shares.map(([u, b]) => <i key={u} style={{ width: `${(100 * b) / node.bytes}%`, background: color(u) }} />)}
        {node.unknown_owner_bytes ? <i style={{ width: `${(100 * node.unknown_owner_bytes) / node.bytes}%`, background: 'var(--other)' }} /> : null}
      </span>
    </Tooltip>
  )
}

/** Storage-class mix of a node as a bar (Standard → Archive, `CLASS_COLORS`); the per-class bytes / rate / $ table on hover. */
export function ClassBar({ node, width = 110 }: { node: TreeNode; width?: number }) {
  const mix = classMix(node)
  const rows = Object.entries(mix).filter(([, b]) => b > 0).sort((a, b) => +a[0] - +b[0])
  if (!node.bytes || rows.length === 0) return null
  return (
    <Tooltip content={<ClassMixTip mix={mix} />} pinnable>
      <span className="own-bar class-bar" style={{ width }} aria-label="storage-class distribution">
        {rows.map(([c, b]) => <i key={c} title={CLASS_NAMES[c]} style={{ width: `${(100 * b) / node.bytes}%`, background: CLASS_COLORS[c] ?? 'var(--other)' }} />)}
      </span>
    </Tooltip>
  )
}
