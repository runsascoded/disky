import type { CardData } from './card.js'

/** Bump whenever rendering or public-disclosure rules change. */
export const RENDER_VERSION = '8'
export type AnonymousPolicy = 'generic' | 'shape' | 'full'
export interface PolicyEnv { OG_ANONYMOUS?: string; ROOT_LABEL?: string; AUTH_MODE?: string }

/** Private deployments require explicit shape/totals opt-in. Unknown settings fail closed. `full`: an anonymous
 * unfurl gets the full card (labelled tiles, owners), for a deployment whose sizes and paths aren't secret behind its
 * sign-in (cw-s3: the site is gated, its link previews are not). */
export const anonymousPolicy = (env: PolicyEnv): AnonymousPolicy =>
  env.OG_ANONYMOUS === 'full' ? 'full'
    : env.OG_ANONYMOUS === 'shape' || (env.OG_ANONYMOUS == null && env.AUTH_MODE === 'public') ? 'shape' : 'generic'

/** The tier a card is drawn at: an anonymous fetch under the `full` policy draws the full card. */
export const cardTier = (tier: 'anon' | 'full', policy: AnonymousPolicy): 'anon' | 'full' =>
  tier === 'anon' && policy === 'full' ? 'full' : tier
export const genericImagePath = `/og/map.png?policy=generic&r=${RENDER_VERSION}`

export const genericCard = (env: PolicyEnv): CardData => ({
  tier: 'anon', site: env.ROOT_LABEL ?? 'storage', title: 'Private storage dashboard',
  subtitle: '', total: '', tiles: [], empty: 'Sign in to explore storage',
})

export const genericMeta = (env: PolicyEnv, url: URL) => ({
  title: env.ROOT_LABEL ?? 'storage', desc: 'Private storage dashboard. Sign in to explore storage.',
  image: url.origin + genericImagePath, imageType: 'image/png', page: url.origin + url.pathname,
})
