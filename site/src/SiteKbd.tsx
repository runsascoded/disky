import { FaGithub } from 'react-icons/fa'
import { MdBrightnessAuto, MdDarkMode, MdHelpOutline, MdLightMode } from 'react-icons/md'
import { useLocation, useNavigate } from 'react-router-dom'
import { Omnibar, ShortcutsModal, SpeedDial, useActions, type SpeedDialAction } from 'use-kbd'
import { SpeedDialTip } from './Tooltip'
import { useRegistry } from './identities'
import { useHelpPref } from './prefs'
import { useStore } from './store'
import { STORES } from './stores'
import { useTheme } from './theme'
import { useUnits } from './units'

export const REPO_URL = 'https://github.com/Open-Athena/marin-gcs-usage'
const CW_URL = 'https://cw-s3.oa.dev/'


/**
 * The keyboard/omnibar chrome every page shares: the lower-right SpeedDial
 * (GitHub · theme · shortcuts), ⌘K omnibar, `?` shortcuts modal, and the
 * actions that make sense anywhere — page links, user pages, theme, units.
 * Pages add their own actions with `useActions` (one registry: the
 * HotkeysProvider sits in Root) and can push extra SpeedDial buttons via
 * `extra`.
 */
export function SiteKbd({ extra = [], placeholder = 'Pages, users, actions…' }: {
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
  const PAGES: [string, string][] = [
    [store.path, 'Map (home)'],
    ['/users', 'Users — storage by owner'],
  ]
  useActions({
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
    'page:cw': { label: 'CoreWeave usage ↗ (cw-s3.oa.dev)', group: 'Pages', handler: () => window.open(CW_URL, '_blank', 'noreferrer') },
    'page:github': { label: 'Source on GitHub ↗', group: 'Pages', handler: () => window.open(REPO_URL, '_blank', 'noreferrer') },
    ...Object.fromEntries(
      USERS.filter(u => pathname !== `/user/${u}`).map(u => [
        `userpage:${u}`,
        { label: `${u} — storage breakdown (/user/${u})`, group: 'User pages', handler: () => navigate(`/user/${u}`) },
      ]),
    ),
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
