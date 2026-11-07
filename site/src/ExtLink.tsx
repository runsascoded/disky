import { FaArrowUpRightFromSquare } from 'react-icons/fa6'
import type { ExternalLink } from './stores'
import { Tooltip } from './Tooltip'

/** The external-link icon beside a path's name (the deepest crumb, an opened object): its page at the provider
 *  (`Store.externalLink`), in a new tab. */
export function ExtLink({ link }: { link?: ExternalLink }) {
  if (!link) return null
  return (
    <Tooltip content={<>Open {link.label}</>}>
      <a className="ext-link" href={link.href} target="_blank" rel="noreferrer" aria-label={`Open ${link.label}`}><FaArrowUpRightFromSquare aria-hidden /></a>
    </Tooltip>
  )
}
