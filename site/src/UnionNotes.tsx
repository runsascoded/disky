/** What a union view's answers say about their members (`unionSources.ts`), on the page: a root's store and
 *  scan beside its name, a member that refused the filter, a series' stretch some member hadn't scanned yet. */
import { fmtScan } from './scan'
import { SourceMark } from './SourceMark'
import { fmtCutoff } from './federatedPickerModel'
import { missingSources, refusedSources, rootSource, seriesMissing, useUnionMarks, type UnionVector } from './unionSources'

/** A union root child's store mark, its tooltip that store's scan ("Index · scan 10/14 6:02a"). Nothing for any
 *  other node. */
export function RootSourceTag({ node }: { node: { src?: string; at?: string } }) {
  const s = rootSource(node, useUnionMarks())
  if (!s) return null
  const text = `${s.mark.label} · scan ${fmtScan(s.at)}`
  return <span className="src-tag"><SourceMark mark={s.mark} tip={text} name={text} /></span>
}

/** The same, inside the map's hover box: icon, label and scan in one line. */
export function RootSourceLine({ node }: { node?: { src?: string; at?: string } }) {
  const s = rootSource(node ?? {}, useUnionMarks())
  if (!s) return null
  return <div className="tt-src"><SourceMark mark={s.mark} inline /> · scan {fmtScan(s.at)}</div>
}

/** A filtered union root's members that refused the search, each named: "· Index not searched (its scans have
 *  no name index)". Nothing when every member answered. */
export function UnionRefusals({ union }: { union?: UnionVector }) {
  const refused = refusedSources(union, useUnionMarks())
  if (!refused.length) return null
  return (
    <span className="union-refused" role="status">
      {refused.map(r => <span key={r.mark.source}> · <SourceMark mark={r.mark} /> not searched ({r.error})</span>)}
    </span>
  )
}

/** A union series' partial stretch: "Partial through 10/1: no Index scan yet." Nothing when every point is whole. */
export function SeriesMissingNote({ points }: { points?: readonly { date: string; missing?: string[] }[] }) {
  const missing = seriesMissing(points ?? [], useUnionMarks())
  if (!missing.length) return null
  return (
    <p className="hint series-missing" role="status">
      {missing.map(m => <span key={m.mark.source}>Partial through {fmtScan(m.through)}: no <SourceMark mark={m.mark} /> scan yet. </span>)}
    </p>
  )
}

/** A union root's members with no scan at or before its cutoff: "No Index scan at or before Oct 13, 7:35 AM: not
 *  on the map or in the table." Nothing when every member has one. */
export function UnionMissingNote({ union }: { union?: UnionVector }) {
  const missing = missingSources(union, useUnionMarks())
  if (!missing.length || !union?.at) return null
  const when = fmtCutoff(union.at, Intl.DateTimeFormat().resolvedOptions().timeZone)
  return (
    <p className="tab-note union-missing" role="status">
      No {missing.map((m, i) => <span key={m.source}>{i ? ' or ' : ''}<SourceMark mark={m} inline /></span>)} scan at or before {when}: not on the map or in the table.
    </p>
  )
}
