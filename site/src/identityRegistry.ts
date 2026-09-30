/** The display-identity registry: canonical id → short name + GitHub handle,
 * with alias keys (email local parts, short handles) pointing at their
 * canonical rec. Built at runtime from the deployment's published attribution
 * rules (`/data/rules.json`, members-only) — never bundled: the roster is a
 * deployment's data, not the base's. React- and DOM-free, so the Functions
 * (`_lib/identity.ts`) build the same registry server-side. */
import type { RuleUser } from './types'

export interface IdentityRec {
  u: string  // canonical user id
  name: string
  github?: string
}
export type Registry = Record<string, IdentityRec>

/**
 * Email or display string → canonical-id-shaped slug: the local part, sanitized
 * like rigging's `sanitize_username` (lowercase; runs of non-`[a-z0-9_-]` → '-').
 * So `jane.doe@example.org` → `jane-doe` = the canonical id (the registry key).
 */
export const whoToHandle = (who: string): string =>
  who.split('@')[0].toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')

/** Default short name: the capitalized first segment of the id. */
export const defaultName = (id: string): string => id.split('-')[0].replace(/^./, c => c.toUpperCase())

/** Canonical ids win over aliases; an alias resolves to its canonical rec. */
export function buildRegistry(users: RuleUser[]): Registry {
  const reg: Registry = {}
  const aliased: Registry = {}
  for (const { u, aliases, name, github } of users) {
    const rec: IdentityRec = { u, name: name ?? defaultName(u), ...(github ? { github } : {}) }
    reg[u] = rec
    for (const a of aliases) aliased[whoToHandle(a)] = rec
  }
  for (const [k, rec] of Object.entries(aliased)) if (!(k in reg)) reg[k] = rec
  return reg
}

/** Canonical id for an actor (email or raw id) under a registry. */
export const canonIn = (reg: Registry, who: string): string => {
  const h = whoToHandle(who)
  return reg[h]?.u ?? h
}
