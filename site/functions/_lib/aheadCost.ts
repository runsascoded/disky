/** What a refused request whose session cookie verifies (`presentsCredentials` true: signed under the gate's secret,
 *  unexpired; the gate then refusing it, e.g. its user since removed from the allowlist) can make the routes that
 *  overlap their gate (`shared.ahead`) read: the lookups each starts beside the gate run to
 *  their end, and their isolate writes are discarded (`Scope`), so every such request reads on a cold isolate's terms
 *  whatever the requests before it left uncommitted. Any other request starts nothing (`ahead` waits for the gate): a forged
 *  or expired cookie, a `Bearer` token or `?key=` link costs exactly its `GATE_COST`. Only a holder of a session minted
 *  under the secret can spend this.
 *
 *  Per route, as a function of what the request chooses (`dirs`: the level's directories, at most `LEVEL_DIRS`) and of
 *  the store (`runs`: the interval-store generation's runs, which its compaction bounds; `footers`: the footer groups
 *  the early plan reads, at most `PLAN_AHEAD_FOOTERS.max`), each count the maximum over the request's other choices
 *  (`api/aheadCost.test.ts` asserts each exactly, for the worst-case request):
 *
 *  - **R2** (`INDEX_R2` gets, heads and lists, and the store's gets): the interval store's state (`scans.json`, the
 *    manifests' list, the newest manifest: 3); for level also both sorts' footer indexes of the base (2) and of each
 *    run (a head and a get each: 4), the early plan's footer groups, and the level sort's footer index of the base (1)
 *    and of each run (2), opened first (`openLevelIndex`). Its worst is a generation without the level sort: those
 *    reads find none and the `path` sort's prep and plan run as before; with it, they don't run at all. A date the store
 *    doesn't hold opens no per-scan handle: `prepareLevels` refuses it first (a level reads the interval store).
 *  - **D1** (statements): an exact scan id the interval store doesn't hold is one `index_schema` read (`indexedScan`),
 *    and the request's generations one more (`pathGens`); a subtree's `d=<slug>` reads its day's pointers and their
 *    starts (2) before the exact check of what it resolves to. Held dates: none.
 *  - **KV** reads: one per response-cache key looked up (a level's every asked directory; one for diff and subtree,
 *    unscoped and unfiltered only).
 *  - **colo** reads: those keys, plus the state's copy (1); for level the footer indexes' (2 per sort read: the base 4,
 *    each run 4), the early plan's footer groups, and the level sort's footer indexes (the base 2, each run 2).
 *
 *  The gate's own reads are apart (`GATE_COST`): they are any refused request's, overlap or not. */
export const AHEAD_COST = {
  level: ({ dirs, runs, footers }: { dirs: number; runs: number; footers: number }) => ({
    r2: 3 + 2 + 4 * runs + footers + 1 + 2 * runs,
    d1: 2 + 1,
    kv: dirs,
    colo: dirs + 1 + 4 + 4 * runs + footers + 2 + 2 * runs,
  }),
  diff: { r2: 3, d1: 2 + 1, kv: 1, colo: 1 + 1 },
  subtree: { r2: 3, d1: 2 + 1 + 1, kv: 1, colo: 1 + 1 },
} as const

/** The gate's own reads of a refused credential, apart from `AHEAD_COST`: a session cookie it can't verify (forged,
 *  malformed or expired) reads nothing; a verifying session it refuses reads its standing (the allowlist row, or its
 *  grant) and logs the denial (one D1 write); a token it looks up (one D1 read) and logs the denial of (one D1 write).
 *  A forged credential's whole cost: `cookie`, or `token`, nothing early. */
export const GATE_COST = { cookie: { d1: 0 }, session: { d1: 2 }, token: { d1: 2 } } as const
