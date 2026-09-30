/** `/staged` — the staged-delete queue (spec `specs/staged-delete.md`, CP3;
 *  page UX per `specs/done/staged-page-ux.md`).
 *  Everyone who can see it sees the staged sets + the runs feed; on a gated
 *  deployment only an admin can dispatch (the local server has no gate, so its
 *  single user can). Dispatch enqueues on the cloud edge, deletes inline on the
 *  Flask peer — either way the plan closes and a run is recorded. The Flask
 *  peer also deletes a subset (`uris`) — one row, or the selection — and
 *  previews (dry run).
 *
 *  Each plan is a multi-select table (the scan-details conventions: click /
 *  ⇧-click / ⌘-click, j/k + arrows, ⌘A, Esc; `useRowSelection`), with the
 *  selection's total and bulk delete / unstage above it. A row's path links
 *  to its details page; its kind icon reveals it in Finder (local paths, when
 *  the server can); the copy glyph copies the full URI. */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Box, Button, Checkbox, Tooltip, Typography } from '@mui/material'
import { FaCheck, FaFileAlt, FaFolder, FaRegCopy, FaRegFile } from 'react-icons/fa'
import { dispatchPlan, fetchStaged, revealPath, unstageUris } from '../api'
import type { DispatchResult, StagedItem, StagedPlan, StagedRun } from '../api'
import { useCapabilities } from '../hooks/useCapabilities'
import { useRowSelection, useRowSelectionKeys } from '../hooks/useRowSelection'
import type { RowSelection } from '../hooks/useRowSelection'
import { isAdmin, ssoUrl, useWhoami } from '../auth'
import { uriToPath } from '../schemes'
import { formatSize } from '../utils/format'
import { commonDir, elideMiddle } from '../staged'

const minute = (s: number | null | undefined): string =>
  s == null ? '—' : new Date(s * 1000).toISOString().slice(0, 16).replace('T', ' ') + 'Z'

function runState(r: StagedRun): string {
  if (r.finished_ts == null) return 'enqueued'
  return r.mode === 'real' ? 'done' : 'dry'
}

/** Whether deleting `a` deletes `b` (`b` is `a` or under it) — the engine's
 *  `covers`. Plans staged before nesting was collapsed can still hold both. */
const covers = (a: string, b: string): boolean => b === a || b.startsWith(`${a.replace(/\/+$/, '')}/`)

/** The other staged URI that `uri` lies under, if any. */
const coveredBy = (uri: string, uris: string[]): string | undefined =>
  uris.find(o => o !== uri && covers(o, uri))

/** Total over `items`, counting an item under another of them once (with it). */
const sum = (items: StagedItem[], k: 'bytes' | 'objects'): number | null => {
  const uris = items.map(i => i.uri)
  const own = items.filter(i => !coveredBy(i.uri, uris))
  return own.some(i => i[k] != null) ? own.reduce((a, i) => a + (i[k] ?? 0), 0) : null
}

const plural = (n: number, word: string): string => `${n} ${word}${n === 1 ? '' : 's'}`

const scope = (r: DispatchResult): string =>
  `${formatSize(r.bytes ?? r.deleted_bytes ?? 0)} across ${plural(r.objects ?? r.deleted_objects ?? 0, 'object')}`

/** The kind icon — a Finder-reveal button for a local path when the server
 *  can (`caps.reveal`), else just the glyph. Unknown kind (no covering scan,
 *  or the edge): an outlined file. */
function KindCell({ item, reveal }: { item: StagedItem; reveal: boolean }) {
  const Icon = item.kind === 'dir' ? FaFolder : item.kind === 'file' ? FaFileAlt : FaRegFile
  if (!reveal || !item.uri.startsWith('/')) return <Icon style={{ opacity: 0.6 }} />
  return (
    <Tooltip title="Reveal in Finder" placement="top" arrow>
      <button type="button" className="staged-btn" onClick={() => revealPath(item.uri)}><Icon /></button>
    </Tooltip>
  )
}

/** A row's size — dimmed, and left out of the plan total, when a staged
 *  ancestor already covers it. */
function SizeCell({ item, under, base }: { item: StagedItem; under?: string; base: string }) {
  if (!under) return <>{formatSize(item.bytes ?? 0)}</>
  return (
    <Tooltip title={`Inside ${under.slice(base.length) || under} — deleted with it, counted once`} placement="top" arrow>
      <span style={{ opacity: 0.45 }}>({formatSize(item.bytes ?? 0)})</span>
    </Tooltip>
  )
}

/** A row's path: relative to the plan's shared dir, middle-elided, linking
 *  to its details page; full URI on hover / long-press; a copy glyph after. */
function PathCell({ uri, base }: { uri: string; base: string }) {
  const [copied, setCopied] = useState(false)
  const rel = uri.startsWith(base) ? uri.slice(base.length) : uri
  const copy = () => {
    navigator.clipboard?.writeText(uri).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200) })
  }
  return (
    <>
      <Tooltip title={uri} placement="top" arrow enterTouchDelay={0} leaveTouchDelay={3000}>
        <Link to={uriToPath(uri)}><code className="staged-path">{elideMiddle(rel)}</code></Link>
      </Tooltip>
      <Tooltip title={copied ? 'copied' : 'copy the full path'} placement="top" arrow>
        <button type="button" className="staged-btn" onClick={copy}>{copied ? <FaCheck /> : <FaRegCopy />}</button>
      </Tooltip>
    </>
  )
}

/** The table's keyboard layer (j/k, ⇧, ⌘A, Esc) — registered once, for the
 *  first plan; use-kbd bindings are page-global, so a second plan's table
 *  gets mouse selection only. */
function SelectionKeys({ sel }: { sel: RowSelection<StagedItem> }) {
  useRowSelectionKeys(sel)
  return null
}

/** A pending delete awaiting its confirm click: the whole plan, the selection, or one row. */
type Confirm = { kind: 'plan' } | { kind: 'selected'; uris: string[] } | { kind: 'row'; uri: string } | null

function PlanSection({ plan: p, canDispatch, perItem, reveal, keys }: {
  plan: StagedPlan
  canDispatch: boolean
  perItem: boolean
  reveal: boolean
  keys: boolean
}) {
  const qc = useQueryClient()
  const [confirm, setConfirm] = useState<Confirm>(null)
  const [preview, setPreview] = useState<DispatchResult | null>(null)
  const sel = useRowSelection(p.items, i => i.uri)

  const invalidate = () => qc.invalidateQueries({ queryKey: ['staged'] })
  const done = () => { setPreview(null); setConfirm(null); sel.clear(); invalidate() }
  const unstage = useMutation({ mutationFn: (uris: string[]) => unstageUris(uris), onSuccess: done })
  const dispatch = useMutation({ mutationFn: () => dispatchPlan({ plan: String(p.id) }), onSuccess: done })
  const deleteUris = useMutation({ mutationFn: (uris: string[]) => dispatchPlan({ plan: String(p.id), uris }), onSuccess: done })
  const dryRun = useMutation({
    mutationFn: () => dispatchPlan({ plan: String(p.id), forReal: false }),
    onSuccess: result => { setPreview(result); invalidate() },
  })
  const busy = dispatch.isPending || deleteUris.isPending || dryRun.isPending || unstage.isPending
  const error = dispatch.error ?? deleteUris.error ?? dryRun.error ?? unstage.error

  const uris = p.items.map(i => i.uri)
  const base = commonDir(uris)
  const bytes = sum(p.items, 'bytes')
  const sized = bytes != null
  const selected = sel.selectedRows()
  const selectedUris = selected.map(i => i.uri)
  const selectedBytes = sum(selected, 'bytes')
  const rowConfirming = (uri: string) => confirm?.kind === 'row' && confirm.uri === uri

  /** confirm / cancel for a pending delete of `uris` (the row's, or the selection's). */
  const confirmButtons = (uris: string[], nbytes: number | null) => (
    <>
      <Button size="small" color="error" variant="contained" disabled={busy} onClick={() => deleteUris.mutate(uris)}>
        confirm — delete {uris.length}{nbytes != null ? ` (${formatSize(nbytes)})` : ''}
      </Button>
      <Button size="small" onClick={() => setConfirm(null)}>cancel</Button>
    </>
  )

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {keys && <SelectionKeys sel={sel} />}
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
        <Typography variant="subtitle2">
          Plan {p.id} “{p.name}” — {plural(p.items.length, 'item')}{sized ? `, ${formatSize(bytes)}` : ''}
        </Typography>
        <span className="dim" style={{ fontSize: '0.8rem' }}>by {p.created_by}</span>
        {canDispatch && p.items.length > 0 && (confirm?.kind === 'plan' ? (
          <>
            <Button size="small" color="error" variant="contained" disabled={busy} onClick={() => dispatch.mutate()}>
              confirm — delete {p.items.length}{sized ? ` (${formatSize(bytes)})` : ''}
            </Button>
            <Button size="small" onClick={() => setConfirm(null)}>cancel</Button>
          </>
        ) : (
          <>
            {perItem && <Button size="small" disabled={busy} onClick={() => dryRun.mutate()}>preview</Button>}
            <Button size="small" color="error" disabled={busy} onClick={() => setConfirm({ kind: 'plan' })}>dispatch</Button>
          </>
        ))}
        {selected.length > 0 && (
          <span className="staged-selected">
            <span style={{ opacity: 0.8 }}>
              {selected.length} selected{selectedBytes != null ? ` (${formatSize(selectedBytes)})` : ''}
            </span>
            {canDispatch && (confirm?.kind === 'selected' ? (
              confirmButtons(confirm.uris, selectedBytes)
            ) : (
              <>
                {perItem && (
                  <Button size="small" color="error" disabled={busy} onClick={() => setConfirm({ kind: 'selected', uris: selectedUris })}>
                    delete selected
                  </Button>
                )}
                <Button size="small" disabled={busy} onClick={() => unstage.mutate(selectedUris)}>unstage selected</Button>
              </>
            ))}
            <Button size="small" sx={{ opacity: 0.7 }} onClick={() => sel.clear()}>clear</Button>
          </span>
        )}
      </Box>
      {preview && (
        <Alert severity="info" onClose={() => setPreview(null)}>
          dry run {preview.run_id}: would delete {scope(preview)} — nothing was removed
        </Alert>
      )}
      {base && <div className="dim staged-base">under <code>{base}</code></div>}
      <table className="access-table staged-table">
        <thead>
          <tr>
            <th className="col-check">
              <Checkbox size="small" sx={{ padding: 0 }} checked={sel.pageAll}
                indeterminate={selected.length > 0 && !sel.pageAll} onChange={sel.togglePage} />
            </th>
            <th className="col-icon"></th>
            <th>path</th>
            {sized && <th className="num col-size">size</th>}
            {canDispatch && <th className="col-actions"></th>}
          </tr>
        </thead>
        <tbody>
          {p.items.map((it, idx) => (
            <tr key={it.uri} ref={sel.rowRef(idx)} {...sel.rowProps(idx)}>
              <td className="col-check" onClick={e => e.stopPropagation()}>
                <Checkbox size="small" sx={{ padding: 0 }} checked={sel.isSelected(it)} onChange={() => sel.toggle(idx)} />
              </td>
              <td className="col-icon"><KindCell item={it} reveal={reveal} /></td>
              <td><PathCell uri={it.uri} base={base} /></td>
              {sized && <td className="num col-size"><SizeCell item={it} under={coveredBy(it.uri, uris)} base={base} /></td>}
              {canDispatch && (
                <td className="col-actions">
                  {perItem && (rowConfirming(it.uri) ? (
                    <>
                      <Button size="small" color="error" variant="contained" disabled={busy} onClick={() => deleteUris.mutate([it.uri])}>
                        confirm
                      </Button>
                      <Button size="small" onClick={() => setConfirm(null)}>cancel</Button>
                    </>
                  ) : (
                    <Button size="small" color="error" disabled={busy} onClick={() => setConfirm({ kind: 'row', uri: it.uri })}>delete</Button>
                  ))}
                  {!rowConfirming(it.uri) && (
                    <Button size="small" disabled={busy} onClick={() => unstage.mutate([it.uri])}>unstage</Button>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {error && <Alert severity="error">{String(error)}</Alert>}
    </Box>
  )
}

export function StagedPage() {
  const caps = useCapabilities()
  const { enabled, whoami } = useWhoami()

  // Gated deployments need admin to dispatch; the local server (no gate) lets
  // its single user dispatch.
  const canDispatch = enabled === false || isAdmin(whoami)
  // Subset deletes and dry runs exist on the Flask peer only: the edge enqueues
  // whole plans for a server-side executor (and a cloud function can't reach a
  // laptop's paths anyway).
  const perItem = canDispatch && caps?.static === false

  const staged = useQuery({ queryKey: ['staged'], queryFn: fetchStaged, enabled: caps?.stageDelete === true, retry: false })

  if (caps === undefined) return <p className="dim">loading…</p>
  if (!caps.stageDelete) return <Alert severity="info">This deployment has no staged-delete queue.</Alert>
  if (enabled && whoami === null) return <Alert severity="info">Sign in to see the queue — <a href={ssoUrl('/staged')}>sign in</a>.</Alert>
  if (staged.error) return <Alert severity="error">{String(staged.error)}</Alert>

  const plans = staged.data?.plans ?? []
  const runs = staged.data?.runs ?? []

  return (
    <Box className="staged" sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Typography variant="h6">Staged for deletion</Typography>
      <Typography variant="body2">
        Nothing here is deleted until it's dispatched — staging is opt-in, with no deadline. Stage paths from a
        bucket's listing (the trash icon); {canDispatch ? 'dispatch a plan to delete it' : 'an admin dispatches a plan to delete it'}
        {perItem ? ', or select rows to delete some of it.' : '.'}
      </Typography>

      {plans.length === 0 && <Alert severity="info">No open plans. Stage a path to start one.</Alert>}

      {plans.map((p: StagedPlan, i) => (
        <PlanSection key={p.id} plan={p} canDispatch={canDispatch} perItem={perItem} reveal={caps.reveal === true} keys={i === 0} />
      ))}

      <Typography variant="subtitle2">Recent runs</Typography>
      {runs.length === 0 ? <span className="dim">none yet</span> : (
        <table className="access-table">
          <thead><tr><th>started</th><th>state</th><th>who</th><th className="num">bytes</th><th className="num">objects</th></tr></thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.run_id}>
                <td>{minute(r.started_ts)}</td>
                <td>{runState(r)}</td>
                <td>{r.actor}</td>
                <td className="num">{formatSize(r.deleted_bytes)}</td>
                <td className="num">{r.deleted_objects}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Box>
  )
}
