import type { PrefixExecution } from './prefixExecution'
import type { PrefixStat } from './prefixes'
import { stagedTree } from './stagedTree'

export type StagedState = 'deleted' | 'empty' | 'restored' | 'recorded' | 'pending' | 'running' | 'unknown'

/** An empty scan is not deletion evidence; a partial restore is not complete. */
export function stagedState(stat: PrefixStat | undefined, sized: boolean, execution: PrefixExecution): StagedState {
  if (execution?.kind === 'live-prefix' || execution?.kind === 'live-bucket') return 'running'
  if (execution?.kind === 'recorded' && execution.undone > 0) return 'restored'
  if (!sized) return execution?.kind === 'recorded' ? 'recorded' : 'unknown'
  if (stat && (stat.b > 0 || stat.o > 0)) return execution?.kind === 'recorded' ? 'recorded' : 'pending'
  return execution?.kind === 'recorded' ? 'deleted' : 'empty'
}

export function batchCompletion(states: readonly StagedState[]) {
  const counts = { deleted: 0, empty: 0, restored: 0, recorded: 0, pending: 0, running: 0, unknown: 0 }
  for (const state of states) counts[state]++
  return { ...counts, settled: states.length > 0 && counts.deleted + counts.empty === states.length }
}

/** Only unsettled prefixes still belong to the dispatchable staged set. */
export const isCurrentlyStaged = (state: StagedState): boolean => state !== 'deleted' && state !== 'empty'

/** Explicit choices survive refreshes; linked batches and flat tables stay accessible. */
export const batchIsCollapsed = (settled: boolean, override: boolean | undefined, linked = false, flat = false): boolean =>
  !flat && (override ?? (settled && !linked))

/** Never mount the treemap (or its detail drawer) for a zero-area root. */
export function stagedMapTree(prefixes: string[], stats: Record<string, PrefixStat>, rootName: string) {
  const tree = stagedTree(prefixes, stats, rootName)
  return tree.b > 0 ? tree : null
}
