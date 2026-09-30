/** The live display-identity registry (see `identityRegistry.ts`): empty until
 * the deployment's rules load, so a store without them (the public demo, a
 * plan-first store) shows default names and initials — and ships no roster. */
import { useEffect, useSyncExternalStore } from 'react'
import { buildRegistry, type Registry } from './identityRegistry'
import { useRules } from './rules'

let reg: Registry = {}
const subs = new Set<() => void>()
const subscribe = (f: () => void) => { subs.add(f); return () => { subs.delete(f) } }

export const registry = (): Registry => reg

export function setRegistry(r: Registry): void {
  reg = r
  for (const f of subs) f()
}

/** Re-render with the registry (it arrives after first paint). */
export const useRegistry = (): Registry => useSyncExternalStore(subscribe, registry)

/** Mount once, at the root: load the registry from the published rules, and
 * re-render the tree when it lands (sync helpers like `shortName` read it). */
export function useLoadIdentities(): void {
  const { data } = useRules()
  useEffect(() => { if (data) setRegistry(buildRegistry(data.users)) }, [data])
  useRegistry()
}
