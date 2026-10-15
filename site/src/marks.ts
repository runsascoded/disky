// A store's tab icon: its `mark` (a `public/icons/` key, `scripts/marks/build.mjs`) while the route is in that store.
// The deployment's mark (`MARK`) is built into `index.html` (`vite.config.ts`), so a first paint and an unfurl have
// it; a store with a mark of its own swaps the links in when its routes open, and back on leaving them.
import { useEffect } from 'react'
import type { Store } from './stores'

/** The head links a mark sets, as `vite.config.ts`'s `markIcons` writes them. */
export interface MarkLink { rel: 'icon' | 'apple-touch-icon'; type?: string; sizes?: string; href: string }

export const markLinks = (mark: string): MarkLink[] => [
  { rel: 'icon', type: 'image/svg+xml', href: `/icons/${mark}.svg` },
  { rel: 'icon', sizes: '32x32', href: `/icons/${mark}.ico` },
  { rel: 'apple-touch-icon', href: `/icons/${mark}-180.png` },
]

const selector = (l: Omit<MarkLink, 'href'>): string =>
  `link[rel="${l.rel}"]${l.type ? `[type="${l.type}"]` : ''}${l.sizes ? `[sizes="${l.sizes}"]` : ''}`

/** Point `doc`'s icon links at `mark` (null: back to the built ones). A link the build didn't write (a deployment
 *  without `MARK` has only its label's svg) is added for the mark and removed again after. */
export function applyMark(doc: Document, mark: string | null): void {
  for (const l of markLinks(mark ?? '')) {
    let el = doc.head.querySelector<HTMLLinkElement>(selector(l))
    if (!el) {
      if (!mark) continue
      el = doc.createElement('link')
      el.rel = l.rel
      if (l.type) el.type = l.type
      if (l.sizes) el.setAttribute('sizes', l.sizes)
      el.dataset.markAdded = ''
      doc.head.appendChild(el)
    }
    if (el.dataset.builtHref === undefined && el.dataset.markAdded === undefined) el.dataset.builtHref = el.getAttribute('href') ?? ''
    if (mark) el.href = l.href
    else if (el.dataset.markAdded !== undefined) el.remove()
    else el.setAttribute('href', el.dataset.builtHref!)
  }
}

/** The tab icon follows the route's store: its own `mark`, else the deployment's. */
export function useStoreMark(store: Pick<Store, 'mark'>): void {
  const mark = store.mark ?? null
  useEffect(() => {
    applyMark(document, mark)
    return () => applyMark(document, null)
  }, [mark])
}
