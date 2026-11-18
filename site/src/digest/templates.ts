/** The digest's templates (`digest.TEMPLATES`) and their old names (`digest.TEMPLATE_ALIASES`): keep identical. Every
 * input names a template through `canonicalTemplate` (`/api/digest-settings`, `/api/digest-op`, `DIGEST_REAPPLY`, a
 * preview bundle), so nothing past it sees an alias. Pure; DOM-free. */
export const TEMPLATES = ['quota', 'cost'] as const
export type TemplateName = typeof TEMPLATES[number]
/** Each template's old name (deployments' configs, scan profiles and older preview bundles still carry them). */
export const TEMPLATE_ALIASES: Record<string, TemplateName> = { cw: 'quota', gcs: 'cost' }

/** `name`, or the template its old name stands for; null for anything else. */
export function canonicalTemplate(name: unknown): TemplateName | null {
  if (typeof name !== 'string') return null
  const t = Object.hasOwn(TEMPLATE_ALIASES, name) ? TEMPLATE_ALIASES[name] : name
  return (TEMPLATES as readonly string[]).includes(t) ? t as TemplateName : null
}

/** `{cfg, rows}` (a preview bundle, a `/api/digest-op` request) with each `template` its canonical name; an unknown one
 *  is left as it is, for the caller's check to refuse. */
export function canonicalTemplates<T extends { cfg?: unknown; rows?: unknown }>(b: T): T {
  const fix = (o: unknown): unknown => {
    if (typeof o !== 'object' || o === null || Array.isArray(o)) return o
    const t = canonicalTemplate((o as { template?: unknown }).template)
    return t ? { ...o, template: t } : o
  }
  return { ...b, cfg: fix(b.cfg), rows: fix(b.rows) }
}
