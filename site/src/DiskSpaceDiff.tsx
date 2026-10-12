import type { DiskSpace } from './types'
import { useUnits } from './units'

export interface SpaceChange {
  label: string
  before: number
  after: number
  change: number
}

/** Physical readings are comparable only within the same APFS container. */
export function spaceChanges(before?: DiskSpace, after?: DiskSpace): SpaceChange[] {
  if (!before || !after || before.device !== after.device || before.capacity !== after.capacity) return []
  const row = (label: string, a: number, b: number): SpaceChange => ({ label, before: a, after: b, change: b - a })
  return [
    row('Free space', before.free, after.free),
    row('Disk used', before.used, after.used),
    ...(before.vm_used != null && after.vm_used != null ? [row('VM (swap volume)', before.vm_used, after.vm_used)] : []),
  ]
}

export function DiskSpaceDiff({ before, after }: { before?: DiskSpace; after?: DiskSpace }) {
  const { fmtBytes } = useUnits()
  const rows = spaceChanges(before, after)
  if (!rows.length) return null
  const signed = (n: number) => `${n > 0 ? '+' : n < 0 ? '−' : ''}${fmtBytes(Math.abs(n))}`
  return (
    <section className="disk-space-diff" aria-label="Whole-disk physical space change">
      <h3>Whole-disk space at scan</h3>
      <table>
        <thead><tr><th>measurement</th><th>before</th><th>after</th><th>change</th></tr></thead>
        <tbody>{rows.map(r => <tr key={r.label}><th scope="row">{r.label}</th><td>{fmtBytes(r.before)}</td><td>{fmtBytes(r.after)}</td><td>{signed(r.change)}</td></tr>)}</tbody>
      </table>
      <p className="dim">The file diff below excludes the separate VM (swap) volume. These physical measurements cover the whole disk, including other APFS volumes and snapshots, regardless of the selected folder or filter. VM allocation includes swap and filesystem overhead.</p>
    </section>
  )
}
