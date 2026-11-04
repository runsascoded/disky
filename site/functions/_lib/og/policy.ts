import type { CardData } from './card.js'
import { utcMinute } from './run.js'

/** Bump whenever rendering or public-disclosure rules change. */
export const RENDER_VERSION = '8'
export type AnonymousPolicy = 'generic' | 'shape' | 'full'
export interface PolicyEnv { OG_ANONYMOUS?: string; ROOT_LABEL?: string; AUTH_MODE?: string }

/** Private deployments require explicit shape/totals opt-in. Unknown settings fail closed. `full`: an anonymous
 * unfurl gets the full card (labelled tiles, owners), for a deployment whose sizes and paths aren't secret behind its
 * sign-in (cw-s3: the site is gated, its link previews are not). */
export const anonymousPolicy = (env: PolicyEnv): AnonymousPolicy =>
  // A secondary store's is the deployment's unless its `vars` set one (`stores.ts` `INHERITED_VARS`).
  env.OG_ANONYMOUS === 'full' ? 'full'
    : env.OG_ANONYMOUS === 'shape' || (env.OG_ANONYMOUS == null && env.AUTH_MODE === 'public') ? 'shape' : 'generic'

/** The tier a card is drawn at: an anonymous fetch under the `full` policy draws the full card. */
export const cardTier = (tier: 'anon' | 'full', policy: AnonymousPolicy): 'anon' | 'full' =>
  tier === 'anon' && policy === 'full' ? 'full' : tier
export const genericImagePath = `/og/map.png?policy=generic&r=${RENDER_VERSION}`

/** A page's name and description in a link preview: fixed text a public URL already gives away, no data read (no
 *  counts, no `q`), each `<title> · <site>` unless its URL names it better (`genericMeta`'s `name`) and with its own
 *  generic card. `/staged`, `/users`, `/user/:id`, `/assignments` read only under the `generic` policy, whose
 *  anonymous fetches never reach their Functions, so they may say "Sign in"; the pages below them have no card kind
 *  and read so under every policy, so they don't. A map page stays the one generic card: its URL can hold any path. */
interface NamedPage { title: string; desc: string; empty?: string }
const NAMED_PAGES: Record<string, NamedPage> = {
  staged: { title: 'Staged for deletion', desc: 'Prefixes staged for deletion, for review. Sign in to see them.', empty: 'Sign in to review' },
  users: { title: 'Users', desc: 'Who owns what across the scanned buckets. Sign in to see it.', empty: 'Sign in to see who owns what' },
  user: { title: 'User', desc: 'One user’s storage: what they own, and what was assigned to them. Sign in to see it.', empty: 'Sign in to see their storage' },
  assignments: { title: 'Assignments', desc: 'Who assigned what to whom. Sign in to see it.', empty: 'Sign in to see assignments' },
  scans: { title: 'Scan runs', desc: 'What each scan job did: status, phases and timing.' },
  scan: { title: 'Scan run', desc: 'One scan job: status, phases and timing.' },
  dups: { title: 'Duplicates', desc: 'Candidates for the same data held in more than one place.' },
  names: { title: 'Name search', desc: 'Find paths by name across the scanned buckets.' },
  health: { title: 'Health', desc: 'Whether the scans and indexes behind this site are current.' },
  files: { title: 'Object', desc: 'A link to one object in storage.' },
  admin: { title: 'Admin', desc: 'Site administration.' },
  privacy: { title: 'Privacy', desc: 'What this site collects, what it doesn’t, and how to have it removed.' },
  signin: { title: 'Sign in', desc: 'Sign in to explore storage.' },
}
const namedPage = (kind?: string): NamedPage | undefined => kind && Object.hasOwn(NAMED_PAGES, kind) ? NAMED_PAGES[kind] : undefined

/** A kind with a generic card of its own: one no view or credential changes (`serveCard`). */
export const isNamedPage = (kind: string): boolean => namedPage(kind) !== undefined

/** A kind's generic image: its own URL (and so cache key) only for a named page. */
export const genericImageOf = (kind?: string): string =>
  namedPage(kind) ? `/og/${kind}.png?policy=generic&r=${RENDER_VERSION}` : genericImagePath

export const genericCard = (env: PolicyEnv, kind?: string): CardData => {
  const named = namedPage(kind)
  return {
    tier: 'anon', site: env.ROOT_LABEL ?? 'storage', title: named?.title ?? 'Private storage dashboard',
    subtitle: '', total: '', tiles: [], empty: named ? named.empty ?? named.desc : 'Sign in to explore storage',
  }
}

/** The longest URL text a title echoes; a longer one keeps its tail, the most specific part. */
const MAX_NAME = 100
const boundedName = (name: string): string => name.length > MAX_NAME ? `…${name.slice(-MAX_NAME)}` : name

/** A page's preview: `<page> · <site>`, or the site alone for a map's root. `name`: what the URL itself calls the page
 *  (`urlName`), over the table's title. `desc`: a fact about it the caller read, over the table's. */
export const genericMeta = (env: PolicyEnv, url: URL, kind?: string, name?: string | null, desc?: string | null) => {
  const named = namedPage(kind)
  const page = name ? boundedName(name) : named?.title
  const site = env.ROOT_LABEL ?? 'storage'
  return {
    title: page ? `${page} · ${site}` : site,
    desc: desc ?? named?.desc ?? 'Private storage dashboard. Sign in to explore storage.',
    image: url.origin + genericImageOf(kind), imageType: 'image/png', page: url.origin + url.pathname,
  }
}

/** What a `/scans/<id>` preview reads of its `scan_runs` row. */
export interface ScanRunRow { status: 'running' | 'succeeded' | 'failed' | 'nop'; started_ts: number | null; finished_ts: number | null }
const RUN_STATUS: Record<ScanRunRow['status'], string> = { running: 'Running', succeeded: 'Succeeded', failed: 'Failed', nop: 'No-op' }

/** A scan run's description: its state and time, nothing the job produced. */
export const scanRunDesc = (r: ScanRunRow): string => [
  RUN_STATUS[r.status],
  ...(r.finished_ts != null ? [`finished ${utcMinute(r.finished_ts)}`] : r.started_ts != null ? [`started ${utcMinute(r.started_ts)}`] : []),
].join(' · ')
