// The site's one open dialog (About, Share, Profile, Agent token): a module
// store, so the ☰ / user menus and the ⌘K omnibar open the same instance.
import { useSyncExternalStore } from 'react'

export type Dialog = 'about' | 'share' | 'profile' | 'token'

let current: Dialog | null = null
const listeners = new Set<() => void>()
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } }
const get = () => current

export function openDialog(d: Dialog | null): void {
  if (d === current) return
  current = d
  listeners.forEach(l => l())
}
export const closeDialog = (): void => openDialog(null)
export const useDialog = (): Dialog | null => useSyncExternalStore(subscribe, get, get)
