import { encodeScan, fmtScan, type Scan } from './scan'

// Compact, page-agnostic scan picker for the shared nav (see
// specs/scan-param-all-pages.md). The home map renders its own richer inline
// picker (interleaved with byte/cost/published meta); this is the plain
// select + ambiguous-`?d` disambiguation strip that /users and /user/:id show
// in SiteNav. Absent on pages that aren't scoped to one scan.
export function ScanPicker({ scan }: { scan: Scan }) {
  const { asof, scans, dMatches, dP, setDP } = scan
  if (scans.length < 2 || !asof) return null
  return (
    <span className="scan-nav">
      <label className="scan-lbl">scan</label>
      <select className="scanpick" value={asof} onChange={e => setDP(e.target.value)} aria-label="Scan date">
        {scans.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
      </select>
      {/* Ambiguous `?d`: newest match is shown (a best guess beats a dead end);
          the strip lists every candidate to pin one. */}
      {dMatches.length > 1 && (
        <span className="disambig-inline" title={`?d=${encodeScan(dP) ?? dP} matches ${dMatches.length} scans`}>
          {dMatches.map(s => (
            <button key={s} type="button" className={s === asof ? 'on' : ''} onClick={() => setDP(s)}>{fmtScan(s)}</button>
          ))}
        </span>
      )}
    </span>
  )
}
