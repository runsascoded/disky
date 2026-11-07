import { useEffect } from 'react'
import { DEFAULT_STORE } from './stores'
import { useStore } from './store'

// The suffix every tab title carries: the store's `title` (stores.ts). The
// page-specific crumbs sit in front of it, most-specific first (`Sweep · <title>`,
// `user_emails · DB · Admin · <title>`). A secondary store's pages carry its own
// title instead.
export const SITE = DEFAULT_STORE.title

/** Set `document.title` to `<crumb> · … · <store title>`. Pass the
 *  page-specific crumbs (most-specific first); falsy crumbs drop out, and no
 *  crumbs yields the bare site name (the home page). */
export function useDocTitle(...crumbs: (string | false | null | undefined)[]) {
  const site = useStore().title
  const title = [...crumbs.filter(Boolean), site].join(' · ')
  useEffect(() => {
    document.title = title
  }, [title])
}
