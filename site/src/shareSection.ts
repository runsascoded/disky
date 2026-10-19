import { SECTION_KEY, sectionOfHash } from './homeSections'

/** Carry the visible section to unfurlers, which never receive URL fragments. The page names its section in
 * `?section=` already (the scroll-spy's param mode); a link from before that, or a hand-typed one, names it in the
 * fragment: promote a home section there (`#diff`, a retired `#changes`) to the param and drop the fragment, which
 * is the URL the page itself would settle on. Any other fragment stays. */
export function shareSection(url: URL): URL {
  const out = new URL(url)
  const id = sectionOfHash(out.hash)
  if (id) {
    out.searchParams.set(SECTION_KEY, id)
    out.hash = ''
  }
  return out
}
