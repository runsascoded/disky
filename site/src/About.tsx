import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useStore } from './store'

// The onboarding copy that used to sit above the map as two folds. It lives
// behind the ☰ menu now — the home page's rows above the map are the scope
// bar and the map.
export function AboutModal({ onClose }: { onClose: () => void }) {
  const store = useStore()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="token-backdrop" onClick={onClose}>
      <div className="token-modal about-modal" role="dialog" aria-label="About" onClick={e => e.stopPropagation()}>
        <div className="token-head">
          <strong>{store.title}</strong>
          <button type="button" className="token-x" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <h3>The data</h3>
        {store.about ?? <p>{store.desc}</p>}
        {store.staging && <>
        <h3>Staged deletion</h3>
        <p>
          The trash icon on a table row stages that prefix (select several rows to stage them together, with
          a note). Staged prefixes collect on <Link to="/staged" onClick={onClose}>/staged</Link>, where an
          admin reviews and runs them; deletes are recoverable for a while. You can withdraw anything you
          staged until it runs.
        </p>
        </>}
        <h3>The bar</h3>
        <p>
          The top bar sets the scope for the whole page: the <b>scan</b> (and the diff’s start, while the Diff
          or size chart is in view), the drilled <b>path</b>,{store.owners && <> the <b>owner</b>,</>} and a
          {' '}<b>path filter</b>. “Color by” recolors the map by{store.owners && <> <b>read</b> (last-read
          recency), owning <b>user</b>,</>} <b>written</b> (older → newer) or top-level <b>tree</b>. Hover a
          cell for its makeup; <kbd>⌘K</kbd> finds pages and actions{store.owners && <>, or a user’s breakdown
          (all of them at <Link to="/users" onClick={onClose}>/users</Link>)</>}.
        </p>
      </div>
    </div>
  )
}
