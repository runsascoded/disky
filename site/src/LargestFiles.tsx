import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { intParam, useUrlState } from 'use-prms'
import { dateColor, epochDaysToMonthShort } from './colors'
import { elideMid } from './CopyName'
import { pathText } from './pathCrumbs'
import { useStore, useStoreFetch } from './store'
import { Tooltip } from './Tooltip'
import { useUnits } from './units'

// "Large Files": the biggest objects anywhere under the drilled directory —
// files only, recursively, size-descending (`/api/largest`, read from the
// store's `bysize` sort). The children table's flat twin: where it lists the
// directory's direct children, this lists what a cleanup would actually go
// after, however deep it sits.

/** One listed object: its full store path, bytes, and stamp (epoch seconds). */
export interface LargeFile { path: string; size: number; mtime: number | null }
export interface Largest { date: string; path: string; n: number; files: LargeFile[]; more: boolean; groups: number; rows: number; capped?: boolean }

/** How many to list: `?ln=` (default 20). */
const COUNTS = [20, 50, 100, 500]
const NAME_MAX = 60

/** `/api/largest` for a store path at a scan; `enabled` false = not asked
 *  (a scoped view: the list is the whole directory's, never a lens's). */
export function useLargest(path: string, scan: string | null | undefined, n: number, enabled = true) {
  const store = useStore()
  const sfetch = useStoreFetch()
  return useQuery({
    queryKey: ['largest', store.key, scan, path, n],
    enabled: !!scan && enabled,
    staleTime: Infinity,
    // 409 (a scan indexed before objects were listed) is deterministic.
    retry: (k: number, e: Error) => !/^4\d\d/.test(e.message) && k < 2,
    queryFn: async ({ signal }) => {
      const r = await sfetch(`/api/largest?date=${scan}&path=${encodeURIComponent(path)}&n=${n}`, { credentials: 'include', signal })
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
      return await r.json() as Largest
    },
  })
}

/** A file's path below `segs` (the drilled dir), split into its dir part and basename. */
export function relParts(segs: string[], path: string): { dirSegs: string[]; rel: string[]; base: string } {
  const all = path.split('/')
  const rel = all.slice(segs.length)
  return { dirSegs: all.slice(0, -1), rel: rel.slice(0, -1), base: all[all.length - 1] }
}

export function LargestFiles({ segs, scheme, scan, total, enabled = true, onOpen, onOpenObject }: {
  /** Path segments from the store root to the drilled dir. */
  segs: string[]
  scheme: string
  scan: string | null | undefined
  /** The drilled dir's bytes (the share column's denominator). */
  total: number
  enabled?: boolean
  /** Drill to a directory (segments from the root). */
  onOpen: (segs: string[]) => void
  /** Open an object in the leaf viewer (segments from the root). */
  onOpenObject: (segs: string[]) => void
}) {
  const { fmtBytes } = useUnits()
  const [nP, setNP] = useUrlState('ln', intParam(20))
  const n = COUNTS.includes(nP) ? nP : 20
  const path = segs.join('/')
  const q = useLargest(path, scan, n, enabled)
  const files = useMemo(() => q.data?.files ?? [], [q.data])
  // Created-month ink over the listed rows' range, as in the children table.
  const [dMin, dMax] = useMemo(() => {
    const ds = files.map(f => f.mtime).filter((t): t is number => t != null).map(t => Math.floor(t / 86400))
    return ds.length ? [Math.min(...ds), Math.max(...ds)] : [0, 0]
  }, [files])
  if (!enabled || !scan) return null
  // Nothing to say: a scan without objects (409), or a directory with no files under it.
  if (q.error && /^409/.test(q.error.message)) return null
  if (q.data && !files.length) return null
  const ageInk = (d: number) => dateColor(dMax > dMin ? (d - dMin) / (dMax - dMin) : 1)
  const createdParts = (d: number) => epochDaysToMonthShort(d).split(' ')
  const hasYr = files.some(f => f.mtime != null && createdParts(Math.floor(f.mtime / 86400)).length > 1)
  const sum = files.reduce((s, f) => s + f.size, 0)
  return (
    <section className="children-tbl largest-tbl" aria-label="largest files">
      <h3>
        Largest files
        <Tooltip content={<>
          The biggest files anywhere under <code>{pathText(scheme, segs, true)}</code>, however deep — files only, never folders.
          Sizes are per-path: a cloned or hard-linked file is charged in full here, so deleting it can free less.
        </>}><span className="info" tabIndex={0} aria-label="about largest files"> ⓘ</span></Tooltip>
        <select className="psize" value={n} onChange={e => setNP(+e.target.value)} aria-label="files to list">
          {COUNTS.map(c => <option key={c} value={c}>top {c}</option>)}
        </select>
      </h3>
      {q.error ? <p className="err">Couldn’t list the largest files: {q.error.message}</p>
        : !q.data ? <p className="loading">listing the largest files…</p>
        : (
          <table className="worklist">
            <thead>
              <tr>
                <th>file</th>
                <th className="num">bytes</th>
                <th className="num">share</th>
                <th className="created" colSpan={hasYr ? 3 : 2}>created</th>
              </tr>
            </thead>
            <tbody>
              {files.map(f => {
                const { dirSegs, rel, base } = relParts(segs, f.path)
                const fileSegs = [...dirSegs, base]
                const d = f.mtime == null ? null : Math.floor(f.mtime / 86400)
                const relDir = rel.join('/')
                return (
                  <tr key={f.path}>
                    <td className="prefix">
                      <Tooltip content={<code className="elide-full">{pathText(scheme, fileSegs)}</code>}>
                        <span>
                          {relDir && (
                            <a role="link" tabIndex={0} className="dir"
                              onClick={() => onOpen(dirSegs)}
                              onKeyDown={e => { if (e.key === 'Enter') onOpen(dirSegs) }}>
                              {elideMid(relDir, Math.max(16, NAME_MAX - base.length))}/
                            </a>
                          )}
                          <a role="link" tabIndex={0}
                            onClick={() => onOpenObject(fileSegs)}
                            onKeyDown={e => { if (e.key === 'Enter') onOpenObject(fileSegs) }}>
                            {elideMid(base, NAME_MAX)}
                          </a>
                        </span>
                      </Tooltip>
                    </td>
                    <td className="num">{fmtBytes(f.size)}</td>
                    <td className="num">{total ? ((100 * f.size) / total).toFixed(1) : 0}%</td>
                    {d != null ? (() => {
                      const [mon, yr] = createdParts(d)
                      return <>
                        <td className="created sw"><i style={{ background: ageInk(d) }} /></td>
                        <td className="created mon">{mon}</td>
                        {hasYr && <td className="created yr">{yr ?? ''}</td>}
                      </>
                    })() : <td className="created none num" colSpan={hasYr ? 3 : 2}>—</td>}
                  </tr>
                )
              })}
            </tbody>
            <tfoot>
              <tr className="total-row">
                <td>total ({files.length.toLocaleString('en-US')} {files.length === 1 ? 'file' : 'files'}{q.data.more ? `; more below ${fmtBytes(files[files.length - 1].size)}` : ''})</td>
                <td className="num">{fmtBytes(sum)}</td>
                <td className="num">{total ? ((100 * sum) / total).toFixed(1) : 0}%</td>
                <td colSpan={hasYr ? 3 : 2} />
              </tr>
            </tfoot>
          </table>
        )}
    </section>
  )
}
