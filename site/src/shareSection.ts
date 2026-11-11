import { encodeSection, LEGACY_SECTION_KEY, SECTION_KEY, sectionOfHash, sectionParam } from './homeSections'

/** Carry the visible section to unfurlers, which never receive URL fragments. The page names its section in `?v=`
 * already (the scroll-spy's param mode); a link from before that names it in the legacy `?section=` (rewritten to
 * `?v=`) or, hand-typed or older, in the fragment: promote a home section there (`#diff`, a retired `#changes`) to
 * the param and drop the fragment, which is the URL the page itself would settle on. Any other fragment stays. */
export function shareSection(url: URL): URL {
  const out = new URL(url)
  const id = sectionOfHash(out.hash) ?? sectionParam(out.searchParams)
  const hashed = sectionOfHash(out.hash) != null
  if (id && (hashed || out.searchParams.has(LEGACY_SECTION_KEY))) {
    out.searchParams.delete(LEGACY_SECTION_KEY)
    out.searchParams.set(SECTION_KEY, encodeSection(id))
    if (hashed) out.hash = ''
  }
  return out
}
