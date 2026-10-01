import {
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  safePolygon,
  shift,
  useDismiss,
  useFloating,
  useFocus,
  useHover,
  useInteractions,
  useRole,
} from '@floating-ui/react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Avatar } from './Avatar'
import { registry } from './identities'
import { canonIn, defaultName } from './identityRegistry'

// Canonical display for an actor. Assignments carry `who` as an email or a raw
// id; canonicalize it, then read the deployment's registry (short name + GitHub
// avatar). Everything shows the *short name*, never the raw email — with a
// GitHub-style hover card (interactive: you can move into it and click the link).

// Canonical id: sanitize to a handle, then follow the registry — alias keys
// (email local parts, short handles) resolve to their canonical rec's id.
/** Shortest registry key that canonicalizes to `id` — for golfed URLs
 * (`?u=rw`, `?u=gonzalo`). Falls back to the id itself. */
export const shortUserKey = (id: string): string => {
  // The shortest alias that is a *prefix* of the canonical id (`kaiyue` for
  // `kaiyue-wen`) — a first name reads as the person in a URL; an unrelated
  // handle-derived alias (`when`) reads as a word.
  let best = id
  for (const [k, rec] of Object.entries(registry())) {
    if (rec.u === id && id.startsWith(k) && k.length < best.length) best = k
  }
  return best
}

export const canonId = (who: string): string => canonIn(registry(), who)

/** Short display name — registry `name`, else the capitalized first id segment. */
export const shortName = (who: string): string => {
  const id = canonId(who)
  const rec = registry()[id]
  if (rec) return rec.name
  if (id) return defaultName(id)
  return who
}

/** Explicit GitHub handle for the real avatar, or undefined (never guessed). */
export const ghHandle = (who: string): string | undefined => registry()[canonId(who)]?.github

/** All known users (canonical id + short name), sorted by name — for pickers. */
export const allUsers = (): { id: string; name: string }[] =>
  Object.entries(registry())
    .map(([id, rec]) => ({ id, name: rec.name }))
    .sort((a, b) => a.name.localeCompare(b.name))

/** The GitHub-style identity card shown on hover — avatar, name, links. */
export function UserCard({ who, avatar, extra }: { who: string; avatar?: string; extra?: React.ReactNode }) {
  const id = canonId(who)
  const name = shortName(who)
  const gh = ghHandle(who)
  const showsRaw = who !== name && who !== id
  return (
    <div className="user-card">
      <div className="uc-head">
        <Avatar src={avatar} github={gh} name={name} size={38} />
        <div className="uc-id">
          <b>{name}</b>
        </div>
      </div>
      {showsRaw && <div className="uc-sub">{who}</div>}
      {gh && (
        <a className="uc-link" href={`https://github.com/${gh}`} target="_blank" rel="noreferrer">
          @{gh} on GitHub
        </a>
      )}
      {id && <Link className="uc-link" to={`/user/${id}`}>Storage Breakdown →</Link>}
      {extra}
    </div>
  )
}

/** Avatar + short name, with an interactive hover card. The default user display. */
export function UserChip({ who, size = 18, extra, before }: {
  who: string
  size?: number
  extra?: React.ReactNode
  /** Rendered inside the hover target, before the avatar (e.g. a group glyph
   * — so hovering it opens this card too, not a dead zone). */
  before?: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    // Off to the side, not above: an above-placed card covers the neighboring
    // rows/chips you're about to hover next (flip handles tight right edges).
    placement: 'right-start',
    middleware: [offset(6), flip(), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  })
  const { getReferenceProps, getFloatingProps } = useInteractions([
    // safePolygon: keep the card open while the cursor travels into it, so its
    // links stay clickable (the whole point — a mouse-following tip can't be).
    // Small close delay: rows render flush, so scanning down a list crosses
    // chip borders constantly — without it the card flickers per row.
    useHover(context, { delay: { open: 120, close: 80 }, handleClose: safePolygon() }),
    useFocus(context),
    useDismiss(context),
    useRole(context, { role: 'label' }),
  ])
  return (
    <>
      <span className="user-chip" ref={refs.setReference} tabIndex={0} {...getReferenceProps()}>
        {before}
        <Avatar github={ghHandle(who)} name={shortName(who)} size={size} />
        {shortName(who)}
      </span>
      {open && (
        <FloatingPortal>
          <div className="tooltip-content user-card-pop" ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()}>
            <UserCard who={who} extra={extra} />
          </div>
        </FloatingPortal>
      )}
    </>
  )
}
