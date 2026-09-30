import { useEffect } from 'react'
import { SITE } from './title'
import { Link } from 'react-router-dom'

// The onboarding copy that used to sit above the map as two folds. It lives
// behind the ☰ menu now — the home page's rows above the map are the scope
// bar and the map.
export function AboutModal({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="token-backdrop" onClick={onClose}>
      <div className="token-modal about-modal" role="dialog" aria-label="About" onClick={e => e.stopPropagation()}>
        <div className="token-head">
          <strong>{SITE}</strong>
          <button type="button" className="token-x" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <h3>The data</h3>
        <p>
          Storage across the six <code>marin-*</code> GCS buckets — a full per-object listing
          (deduped), snapshotted daily by the{' '}
          <a href="https://github.com/Open-Athena/marin-gcs-usage/blob/gcs/AGENTS.md#data-flow" target="_blank" rel="noreferrer"><code>marin-gcs-usage</code></a>{' '}
          pipeline, which also ingests the buckets’ access logs and joins ownership onto every prefix
          (W&amp;B run/config matching, executor sidecars, manual curation). Owner assignments apply live on
          top of the latest snapshot.
        </p>
        <h3>Staged deletion</h3>
        <p>
          Nothing is deleted by inaction — there is no deadline. Deleting is opt-in: the trash icon on a row
          of the table under the map <b>stages</b> that prefix (select several rows to stage them together,
          with a note). Staged prefixes collect on <Link to="/staged" onClick={onClose}>/staged</Link>, where
          an admin reviews them, dry-runs, and dispatches a real run that deletes recoverably (a versioned
          delete marker or a soft-delete window, undoable for a while). Anyone who staged something can take
          it back until it runs.
        </p>
        <h3>The bar</h3>
        <p>
          Everything on the page reads the same scope, stated in the top bar: the <b>scan</b> (and, while
          the Diff or size chart is in view, the diff window’s start), the drilled <b>path</b>, the{' '}
          <b>owner</b> axis (owned / unowned, or one person), and a <b>path filter</b>. “Color by” recolors
          the map: <b>read</b> (last-read recency — never-read bytes are the best deletion candidates;
          access logging began 8/13, so “never read” means “not since then”), owning <b>user</b>,{' '}
          <b>written</b> (older→newer), or top-level <b>tree</b>. Hover a cell for its makeup and top
          users, <kbd>⌘K</kbd> to jump to a user or page, or see the per-user breakdown at{' '}
          <Link to="/users" onClick={onClose}>/users</Link>.
        </p>
      </div>
    </div>
  )
}
