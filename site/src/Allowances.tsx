import type { Allowances as Spec } from './stores'
import { Tooltip } from './Tooltip'
import { useUnits } from './units'

/** The store's declared allowances (`Store.allowances`) against the page scan's `class_bytes`: one meter per
 *  allowance over its storage classes, and the used / free split. Store-wide figures, so the root view shows them. */
export function Allowances({ spec, classBytes }: { spec?: Spec; classBytes?: Record<string, number> }) {
  const { fmtBytes } = useUnits()
  if (!spec || !classBytes) return null
  return (
    <section className="allowances" aria-label="Storage allowances">
      {spec.rows.map(a => {
        const used = a.classes.reduce((s, c) => s + (classBytes[c] ?? 0), 0)
        const pct = 100 * used / a.bytes
        const free = `${fmtBytes(Math.max(a.bytes - used, 0))} free`
        return (
          <div className="allowance" key={a.label}>
            <div className="disk-space-labels">
              <span>{a.label}: <b>{fmtBytes(used)}</b> <span className="dim">of {fmtBytes(a.bytes)}</span></span>
              <span>{pct < 0.1 && used > 0 ? '<0.1' : pct.toFixed(1)}% · {free}</span>
            </div>
            <Tooltip content={<>{a.label}: {fmtBytes(used)} of the {a.name} allowance{spec.source && <>, as of {spec.source.asof}</>}</>}>
              <div className="disk-space-bar" role="meter" aria-label={a.label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-valuetext={`${pct.toFixed(1)}%, ${free}`}>
                <span style={{ width: `${Math.min(pct, 100)}%` }} />
              </div>
            </Tooltip>
          </div>
        )
      })}
      {(spec.note || spec.source) && (
        <p className="dim allowances-note">
          {spec.note}{spec.note && spec.source && ' '}
          {spec.source && <>Allowances from the <a href={spec.source.href} target="_blank" rel="noreferrer">{spec.source.label} ↗</a> (<span className="nowrap">{spec.source.asof}</span>).</>}
        </p>
      )}
    </section>
  )
}
