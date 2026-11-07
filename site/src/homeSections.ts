// The home page's sections (DOM-free: the Pages Functions share it). The scroll-spy (`useSectionSpy`, param mode)
// names the one in view as `?section=<id>`, where a link-preview unfurler can see it (fragments never reach the
// server); deep links scroll to it.

/** The URL param holding the section in view. Not `s`, use-prms' default: the home page's `?s=` is the shade axis
 * (`?s=c`), the staged page's its sort. */
export const SECTION_KEY = 'section'

/** Section element ids, top to bottom. */
export const SECTION_IDS = ['tree-map', 'tbl', 'over-time', 'diff', 'mtime'] as const

/** Retired anchor ids → the ids that replaced them; old links keep working. */
export const LEGACY_ANCHORS: Record<string, string> = {
  'size-over-time': 'over-time', 'created-date': 'mtime', changes: 'diff',
}

/** The home section a URL fragment names (`#diff`, a retired `#changes`), canonical; null for any other fragment. */
export function sectionOfHash(hash: string): string | null {
  const id = hash.replace(/^#/, '')
  const canon = LEGACY_ANCHORS[id] ?? id
  return (SECTION_IDS as readonly string[]).includes(canon) ? canon : null
}
