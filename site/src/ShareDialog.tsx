// The "Share…" dialog (specs/done/dogi.md): one choice of link to copy. A plain
// link is the browser's job; this dialog makes the two that need minting:
// a detailed link preview (`og=`; sign-in still required) or, for admins for
// now, a link anyone can view (`key=`, a read-only grant; its preview is
// detailed too). Offered on every page, so opening "anyone can view" to
// non-admins later is only a permission change.
import { FloatingPortal } from '@floating-ui/react'
import { useEffect, useState } from 'react'
import { useCanAssign } from './auth'
import { fmtShareDays, SHARE_DAY_CHOICES, SHARE_DAYS, useShare, type ShareMode } from './sharePreview'

type Minted = Exclude<ShareMode, 'plain'>

function Expiry({ days, onChange, label }: { days: number; onChange: (d: number) => void; label: string }) {
  return (
    <select className="share-days" aria-label={label} value={days} onChange={e => onChange(Number(e.target.value))}>
      {SHARE_DAY_CHOICES.map(d => <option key={d} value={d}>{fmtShareDays(d)}</option>)}
    </select>
  )
}

export function ShareDialog({ onClose }: { onClose: () => void }) {
  const canGrant = useCanAssign()
  const [mode, setMode] = useState<Minted>('preview')
  const [days, setDays] = useState<Record<Minted, number>>({ preview: SHARE_DAYS, access: SHARE_DAYS })
  const setModeDays = (m: Minted) => (d: number) => setDays(s => ({ ...s, [m]: d }))
  const { share, status, reset } = useShare()
  useEffect(() => reset(), [mode, days]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  // Portaled to <body>: rendered inside the nav, the backdrop would stack under
  // the page's own panels.
  return (
    <FloatingPortal>
    <div className="token-backdrop" onClick={onClose}>
      <div className="token-modal share-modal" onClick={e => e.stopPropagation()} role="dialog" aria-label="Share">
        <div className="token-head">
          <h2>Share this view</h2>
          <button className="token-x" type="button" onClick={onClose} aria-label="Close">×</button>
        </div>
        <label className="share-opt">
          <input type="radio" name="share-mode" checked={mode === 'preview'} onChange={() => setMode('preview')} />
          <span>
            Detailed preview only · sign-in still required
            <span className="hint">The card Slack, iMessage etc. show when the link is pasted shows labels, sizes and owners. It grants no access: opening the page still needs the viewer's own sign-in. For this exact view; admins can revoke it.</span>
            <Expiry days={days.preview} onChange={setModeDays('preview')} label="Detailed preview expiry" />
          </span>
        </label>
        <label className={`share-opt${canGrant ? '' : ' disabled'}`}>
          <input type="radio" name="share-mode" checked={mode === 'access'} disabled={!canGrant} onChange={() => setMode('access')} />
          <span>
            Guest access · anyone with this link can browse{!canGrant && <span className="dim"> · admins only for now</span>}
            <span className="hint">A read-only guest link, revocable on /admin: whoever opens it browses the whole site without signing in, not just this view. Its preview is detailed too.</span>
            <Expiry days={days.access} onChange={setModeDays('access')} label="Guest access expiry" />
          </span>
        </label>
        <div className="share-actions">
          <button type="button" className="btn primary" disabled={status.state === 'busy'} onClick={() => void share(mode, days[mode])}>
            {status.state === 'busy' ? 'Copying…' : status.state === 'copied' ? 'Copied ✓' : 'Copy link'}
          </button>
          {status.state === 'error' && <span className="err">{status.error}</span>}
        </div>
        {status.state === 'copied' && <code className="share-url">{status.url}</code>}
      </div>
    </div>
    </FloatingPortal>
  )
}
