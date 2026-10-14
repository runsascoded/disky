// The viewer's scan and attribution id: the scan stamped onto staging gestures,
// and the D1 `user_emails` map. (m3 keeps no ownership ledger: no `/api/actions`
// bindings here.)
import { useQuery } from '@tanstack/react-query'

// The scan id the viewer is looking at, stamped onto posted actions. Set by
// App (module-level: mutations fire from deep components that don't
// otherwise care which scan is showing).
let currentScan: string | undefined
export const setCurrentScan = (s?: string) => { currentScan = s }
/** The scan the viewer is looking at (a staging gesture is "as of" it). */
export const getCurrentScan = (): string | undefined => currentScan

/** D1 `user_emails` as an email → canonical-user map (signed-in readers only). */
export function useUserEmails(enabled: boolean): Record<string, string> | undefined {
  return useUserEmailsQ(enabled).data
}

function useUserEmailsQ(enabled: boolean) {
  return useQuery<Record<string, string>, Error>({
    queryKey: ['user-emails'],
    enabled,
    staleTime: 10 * 60_000,
    retry: false,
    queryFn: async () => {
      const r = await fetch('/api/db/user_emails', { credentials: 'include' })
      if (!r.ok) throw new Error(`user_emails: ${r.status}`)
      const { rows } = (await r.json()) as { rows: { email: string; user: string }[] }
      return Object.fromEntries(rows.map(x => [x.email, x.user]))
    },
  })
}

/** The viewer's canonical attribution user id, from D1 `user_emails`. */
export function useMyUser(email: string | undefined, enabled: boolean): string | null {
  return useMyUserState(email, enabled).user
}

/** `useMyUser`, and whether it's final (`ready`: the map loaded or failed, or there's none to load) —
 *  until then a null `user` means "not known yet", not "unmapped". */
export function useMyUserState(email: string | undefined, enabled: boolean): { user: string | null; ready: boolean } {
  const on = enabled && !!email
  const q = useUserEmailsQ(on)
  return { user: (email && q.data?.[email.toLowerCase()]) || null, ready: !on || !q.isPending }
}
