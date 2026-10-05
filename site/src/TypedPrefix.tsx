import { useEffect, useState } from 'react'
import { OwnerControls } from './OwnerControls'
import type { OwnerIndex } from './owners'
import { useStore } from './store'
import { prefixPattern } from './prefixPattern'

// Assign a prefix by typing it — any depth, even below the treemap's fold
// floor (where no cell or table row exists to assign from). Opens from the ☰ menu.

export function TypedPrefixModal({ idx, onClose }: { idx: OwnerIndex; onClose: () => void }) {
  const store = useStore()
  const [typed, setTyped] = useState('')
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const t = typed.trim()
  const prefix = t ? (t.endsWith('/') ? t : t + '/') : ''
  const valid = prefixPattern(store).test(prefix)
  const example = `${store.scheme}${store.buckets[0] ?? '<bucket>'}/path/`
  return (
    <div className="token-backdrop" onClick={onClose}>
      <div className="token-modal typed-modal" role="dialog" aria-label="Assign a typed prefix" onClick={e => e.stopPropagation()}>
        <div className="token-head">
          <strong>Assign a typed prefix</strong>
          <button type="button" className="token-x" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <p className="sub">Any depth — even below the treemap’s fold floor, where there’s no cell or row to assign from.</p>
        <div className="typed-path">
          <input
            autoFocus
            value={typed}
            onChange={e => setTyped(e.target.value)}
            placeholder={example}
            size={56}
            spellCheck={false}
          />
          {t !== '' && !valid && <span className="err">need {store.scheme}&lt;bucket&gt;/path/, in one of this store’s buckets</span>}
        </div>
        {valid && <OwnerControls uri={prefix.slice(0, -1)} idx={idx} />}
      </div>
    </div>
  )
}
