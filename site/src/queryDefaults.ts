import type { DefaultOptions } from '@tanstack/react-query'

/** The app's query-client defaults (`main.tsx`). A tab left open on the latest scan should pick up new data on its
 *  own, so queries stay fresh in the background rather than only on mount. A read that names no `retry` of its own
 *  is asked twice more on a failure (`fetchQuery` included). */
export const QUERY_DEFAULTS: DefaultOptions = {
  queries: {
    staleTime: 60_000,
    refetchOnWindowFocus: true,
    retry: 2,
  },
}
