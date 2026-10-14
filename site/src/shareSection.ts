/** Carry the visible section to unfurlers, which never receive URL fragments. */
export function shareSection(url: URL): URL {
  const out = new URL(url)
  if (out.hash === '#diff' || out.hash === '#changes') out.searchParams.set('section', 'diff')
  else if (out.hash && out.searchParams.get('section') === 'diff') out.searchParams.delete('section')
  return out
}
