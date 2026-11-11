import { useEffect } from 'react'
import { DEFAULT_STORE } from './stores'
import { useStore } from './store'

// The suffix every tab title carries: the primary store's `title` (stores.ts), the
// site's name. The page-specific crumbs sit in front of it, most-specific first
// (`Sweep · <site>`, `user_emails · DB · Admin · <site>`); a secondary store's pages
// put its `label` before the site's name, as a page would (`Meta · <site>`).
export const SITE = DEFAULT_STORE.title

/** Set `document.title` to `<crumb> · … · <store title>`. Pass the
 *  page-specific crumbs (most-specific first); falsy crumbs drop out, and no
 *  crumbs yields the bare site name (the home page). */
export function useDocTitle(...crumbs: (string | false | null | undefined)[]) {
  const store = useStore()
  const title = [...crumbs.filter(Boolean), store.key !== DEFAULT_STORE.key && store.label, SITE].filter(Boolean).join(' · ')
  useEffect(() => {
    document.title = title
  }, [title])
}
