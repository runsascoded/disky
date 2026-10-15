import type { ReactNode } from 'react'
import { useIdent } from './auth'
import { SelfContext } from './UserChip'

/** The session's identity under every user chip (`UserChip`'s `SelfContext`), so the signed-in user's own chips show
 *  the header's photo. */
export function SelfProvider({ children }: { children: ReactNode }) {
  return <SelfContext.Provider value={useIdent()}>{children}</SelfContext.Provider>
}
