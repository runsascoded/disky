// The "Share…" dialog (specs/done/dogi.md): one choice of link to copy. A plain
// link is the browser's job; this dialog makes the two that need minting:
// a detailed link preview (`og=`; sign-in still required) or, for admins for
// now, a link anyone can view (`key=`, a read-only grant; its preview is
// detailed too). Offered on every page, so opening "anyone can view" to
// non-admins later is only a permission change.
import { FloatingPortal } from '@floating-ui/react'
import { useEffect, useState } from 'react'
import { useCanAssign } from './auth'
import { SHARE_DAYS, useShare, type ShareMode } from './sharePreview'

export function ShareDialog({ onClose }: { onClose: () => void }) {
  const canGrant = useCanAssign()
  const [mode, setMode] = useState<Exclude<ShareMode, 'plain'>>('preview')
  const { share, status, reset } = useShare()
  useEffect(() => reset(), [mode]) // eslint-disable-line react-hooks/exhaustive-deps
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
            Detailed preview · sign-in required
            <span className="hint">The card Slack, iMessage etc. show when the link is pasted shows labels, sizes and owners. Opening the page still needs sign-in. Good for this exact view for {SHARE_DAYS} days; admins can revoke it.</span>
          </span>
        </label>
        <label className={`share-opt${canGrant ? '' : ' disabled'}`}>
          <input type="radio" name="share-mode" checked={mode === 'access'} disabled={!canGrant} onChange={() => setMode('access')} />
          <span>
            Anyone with the link can view{!canGrant && <span className="dim"> · admins only for now</span>}
            <span className="hint">A read-only share link ({SHARE_DAYS} days, revocable on /admin): whoever opens it browses the site without signing in. Its preview is detailed too.</span>
          </span>
        </label>
        <div className="share-actions">
          <button type="button" className="btn primary" disabled={status.state === 'busy'} onClick={() => void share(mode)}>
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
