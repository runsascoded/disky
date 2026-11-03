import type { CardData } from './card.js'
import type { OgKind } from './routes.js'

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

/** Pages an anonymous unfurl may name under the `generic` policy: fixed text a public URL already gives away, no data
 *  read (no counts, no `q`). Every other page, whose URL can hold a private path or name, stays the one generic card. */
const NAMED_PAGES: Partial<Record<OgKind, { title: string; desc: string; empty: string }>> = {
  staged: { title: 'Staged for deletion', desc: 'Prefixes staged for deletion, for review. Sign in to see them.', empty: 'Sign in to review' },
}

/** A kind's generic image: its own URL (and so cache key) only for a named page. */
export const genericImageOf = (kind?: string): string =>
  kind && Object.hasOwn(NAMED_PAGES, kind) ? `/og/${kind}.png?policy=generic&r=${RENDER_VERSION}` : genericImagePath

export const genericCard = (env: PolicyEnv, kind?: string): CardData => {
  const named = kind && Object.hasOwn(NAMED_PAGES, kind) ? NAMED_PAGES[kind as OgKind] : undefined
  return {
    tier: 'anon', site: env.ROOT_LABEL ?? 'storage', title: named?.title ?? 'Private storage dashboard',
    subtitle: '', total: '', tiles: [], empty: named?.empty ?? 'Sign in to explore storage',
  }
}

/** The generic page title: `disky — <label>`, or the label alone when it names disky already ("m3 disky metadata"). */
export const genericTitle = (label: string | undefined): string => {
  const l = label ?? 'storage'
  return /\bdisky\b/i.test(l) ? l : `disky — ${l}`
}

export const genericMeta = (env: PolicyEnv, url: URL, kind?: OgKind) => {
  const named = kind && NAMED_PAGES[kind]
  return {
    title: named ? `${named.title} · ${env.ROOT_LABEL ?? 'storage'}` : genericTitle(env.ROOT_LABEL),
    desc: named?.desc ?? 'Private storage dashboard. Sign in to explore storage.',
    image: url.origin + genericImageOf(kind), imageType: 'image/png', page: url.origin + url.pathname,
  }
}
