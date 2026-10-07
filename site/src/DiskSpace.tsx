import type { DiskSpace as Space } from './types'
import { Tooltip } from './Tooltip'
import { useUnits } from './units'

export function DiskSpace({ space }: { space?: Space }) {
  const { fmtBytes } = useUnits()
  if (!space) return null
  const percent = 100 * space.used / space.capacity
  const used = `${percent.toFixed(1)}% used`
  const free = `${fmtBytes(space.free)} free`
  return (
    <section className="disk-space" aria-label="Physical disk space at scan">
      <div className="disk-space-labels">
        <span><b>{free}</b> <span className="dim">of {fmtBytes(space.capacity)}</span></span>
        <span>{used} <Tooltip pinnable content={<>
          <div>Physical APFS container ({space.device}), shared by all its volumes.</div>
          <div>{fmtBytes(space.used)} used · {free} · {fmtBytes(space.capacity)} total</div>
          <div>Recorded during the scan started {new Date(space.captured_at).toLocaleString()}.</div>
          <div>The map sums per-path sizes; APFS clones can share blocks. Disk usage counts those blocks once and includes system volumes and snapshots.</div>
        </>}><span className="dim">at scan ⓘ</span></Tooltip></span>
      </div>
      <div className="disk-space-bar" role="meter" aria-label="Disk used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-valuetext={`${used}, ${free}`}>
        <span style={{ width: `${percent}%` }} />
      </div>
    </section>
  )
}
