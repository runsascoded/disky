import type { ReactNode } from 'react'
import { jumpToSection } from './hashSpy'

/** A section heading's text as a link to the section (`#id`): a click parks it where a deep link would and names it in
 * the hash; a copied or middle-clicked link opens there. */
export function SectionAnchor({ id, children }: { id: string; children: ReactNode }) {
  return (
    <a className="sec-anchor" href={`#${id}`} onClick={e => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return
      e.preventDefault()
      jumpToSection(id)
    }}>{children}</a>
  )
}
