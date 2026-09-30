/** In-flight marker for a widget that keeps showing its last content while
 * the next loads: centered over a dimmed host (`.busy-host.stale`), or a
 * small corner pill when the shown content is current and only a refresh or
 * fill-in is pending. The host is `position: relative`. */
/** A stand-in for a widget that has nothing to show yet: reserves its
 * height (so the page doesn't reflow when it lands) with the in-flight
 * marker centered. */
export function Skeleton({ height, label, className }: { height: number | string; label?: string; className?: string }) {
  return (
    <div className={`skel busy-host stale${className ? ` ${className}` : ''}`} style={{ height }} aria-busy="true" role="status">
      <Busy label={label} />
    </div>
  )
}

export function Busy({ label, corner = false }: { label?: string; corner?: boolean }) {
  return (
    <div className={corner ? 'busy-overlay corner' : 'busy-overlay'} role="status" aria-live="polite">
      <span className="pill"><span className="spin" aria-hidden="true" />{label ?? 'loading…'}</span>
    </div>
  )
}
