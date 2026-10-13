// The owner axis of a map / diff view (`?o=`, `?by=`) as request params and query-key parts, and when they
// can be built at all. Every input here but the URL is async: the identity registry (`rules.json`, which
// canonicalizes a golfed user key), the viewer's own user id (`user_emails`, for `?o=me`) and the live
// ownership ledger (`/api/actions`). A view's queries keyed on a part that moves once one of them lands are
// cancelled mid-flight (TSQ aborts a fetch whose last observer leaves) and fired again — on a cold
// `?o=…` load that was every `/api/subtree` and `/api/diff`, twice (gcs 2026-10-10).

export type OwnerMode = 'all' | 'user' | 'owned' | 'unowned' | 'others'

export interface OwnerScope {
  ownerUser: string | null
  notUsers: string[]
  ownerMode: OwnerMode
  /** `?o=me` for a viewer with no attribution id: the axis falls back to "all", with a note. */
  meUnmapped: boolean
  /** `user:<id>` (the server's `lens=`), or null. */
  activeLens: string | null
  assigner: string | null
  /** The owner part of the view's query string (`&lens=…`, `&by=…`, `&o=…`). */
  qs: string
  /** The server folds the live ledger into this view (a user lens or a pool): its keys carry the ledger. */
  ledgered: boolean
}

/** The owner axis from the URL (`oP` = decoded `?o=`, `byP` = `?by=`), the viewer's id and the registry's
 *  `canon`. */
export function ownerScope({ ownersMode, oP, byP, myUser, canon }: {
  ownersMode: boolean
  oP: string | undefined
  byP: string | undefined
  myUser: string | null
  canon: (who: string) => string
}): OwnerScope {
  const notUsers = ownersMode && oP?.startsWith('!') ? oP.slice(1).split(',').filter(Boolean).map(canon) : []
  const ownerUser =
    !ownersMode || !oP || oP === 'owned' || oP === 'unowned' || oP.startsWith('!') ? null
    : oP === 'me' ? myUser
    : canon(oP)
  const ownerMode: OwnerMode =
    !ownersMode || !oP ? 'all'
    : notUsers.length ? 'others'
    : oP === 'owned' ? 'owned' : oP === 'unowned' ? 'unowned' : ownerUser ? 'user' : 'all'
  const activeLens = ownerUser ? `user:${ownerUser}` : null
  const assigner = ownersMode && byP ? canon(byP) : null
  const qs =
    (activeLens ? `&lens=${activeLens}` : '') +
    (activeLens && assigner ? `&by=${encodeURIComponent(assigner)}` : '') +
    (ownerMode === 'owned' || ownerMode === 'unowned' ? `&o=${ownerMode}` : '') +
    (notUsers.length ? `&o=!${notUsers.map(encodeURIComponent).join(',')}` : '')
  return {
    ownerUser, notUsers, ownerMode,
    meUnmapped: ownersMode && oP === 'me' && !myUser,
    activeLens, assigner, qs,
    ledgered: !!activeLens || ownerMode === 'owned' || ownerMode === 'unowned' || notUsers.length > 0,
  }
}

/** Whether the owner axis's params are final: a user key (`?o=x`, `?o=!x`, `?by=x`) waits for the
 *  registry (`registryReady`: loaded, or failed for good), `?o=me` also for the viewer's id. The pools
 *  and an unscoped view never wait. A view's queries stay disabled until this holds, so the first
 *  request is the one the page means. */
export function ownerScopeReady({ ownersMode, oP, byP, registryReady, myUserReady }: {
  ownersMode: boolean
  oP: string | undefined
  byP: string | undefined
  registryReady: boolean
  myUserReady: boolean
}): boolean {
  if (!ownersMode || !oP || oP === 'owned' || oP === 'unowned') return true
  if (oP === 'me' && !myUserReady) return false
  return registryReady || (oP === 'me' && !byP)
}

/** The ledger's revision as a key part: the newest action id and the row count. */
export const ledgerRevOf = (count: number, maxActionId: number): string => `${count}.${maxActionId}`

/** The ledger's part of a ledgered view's key: `'0'` until the ledger moves past `base`, the revision
 *  its first load had (undefined: not loaded yet). The first load isn't a change — the server folds the
 *  live ledger into every request anyway — so a view fired before `/api/actions` lands keeps its key,
 *  and only a later revision (an assignment, its undo, another viewer's seen by the poll) re-reads. */
export const ledgerKey = (rev: string, base: string | undefined): string =>
  base === undefined || rev === base ? '0' : rev

/** `ledgerKey` against the first revision it is handed once the ledger has `loaded`. */
export function ledgerKeyer(): (rev: string, loaded: boolean) => string {
  let base: string | undefined
  return (rev, loaded) => {
    if (loaded && base === undefined) base = rev
    return ledgerKey(rev, base)
  }
}

/** This page load's: module-level, so it outlives a remount — a view cached under `'0'` means the same
 *  revision for the QueryClient's whole life. */
export const pageLedgerKey = ledgerKeyer()

/** A view's scope as a key part: its query string, plus the ledger's for a ledgered view. */
export const scopeKeyOf = (scopeQs: string, ledgered: boolean, ledger: string): string =>
  scopeQs + (ledgered ? `|ledger=${ledger}` : '')
