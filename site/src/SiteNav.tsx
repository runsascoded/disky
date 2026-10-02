import { Explain } from './Help'
import {
  FloatingFocusManager,
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  shift,
  useClick,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from '@floating-ui/react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { DEFAULT_STORE, STORES } from './stores'
import { useStore } from './store'
import { FaGithub } from 'react-icons/fa'
import { MdMenu } from 'react-icons/md'
import { Link, useLocation } from 'react-router-dom'
import { AboutModal } from './About'
import { offerAppLink, openInApp } from './appLink'
import { Avatar } from './Avatar'
import { AUTH_MODE, signInUrl, useCanAssign, useIdent, useSignOut } from './auth'
import { useRegistry } from './identities'
import { REPO_URL } from './SiteKbd'
import { useMyUser, useUserEmails } from './owners'
import TokenModal from './TokenModal'
import { UserCard, ghHandle, shortName } from './UserChip'
import { useUnits } from './units'
import { Tooltip } from './Tooltip'


// The one bar every page wears, pinned to the viewport top: the site nav
// folded into a ☰ menu (far left), the page's own scope/controls across the
// middle, and the signed-in identity as an avatar menu (far right — units,
// token, sign-out live in it). On the home page the middle IS the page scope
// (scan, diff window, color axis, owner axis, name filter), so every
// section below reads against a visible statement of what it's scoped to,
// and the bar looks the same parked at the top as it does stuck mid-page.
//
// The bar's rendered height is published as `--topbar-h` on <html>, so
// anchored sections park just under it (`scroll-margin-top`) however many
// rows the controls wrap onto.
export const TOPBAR_VAR = '--topbar-h'
/** The bar's current height (px) — where anchored sections park. */
export const topbarH = (): number =>
  parseFloat(getComputedStyle(document.documentElement).getPropertyValue(TOPBAR_VAR)) || 48

export function SiteNav({ children, menu, crumbs }: {
  /** Page controls for the middle of the bar. */
  children?: ReactNode
  /** Page-specific entries appended to the ☰ menu (after the site links). */
  menu?: MenuEntry[]
  /** Where the page is (the drilled path): the first row, beside the brand;
   * the controls then take a row of their own under it. */
  crumbs?: ReactNode
}) {
  const ref = useRef<HTMLDivElement>(null)
  const row2Ref = useRef<HTMLDivElement | null>(null)
  // `--topbar-h` = the sticky bar's height (row 1 when the controls overlay);
  // `--tb-row2-h` = the controls' own height, so the flow spacer under the bar
  // reserves exactly that at the top of the page — where the overlay would
  // otherwise sit over the first slice of content. Measured in a layout effect
  // (synchronous, post-layout, so no zero-on-mount read) after every render,
  // plus on viewport resize (which wraps the controls to a different height).
  const measure = () => {
    const el = ref.current
    const root = document.documentElement
    if (el) root.style.setProperty(TOPBAR_VAR, `${el.offsetHeight}px`)
    const r2 = row2Ref.current
    root.style.setProperty('--tb-row2-h', r2 ? `${r2.offsetHeight}px` : '0px')
  }
  useLayoutEffect(measure)
  useEffect(() => {
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])
  // The crumbs scroll horizontally when they don't fit (a phone), and snap
  // to their END on every path change so the basename — the one segment the
  // reader needs — is what shows, not `marin GCS/marin-us-…`.
  const crumbsRef = useRef<HTMLDivElement | null>(null)
  const { pathname } = useLocation()
  useLayoutEffect(() => {
    const el = crumbsRef.current
    if (el) el.scrollLeft = el.scrollWidth
  }, [pathname])
  // The path row stays pinned; on a phone the controls row is an OVERLAY (out of
  // page flow — CSS below, gated to a narrow viewport) that auto-hides on scroll
  // down and returns on scroll up, like a native toolbar. Out of flow is the
  // whole point: toggling it moves no content and can't perturb `window.scrollY`
  // (an in-flow control row sits in the sticky bar above the viewport, so
  // collapsing it drops scrollY by its own height and a scroll-driven fold then
  // reads its own change and loops). So we can just watch scroll DIRECTION.
  const hasControls = !!(crumbs && children)
  const [hidden, setHidden] = useState(false)
  useEffect(() => {
    if (!hasControls) { setHidden(false); return }
    let lastY = window.scrollY
    let raf = 0
    const update = () => {
      raf = 0
      const y = Math.max(0, window.scrollY)
      if (y <= 4) { setHidden(false); lastY = y; return }   // always shown at the top
      const dy = y - lastY
      if (dy > 10) { setHidden(true); lastY = y }            // scrolled down → hide
      else if (dy < -10) { setHidden(false); lastY = y }     // scrolled up → show
      // small moves in the dead band leave lastY put, so a slow drag accumulates
    }
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(update) }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => { window.removeEventListener('scroll', onScroll); if (raf) cancelAnimationFrame(raf) }
  }, [hasControls])
  return (
    <>
    <div className={'topbar' + (hidden ? ' ctrl-hidden' : '')} ref={ref}>
      <div className="tb-row">
        <NavMenu extra={menu} />
        {crumbs ? <div className="tb-crumbs" ref={crumbsRef}>{crumbs}</div> : <div className="tb-mid">{children}</div>}
        <UserMenu />
      </div>
      {hasControls && <div className="tb-row tb-row2" ref={row2Ref}><div className="tb-mid">{children}</div></div>}
    </div>
    {/* A flow spacer the height of the controls: at the top it holds their
        place under the overlay so nothing is covered; once scrolled it rides up
        off-screen, so hiding the overlay leaves no gap. Zero on desktop. */}
    {hasControls && <div className="tb-row2-spacer" aria-hidden />}
    </>
  )
}

export interface MenuEntry {
  key: string
  label: ReactNode
  onClick: () => void
}

/** A click-toggled floating menu anchored to its trigger button. */
function useMenu(placement: 'bottom-start' | 'bottom-end') {
  const [open, setOpen] = useState(false)
  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    placement,
    middleware: [offset(6), flip(), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  })
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useClick(context),
    useDismiss(context),
    useRole(context, { role: 'menu' }),
  ])
  return { open, setOpen, refs, floatingStyles, context, getReferenceProps, getFloatingProps }
}

function NavMenu({ extra }: { extra?: MenuEntry[] }) {
  const { pathname } = useLocation()
  const canAssign = useCanAssign()
  const [aboutOpen, setAboutOpen] = useState(false)
  const m = useMenu('bottom-start')
  // The subtree's store: its own map (`/meta`), and only the affordances it
  // has (ownership, staging are the primary's).
  const store = useStore()
  // The map is "here" at the store's root only (a drilled path is a place of
  // its own); every other page, on it or under it.
  const here = (to: string) => (to === store.path ? pathname === to : pathname === to || pathname.startsWith(to + '/'))
  const link = (to: string, label: string) => (
    <Link key={to} role="menuitem" className="mi" to={to} aria-current={here(to) ? 'page' : undefined} onClick={() => m.setOpen(false)}>
      {label}
    </Link>
  )
  return (
    <>
      {aboutOpen && <AboutModal onClose={() => setAboutOpen(false)} />}
      <button type="button" className="tb-menu-btn" ref={m.refs.setReference} {...m.getReferenceProps()} aria-label="Site menu" title="Site menu">
        <MdMenu aria-hidden />
      </button>
      {m.open && (
        <FloatingPortal>
          <FloatingFocusManager context={m.context} modal={false}>
            <div className="menu-pop" ref={m.refs.setFloating} style={m.floatingStyles} {...m.getFloatingProps()}>
              {link(store.path, 'Map')}
              {canAssign && store.owners && link('/users', 'Users')}
              {canAssign && store.owners && link('/assignments', 'Assignments')}
              {store.staging && link('/staged', 'Staged')}
              {/* The store switcher — only a multi-store deploy has one
                  (specs/multi-store.md phase 2): each configured store at its
                  own path, the current one marked. */}
              {STORES.length > 1 && (
                <>
                  <hr />
                  {STORES.map(s => (
                    <Link key={`store:${s.key}`} role="menuitem" className="mi store-link" to={s.path} aria-current={s.key === store.key ? 'true' : undefined} onClick={() => m.setOpen(false)}>
                      {s.label} <span className="dim">{s.path}</span>
                    </Link>
                  ))}
                </>
              )}
              <hr />
              <button type="button" role="menuitem" className="mi" onClick={() => { m.setOpen(false); setAboutOpen(true) }}>About — the data, axes &amp; colors</button>
              {extra?.map(e => (
                <button key={e.key} type="button" role="menuitem" className="mi" onClick={() => { m.setOpen(false); e.onClick() }}>{e.label}</button>
              ))}
              <hr />
              {DEFAULT_STORE.peer && <a role="menuitem" className="mi" href={DEFAULT_STORE.peer.href} target="_blank" rel="noreferrer">{DEFAULT_STORE.peer.label} ↗</a>}
              <a role="menuitem" className="mi" href={REPO_URL} target="_blank" rel="noreferrer"><FaGithub aria-hidden /> Source on GitHub ↗</a>
            </div>
          </FloatingFocusManager>
        </FloatingPortal>
      )}
    </>
  )
}

function UserMenu() {
  const ident = useIdent()
  const canAssign = useCanAssign()
  const signOut = useSignOut()
  // The email → user map exists only on an attribution store.
  const ownersOn = !!ident && DEFAULT_STORE.owners
  const myUser = useMyUser(ident?.email, ownersOn)
  const emails = useUserEmails(ownersOn)
  const [tokenOpen, setTokenOpen] = useState(false)
  const [appHint, setAppHint] = useState<string | null>(null)
  const { units, suffixB, toggleUnits, toggleSuffixB } = useUnits()
  const m = useMenu('bottom-end')
  useEffect(() => {
    if (!appHint) return
    const t = setTimeout(() => setAppHint(null), 8000)
    return () => clearTimeout(t)
  }, [appHint])
  // "Open in disky" (specs/app-link.md): hand this session to the app. An
  // unregistered `disky://` scheme navigates nowhere, silently; if the page
  // still has focus a moment later, say why.
  const openApp = () => {
    m.setOpen(false)
    openInApp().then(
      () => setTimeout(() => {
        if (document.visibilityState === 'visible' && document.hasFocus()) setAppHint('Nothing happened? Install the disky app.')
      }, 1500),
      (e: Error) => setAppHint(`Couldn't open disky: ${e.message}`),
    )
  }
  // Public deploys have no auth — no sign-in affordance.
  if (!ident) return AUTH_MODE === 'public' ? null : <a className="tb-signin" href={signInUrl()}>sign in</a>
  const who = myUser ?? ident.email
  // A guest (share-link) session shows its own subject — the name + avatar the
  // admin minted it with — not the owner-registry lookup (which an external
  // guest isn't in, so it'd fall back to an email-derived initial + "ping Ryan").
  const guest = ident.guest
  const dispName = guest ? (ident.name ?? ident.email) : shortName(who)
  // Offered to a signed-in session on a desktop Mac. A guest link can't (the
  // server refuses a grant minting a grant).
  const showAppLink = !guest && offerAppLink(navigator, window)
  return (
    <>
      {tokenOpen && <TokenModal onClose={() => setTokenOpen(false)} />}
      {appHint && <div className="app-hint" role="status" onClick={() => setAppHint(null)}>{appHint}</div>}
      <button type="button" className="tb-avatar" ref={m.refs.setReference} {...m.getReferenceProps()} aria-label={`Signed in as ${dispName}`} title={dispName}>
        {guest || ident.avatar
          ? <Avatar src={ident.avatar} name={dispName} size={26} />
          : <Avatar github={ghHandle(who)} name={shortName(who)} size={26} />}
      </button>
      {m.open && (
        <FloatingPortal>
          <FloatingFocusManager context={m.context} modal={false}>
            <div className="menu-pop user-menu" ref={m.refs.setFloating} style={m.floatingStyles} {...m.getFloatingProps()}>
              {guest
                ? <GuestCard name={dispName} avatar={ident.avatar} email={ident.email} />
                : <UserCard who={who} avatar={ident.avatar} extra={<SessionLines email={ident.email} user={myUser} emails={emails} />} />}
              <hr />
              <Explain text="Byte units, site-wide: binary (TiB) ↔ decimal (TB)">
                <button type="button" role="menuitem" className="mi" onClick={() => toggleUnits()}>
                  Units: <b>{(units === 'iec' ? 'Ti' : 'T') + (suffixB ? 'B' : '')}</b> → {(units === 'iec' ? 'T' : 'Ti') + (suffixB ? 'B' : '')}
                </button>
              </Explain>
              <Explain text="Show or hide the trailing B (Ti vs TiB), site-wide">
                <button type="button" role="menuitem" className="mi" onClick={() => toggleSuffixB()}>
                  Trailing B: <b>{suffixB ? 'On' : 'Off'}</b> <span className="dim">({units === 'iec' ? 'Ti' : 'T'}{suffixB ? 'B' : ''})</span>
                </button>
              </Explain>
              {canAssign && (
                <button type="button" role="menuitem" className="mi" onClick={() => { m.setOpen(false); setTokenOpen(true) }}>
                  Agent / CLI Token…
                </button>
              )}
              {showAppLink && (
                <Explain text="Sign the disky macOS app in as you, with a single-use link (expires in a minute)">
                  <button type="button" role="menuitem" className="mi" onClick={openApp}>Open in disky</button>
                </Explain>
              )}
              <button type="button" role="menuitem" className="mi" onClick={signOut}>Log Out</button>
            </div>
          </FloatingFocusManager>
        </FloatingPortal>
      )}
    </>
  )
}

// A guest (share-link) session's card: the subject the admin minted — avatar +
// name + (if present) the bound email. No registry framing (storage-breakdown
// link, alias lines, "not mapped" warning) — a guest isn't an owner.
function GuestCard({ name, avatar, email }: { name: string; avatar?: string; email: string }) {
  return (
    <div className="user-card">
      <div className="uc-head">
        <Avatar src={avatar} name={name} size={38} />
        <div className="uc-id"><b>{name}</b></div>
      </div>
      {email.includes('@') && <div className="uc-sub">{email}</div>}
      <div className="uc-session"><div className="dim">guest share link</div></div>
    </div>
  )
}

// Session-specific lines appended to the identity card (the card itself —
// avatar, name, group, GitHub, storage link — is the shared <UserCard>):
// which sign-in email this session is, the user's other aliases and sign-in
// emails, or a warning when the email maps to no user.
function SessionLines({ email, user, emails }: { email: string; user: string | null; emails?: Record<string, string> }) {
  const reg = useRegistry()
  const aliases = user ? Object.keys(reg).filter(k => k !== user && reg[k].u === user) : []
  const others = user && emails ? Object.keys(emails).filter(e => emails[e] === user && e !== email.toLowerCase()) : []
  return (
    <div className="uc-session">
      <div>Signed in as <code>{email}</code></div>
      {user ? (
        <>
          {aliases.length > 0 && <div>aliases: {aliases.map(a => <code key={a}>{a}</code>)}</div>}
          {others.length > 0 && <div>also signs in as: {others.map(e => <code key={e}>{e}</code>)}</div>}
        </>
      ) : (
        <div className="uc-warn">Not mapped to a user in the identity registry — the “me” owner filter won't resolve; ping Ryan.</div>
      )}
    </div>
  )
}
