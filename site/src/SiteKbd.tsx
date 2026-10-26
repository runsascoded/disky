import { FaGithub } from 'react-icons/fa'
import { MdBrightnessAuto, MdDarkMode, MdHelpOutline, MdLightMode, MdLink } from 'react-icons/md'
import { useSyncExternalStore } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { queryStrategy } from 'use-prms'
import { Omnibar, ShortcutsModal, SpeedDial, useActions, type SpeedDialAction } from 'use-kbd'
import { SpeedDialTip } from './Tooltip'
import { useRegistry } from './identities'
import { hostPair, otherHostUrl, peerUrl, urlLabel, type Peer } from './hosts'
import { useHelpPref, useScrollSpyPref } from './prefs'
import { useStore } from './store'
import { DEFAULT_STORE, DUPS, STORES } from './stores'
import { useTheme } from './theme'
import { useUnits } from './units'
import { AUTH_MODE, useCanAssign, useCanStage, useIdent, useIsAdmin, useSignOut, useStores } from './auth'
import { pageAction } from './pageAction'
import { openDialog } from './dialogs'
import { useShare } from './sharePreview'

/** The source link (SpeedDial, omnibar, user menu): the deployment's
 * `REPO_URL` (wrangler `[vars]`, read at build time), else this repo. */
export const REPO_URL = import.meta.env.VITE_REPO_URL || 'https://github.com/runsascoded/disky'
const HOSTS = hostPair(import.meta.env.VITE_PROD_HOST, import.meta.env.VITE_DEV_HOST)
/** The other deployments: the family list less this one. */
export const PEERS: Peer[] = (STORES[0]?.peers ?? []).filter(p => p.prod !== import.meta.env.VITE_PROD_HOST)
/** This page on a peer, from the current location. */
export const peerHref = (p: Peer, href: string) => peerUrl(href, p, new URL(href).hostname === HOSTS?.dev)
/** The characters an omnibar row shows before clipping its label's end. */
const LABEL_MAX = 54
/** An omnibar label ending in a URL, the URL elided from the middle so the whole fits the row. */
const toUrl = (prefix: string, href: string) => prefix + urlLabel(href, LABEL_MAX - prefix.length)
/** The address bar's URL, live: use-prms writes params with `history.replaceState`, which the router never sees. */
const useHref = () => useSyncExternalStore(cb => queryStrategy.subscribe(cb), () => location.href)


/**
 * The keyboard/omnibar chrome every page shares: the lower-right SpeedDial
 * (GitHub · theme · shortcuts), ⌘K omnibar, `?` shortcuts modal, and the
 * actions that make sense anywhere — page links, user pages, theme, units.
 * Pages add their own actions with `useActions` (one registry: the
 * HotkeysProvider sits in Root) and can push extra SpeedDial buttons via
 * `extra`.
 */
export function SiteKbd({ extra = [], placeholder }: {
  extra?: SpeedDialAction[]
  placeholder?: string
}) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const href = useHref()
  const [theme, cycleTheme] = useTheme()
  const { units, suffixB, toggleUnits, toggleSuffixB } = useUnits()
  const [help, setHelp] = useHelpPref()
  const toggleHelp = () => setHelp(help === 'on' ? 'off' : 'on')
  const [spy, setSpy] = useScrollSpyPref()
  // Every distinct attribution user in the registry (aliases collapse onto `u`).
  const reg = useRegistry()
  const USERS = [...new Set(Object.values(reg).map(i => i.u))].sort()
  // Site-wide pages, in nav order — the omnibar's "Pages" group on every
  // route. The map is the subtree's store's (`/meta` on a secondary store);
  // the other configured stores follow as switches (a single-store build has
  // none).
  const store = useStore()
  placeholder ??= store.owners ? 'Pages, users, actions…' : 'Pages and actions…'
  const canShare = useCanStage()
  const canAssign = useCanAssign()
  const ident = useIdent()
  const isAdmin = useIsAdmin()
  const stores = useStores()
  const signOut = useSignOut()
  // The ☰ menu's pages, gated as it gates them.
  const PAGES: [string, string][] = [
    [store.path, 'Map (home)'],
    ...(canAssign && store.owners ? [['/users', 'Users — storage by owner'], ['/assignments', 'Assignments']] as [string, string][] : []),
    ...(store.staging ? [['/staged', 'Staged deletions']] as [string, string][] : []),
    ...(DUPS && store === DEFAULT_STORE ? [['/dups', 'Duplicates — copies across buckets']] as [string, string][] : []),
    ['/scans', 'Scans — job runs'],
    ['/health', 'Health — the stores\' state'],
    ...(isAdmin ? [['/admin', 'Admin'], ['/admin/db', 'Database'], ['/admin/sessions', 'Session logs'], ['/admin/activity', 'Activity — who did what'], ['/admin/usage', 'Cloudflare usage — cost and callers'], ['/admin/cost/gcp', 'GCP cost — storage and compute']] as [string, string][] : []),
    ['/privacy', 'Privacy policy'],
    ...(!ident && AUTH_MODE !== 'public' ? [['/signin', 'Sign in']] as [string, string][] : []),
  ]
  const { share, status: shareStatus } = useShare()
  useActions({
    ...(canShare ? {
      'share:preview': {
        label: shareStatus.state === 'copied' ? 'Copied: link with detailed preview' : shareStatus.state === 'error' ? `Couldn't copy: ${shareStatus.error}` : 'Copy link with detailed preview (link-preview card only; grants no access)',
        description: 'Copies this page\'s link with a token that makes its unfurl card in Slack, iMessage etc. show labels, sizes and owners. Opening the link still needs sign-in.',
        group: 'Share',
        handler: () => void share('preview'),
      },
    } : {}),
    'dialog:about': { label: 'About — the data, axes & colors', group: 'Site', handler: () => openDialog('about') },
    'dialog:params': { label: 'URL params — what this link sets', group: 'Site', keywords: ['query', 'parameters', 'link'], handler: () => openDialog('params') },
    ...(canShare ? { 'dialog:share': { label: 'Share…', group: 'Share', handler: () => openDialog('share') } } : {}),
    ...(ident && !ident.guest ? { 'dialog:profile': { label: 'Profile… (name + avatar)', group: 'Site', handler: () => openDialog('profile') } } : {}),
    ...(canAssign ? { 'dialog:token': { label: 'Agent / CLI token…', group: 'Site', handler: () => openDialog('token') } } : {}),
    ...(ident ? { 'auth:logout': { label: 'Log out', group: 'Site', handler: signOut } } : {}),
    'help:toggle': {
      label: `Help line: ${help} (toggle)`,
      group: 'View',
      defaultBindings: ['h'],
      handler: toggleHelp,
    },
    'spy:toggle': {
      label: `Section spy (?section= in the URL while scrolling): ${spy} (toggle)`,
      description: 'Names the section in view (tree-map, tbl, over-time, diff, mtime) in the URL as you scroll, so a copied link opens there and unfurls with that section\'s card.',
      group: 'View',
      keywords: ['section', 'spy', 'scroll', 'url', 'param'],
      handler: () => setSpy(spy === 'on' ? 'off' : 'on'),
    },
    ...Object.fromEntries(
      PAGES.map(([to, label]) => [
        `page:${to}`,
        { ...pageAction(to, label), handler: () => navigate(to) },
      ]),
    ),
    ...Object.fromEntries(
      stores.filter(s => s.key !== store.key).map(s => [
        `store:${s.key}`,
        { label: `${s.label} store (${s.path})`, group: 'Pages', handler: () => navigate(s.path) },
      ]),
    ),
    ...Object.fromEntries(
      PEERS.map(p => [
        `peer:${p.prod}`,
        { label: toUrl('This page on ', peerHref(p, href)), group: 'Pages', keywords: [p.label, p.prod.split('.')[0]], handler: () => location.assign(peerHref(p, location.href)) },
      ]),
    ),
    'page:github': { label: 'Source on GitHub ↗', group: 'Pages', handler: () => window.open(REPO_URL, '_blank', 'noreferrer') },
    ...Object.fromEntries(
      USERS.filter(u => pathname !== `/user/${u}`).map(u => [
        `userpage:${u}`,
        { label: `${u} — storage breakdown (/user/${u})`, group: 'User pages', handler: () => navigate(`/user/${u}`) },
      ]),
    ),
    ...(HOSTS ? {
      'host:toggle': {
        label: toUrl(`This page on ${location.hostname === HOSTS.dev ? 'prod' : 'dev'}: `, otherHostUrl(href, HOSTS)),
        group: 'Pages',
        defaultBindings: ['g d'],
        handler: () => location.assign(otherHostUrl(location.href, HOSTS)),
      },
      'host:toggle-tab': {
        label: toUrl(`This page on ${location.hostname === HOSTS.dev ? 'prod' : 'dev'} ↗: `, otherHostUrl(href, HOSTS)),
        group: 'Pages',
        defaultBindings: ['g shift+d'],
        handler: () => { window.open(otherHostUrl(location.href, HOSTS), '_blank', 'noopener') },
      },
    } : {}),
    'theme:cycle': {
      label: `Theme: ${theme} (cycle)`,
      group: 'View',
      defaultBindings: ['shift+d'],
      handler: cycleTheme,
    },
    'units:toggle': {
      label: `Byte units: ${units === 'si' ? 'SI (TB) → IEC (TiB)' : 'IEC (TiB) → SI (TB)'}`,
      group: 'View',
      defaultBindings: ['i'],
      handler: toggleUnits,
    },
    'units:suffix': {
      label: `Unit suffix: ${suffixB ? 'TiB/TB → Ti/T (drop B)' : 'Ti/T → TiB/TB (show B)'}`,
      group: 'View',
      defaultBindings: pathname.startsWith('/runs/') || pathname === '/staged' ? [] : ['B'],
      handler: toggleSuffixB,
    },
  })
  return (
    <>
      <SpeedDial TooltipRenderer={SpeedDialTip} actions={[
        { key: 'github', label: 'GitHub', icon: <FaGithub />, href: REPO_URL },
        ...extra,
        { key: 'help', label: `Help line: ${help}`, icon: <MdHelpOutline />, onClick: toggleHelp },
        { key: 'params', label: 'URL params', icon: <MdLink />, onClick: () => openDialog('params') },
        {
          key: 'theme',
          label: `Theme: ${theme}`,
          icon: theme === 'light' ? <MdLightMode /> : theme === 'dark' ? <MdDarkMode /> : <MdBrightnessAuto />,
          onClick: cycleTheme,
        },
      ]} />
      <Omnibar placeholder={placeholder} maxResults={15} />
      <ShortcutsModal />
    </>
  )
}
