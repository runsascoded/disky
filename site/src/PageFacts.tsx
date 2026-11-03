// One compact row under the map's own section (its drawer and table): the drilled node's storage-class mix as a
// stacked bar (its bytes and list-price $/mo per class in the tooltip, and in a table the row's disclosure opens —
// `?cx`), and the ownership share with its explainer behind an ⓘ. The class mix follows the map's drill; both hide
// under a name filter (`filterBlind.ts`).
import { useEffect, type ReactNode } from 'react'
import { boolParam, useUrlState } from 'use-prms'
import { useStore } from './store'
import { ClassMixTip, Tooltip } from './Tooltip'
import { useUnits } from './units'
import { CLASS_COLORS, CLASS_PRICE_US, fmtUsd, userBytes, type TreeNode } from './types'
import { classSegments, fracLabel, type ClassSeg } from './classStrip'

/** The facts row: the class strip (with its table behind the disclosure) and the ownership share, either absent. */
export function PageFacts({ mix, scope, tree }: {
  /** The drilled node's class mix (`classMix`); absent where the store has no prices. */
  mix?: Record<string, number>
  /** The path the mix is of. */
  scope: ReactNode
  /** The map's tree, for the ownership share; absent where the store has no owners. */
  tree?: TreeNode
}) {
  const [open, setOpen] = useUrlState('cx', boolParam)
  const segs = mix ? classSegments(mix) : null
  const classes = segs && segs.segs.length > 0
  if (!classes && !tree) return null
  return (
    <div className="page-facts">
      <div className="facts-row">
        {classes && <ClassStrip mix={mix!} scope={scope} open={open} onToggle={() => setOpen(!open)} />}
        {tree && <OwnershipFact tree={tree} />}
      </div>
      {classes && open && <ClassTable segs={segs.segs} usd={segs.usd} scope={scope} />}
    </div>
  )
}

/** The drilled node's class mix for `scope` (the path it names); its caret opens the per-class table. */
function ClassStrip({ mix, scope, open, onToggle }: { mix: Record<string, number>; scope: ReactNode; open: boolean; onToggle: () => void }) {
  const { segs, usd } = classSegments(mix)
  const tip = (
    <>
      <div className="classes-scope">Storage classes under <code>{scope}</code></div>
      <ClassMixTip mix={mix} note="List prices (US regions) × scanned bytes; follows the map's drill. Actual spend depends on negotiated rates and credits." />
    </>
  )
  return (
    <span className="fact classes-strip" id="storage-classes">
      <button type="button" className="disclose" aria-expanded={open} aria-controls="classes-table" onClick={onToggle}>
        <span className="caret" aria-hidden="true">{open ? '▾' : '▸'}</span><span className="lbl">classes</span>
      </button>
      <Tooltip content={tip} pinnable>
        <span className="classbar" tabIndex={0} role="img" aria-label={segs.map(s => `${s.name} ${fracLabel(s.frac)}`).join(', ')}>
          {segs.map(s => <i key={s.c} style={{ flexGrow: s.frac, background: CLASS_COLORS[s.c] ?? 'var(--other)' }} />)}
        </span>
      </Tooltip>
      <span className="classes-legend">
        {segs.map(s => (
          <span key={s.c} className="li"><i className="sw" style={{ background: CLASS_COLORS[s.c] ?? 'var(--other)' }} />{s.name} <span className="pct">{fracLabel(s.frac)}</span></span>
        ))}
      </span>
      <span className="usd">≈ {fmtUsd(usd)}/mo</span>
    </span>
  )
}

/** The disclosure's table: per class its bytes, share, list price and est. $/mo, and the total. */
function ClassTable({ segs, usd, scope }: { segs: ClassSeg[]; usd: number; scope: ReactNode }) {
  const { fmtBytes } = useUnits()
  const bytes = segs.reduce((s, x) => s + x.bytes, 0)
  return (
    <div className="classes-table" id="classes-table">
      <p className="cap">Storage classes under <code>{scope}</code> · list prices (US regions) × scanned bytes; actual spend depends on negotiated rates and credits</p>
      <table>
        <thead>
          <tr><th>class</th><th className="num">bytes</th><th className="num">share</th><th className="num rate">list $/GiB·mo</th><th className="num">est. $/mo</th></tr>
        </thead>
        <tbody>
          {segs.map(s => (
            <tr key={s.c}>
              <td><i className="sw" style={{ background: CLASS_COLORS[s.c] ?? 'var(--other)' }} />{s.name}</td>
              <td className="num">{fmtBytes(s.bytes)}</td>
              <td className="num">{fracLabel(s.frac)}</td>
              <td className="num rate">${CLASS_PRICE_US[s.c] ?? 0.02}</td>
              <td className="num">{fmtUsd(s.usd)}</td>
            </tr>
          ))}
        </tbody>
        {segs.length > 1 && (
          <tfoot>
            <tr><td>total</td><td className="num">{fmtBytes(bytes)}</td><td /><td className="rate" /><td className="num">{fmtUsd(usd)}</td></tr>
          </tfoot>
        )}
      </table>
    </div>
  )
}

/** How owners are decided, and whom to ask: the explainer that used to be the "Ownership" section. */
export function OwnershipHelp() {
  const { contact } = useStore()
  return (
    <div className="ownership-help">
      <p>
        An owner comes from one of two places. An <b>assignment</b> — someone assigning a prefix to a person
        (themselves or anyone else) from the map, the CLI, or the API — is the source of truth and always wins.
        Everything unassigned falls back to <b>inferred</b> ownership (W&amp;B run configs, <code>.executor_info</code>{' '}
        sidecars, provenance records, and a short list of manual prefix rules): “this user's runs wrote these bytes” — a
        starting point for finding your data, not a bill.
      </p>
      <p>
        Ownership has one axis: a person, or nobody. Bytes without an owner show as <i>unowned</i> (gray) until someone
        assigns them — shared corpora and infra included, because a deletion needs a person to sign off.
      </p>
      {contact && (contact.email || contact.chat) && (
        <p className="feedback">
          Questions, a wrong owner, access for a teammate:{' '}
          {contact.email && <a href={`mailto:${contact.email}`}>{contact.email}</a>}
          {contact.email && contact.chat && ' or '}
          {contact.chat && <a href={contact.chat.href} target="_blank" rel="noreferrer">{contact.chat.label}</a>}.
        </p>
      )}
    </div>
  )
}

/** The share of `tree`'s bytes that have an owner, with the explainer (pin the ⓘ to click its links). */
function OwnershipFact({ tree }: { tree: TreeNode }) {
  // deep-linkable: the row mounts after data loads, so honor #attribution then
  useEffect(() => {
    if (location.hash === '#attribution') document.getElementById('attribution')?.scrollIntoView()
  }, [])
  const pct = tree.b ? ((100 * userBytes(tree)) / tree.b).toFixed(1) : '0.0'
  return (
    <span className="fact ownership-fact" id="attribution">
      <span className="lbl">ownership</span>
      <span><b>{pct}%</b> of bytes have an owner</span>
      <Tooltip content={<OwnershipHelp />} pinnable>
        <span className="info" tabIndex={0} aria-label="how ownership works">ⓘ</span>
      </Tooltip>
    </span>
  )
}
