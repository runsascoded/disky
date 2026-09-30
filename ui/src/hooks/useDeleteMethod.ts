/** The effective delete method for the current viewer (spec `specs/staged-delete.md`
 *  CP6): the deployment's `deleteApproval` policy, with a sticky per-user override
 *  when the policy is `user-choice`.
 *
 *  - `sync`   — the trash gesture deletes inline (needs an inline executor: `delete`).
 *  - `staged` — it stages into the admin queue (needs the queue: `stageDelete`).
 *
 *  A locked deployment (`sync` or `staged`) never offers the control, so trust is
 *  enforced server-side, not by a hideable client toggle. The preference is a
 *  per-viewer convenience in `localStorage`, so a failed read/write is non-fatal. */
import { useSyncExternalStore } from 'react'
import type { Capabilities } from '../api'
import { useCapabilities } from './useCapabilities'

export type DeleteMethod = 'sync' | 'staged'

const KEY = 'disk-tree:deleteMethod'

function read(): DeleteMethod | null {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'sync' || v === 'staged' ? v : null
  } catch {
    return null
  }
}

let current: DeleteMethod | null = read()
const listeners = new Set<() => void>()

/** Set the sticky per-user method; notifies every `useDeleteMethod` consumer. */
export function setDeleteMethod(m: DeleteMethod): void {
  current = m
  try {
    localStorage.setItem(KEY, m)
  } catch {
    /* private window / blocked storage — the in-memory value still drives the session */
  }
  listeners.forEach(l => l())
}

const subscribe = (cb: () => void): (() => void) => {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

/** Resolve the method from capabilities + a stored preference. Pure, so the
 *  behavior is testable and the hook is a thin wrapper. */
export function resolveDeleteMethod(
  caps: Capabilities | undefined,
  pref: DeleteMethod | null,
): { method: DeleteMethod; offered: boolean } {
  const approval = caps?.deleteApproval ?? 'sync'
  const offered = approval === 'user-choice'
  let method: DeleteMethod = offered
    ? pref ?? (caps?.delete ? 'sync' : 'staged')
    : approval === 'staged'
      ? 'staged'
      : 'sync'
  // Fall back if the chosen method needs an executor this deployment lacks.
  if (caps) {
    if (method === 'sync' && !caps.delete && caps.stageDelete) method = 'staged'
    if (method === 'staged' && !caps.stageDelete && caps.delete) method = 'sync'
  }
  return { method, offered }
}

export function useDeleteMethod(): {
  method: DeleteMethod
  offered: boolean
  setMethod: (m: DeleteMethod) => void
} {
  const caps = useCapabilities()
  const pref = useSyncExternalStore(subscribe, () => current, () => null)
  return { ...resolveDeleteMethod(caps, pref), setMethod: setDeleteMethod }
}
