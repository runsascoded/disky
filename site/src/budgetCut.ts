// A filter view or diff whose phase 2 its time budget cut short (`budgetCut`: e.g. a heavy literal's nested
// rollup on a cold isolate, the fleet root drawn to bucket → top dirs only) is exact in its totals but partial
// in its drawn interiors; the server computes the whole answer in the background and replaces it at the edge
// (`upgradePartial`, functions/_lib/edgeCache.ts). The client's queries are `staleTime: Infinity`, so without
// asking again the cut answer stays on screen for the session: ask again every `BUDGET_CUT_REFETCH_MS` until
// the answer is whole, at most `BUDGET_CUT_REFETCHES` times (the server gives up on a key whose upgrade was cut
// too, until its partial expires).

export const BUDGET_CUT_REFETCH_MS = 3000
export const BUDGET_CUT_REFETCHES = 10

/** react-query's `refetchInterval` for a view query: `data` the held answer, `fetches` how many times it was
 *  fetched (`query.state.dataUpdateCount`). */
export function budgetCutRefetch(data: { budgetCut?: boolean } | undefined, fetches: number): number | false {
  return data?.budgetCut && fetches <= BUDGET_CUT_REFETCHES ? BUDGET_CUT_REFETCH_MS : false
}
