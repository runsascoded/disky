import type { ReactNode } from 'react'
import { SectionLink } from 'use-prms/sections'

/** A section heading's text as a link to the section: a click parks it where a deep link would and names it in the
 * URL; a copied or middle-clicked link opens there. */
export function SectionAnchor({ id, children }: { id: string; children: ReactNode }) {
  return <SectionLink id={id} className="sec-anchor">{children}</SectionLink>
}
