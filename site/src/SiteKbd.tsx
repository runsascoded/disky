import { FaGithub } from 'react-icons/fa'
import { MdBrightnessAuto, MdDarkMode, MdHelpOutline, MdLightMode } from 'react-icons/md'
import { useLocation, useNavigate } from 'react-router-dom'
import { Omnibar, ShortcutsModal, SpeedDial, useActions, type SpeedDialAction } from 'use-kbd'
import { SpeedDialTip } from './Tooltip'
import { useRegistry } from './identities'
import { hostPair, otherHostUrl } from './hosts'
import { useHelpPref } from './prefs'
import { useStore } from './store'
import { STORES } from './stores'
import { useTheme } from './theme'
import { useUnits } from './units'
import { useCanAssign, useCanStage, useIdent, useSignOut } from './auth'
import { openDialog } from './dialogs'
import { useShare } from './sharePreview'

/** The source link (SpeedDial, omnibar, user menu): the deployment's
 * `REPO_URL` (wrangler `[vars]`, read at build time), else this repo. */
export const REPO_URL = import.meta.env.VITE_REPO_URL || 'https://github.com/runsascoded/disky'
const HOSTS = hostPair(import.meta.env.VITE_PROD_HOST, import.meta.env.VITE_DEV_HOST)


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
  const [theme, cycleTheme] = useTheme()
  const { units, suffixB, toggleUnits, toggleSuffixB } = useUnits()
  const [help, setHelp] = useHelpPref()
  const toggleHelp = () => setHelp(help === 'on' ? 'off' : 'on')
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
  const signOut = useSignOut()
  // The ☰ menu's pages, gated as it gates them.
  const PAGES: [string, string][] = [
    [store.path, 'Map (home)'],
    ...(canAssign && store.owners ? [['/users', 'Users — storage by owner'], ['/assignments', 'Assignments']] as [string, string][] : []),
    ...(store.staging ? [['/staged', 'Staged deletions']] as [string, string][] : []),
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
    ...Object.fromEntries(
      PAGES.filter(([to]) => to !== pathname).map(([to, label]) => [
        `page:${to}`,
        { label, group: 'Pages', handler: () => navigate(to) },
      ]),
    ),
    ...Object.fromEntries(
      STORES.filter(s => s.key !== store.key).map(s => [
        `store:${s.key}`,
        { label: `${s.label} store (${s.path})`, group: 'Pages', handler: () => navigate(s.path) },
      ]),
    ),
    ...(store.peer ? {
      'page:peer': { label: `${store.peer.label} ↗ (${new URL(store.peer.href).host})`, group: 'Pages', handler: () => window.open(store.peer!.href, '_blank', 'noreferrer') },
    } : {}),
    'page:github': { label: 'Source on GitHub ↗', group: 'Pages', handler: () => window.open(REPO_URL, '_blank', 'noreferrer') },
    ...Object.fromEntries(
      USERS.filter(u => pathname !== `/user/${u}`).map(u => [
        `userpage:${u}`,
        { label: `${u} — storage breakdown (/user/${u})`, group: 'User pages', handler: () => navigate(`/user/${u}`) },
      ]),
    ),
    ...(HOSTS ? {
      'host:toggle': {
        label: location.hostname === HOSTS.dev ? `This page on prod (${HOSTS.prod})` : `This page on dev (${HOSTS.dev})`,
        group: 'Pages',
        defaultBindings: ['g d'],
        handler: () => location.assign(otherHostUrl(location.href, HOSTS)),
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
      defaultBindings: ['B'],
      handler: toggleSuffixB,
    },
  })
  return (
    <>
      <SpeedDial TooltipRenderer={SpeedDialTip} actions={[
        { key: 'github', label: 'GitHub', icon: <FaGithub />, href: REPO_URL },
        ...extra,
        { key: 'help', label: `Help line: ${help}`, icon: <MdHelpOutline />, onClick: toggleHelp },
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
