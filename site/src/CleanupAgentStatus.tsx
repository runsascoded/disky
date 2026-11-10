import { useQuery } from '@tanstack/react-query'
import { call } from './plans'

export interface CleanupAgent {
  executor: 'laptop'
  ready: boolean
  last_seen: number | null
  checked_at: number
  stale_after: number
}

export function useCleanupAgent(enabled: boolean) {
  return useQuery<CleanupAgent, Error>({
    queryKey: ['cleanup-agent'],
    queryFn: () => call('/api/executor-status'),
    enabled,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
}

/** The agent's D1 check-in, independent of the file-preview tunnel's health. */
export function CleanupAgentStatus({ agent, error, checking, recheck }: { agent?: CleanupAgent; error: Error | null; checking: boolean; recheck: () => void }) {
  const state = error || !agent ? 'unknown' : agent.ready ? 'connected' : 'disconnected'
  const age = agent?.last_seen == null ? null : Math.max(0, agent.checked_at - agent.last_seen)
  const ago = age == null ? null : age < 60 ? `${age}s` : age < 3600 ? `${Math.floor(age / 60)}m` : `${Math.floor(age / 3600)}h ${Math.floor(age % 3600 / 60)}m`
  return (
    <div className={`cleanup-agent ${state}`} aria-label="Cleanup agent">
      <div className="cleanup-agent-head">
        <strong><span className="dot" aria-hidden="true" />Cleanup agent {state === 'unknown' ? (checking ? 'checking…' : 'status unknown') : state}</strong>
        <button type="button" disabled={checking} onClick={recheck}>{checking ? 'checking…' : 'check again'}</button>
      </div>
      {agent && <div className="dim">{agent.last_seen == null ? 'No check-in recorded.' : <>Last check-in {ago} ago · <time dateTime={new Date(agent.last_seen * 1000).toISOString()}>{new Date(agent.last_seen * 1000).toLocaleString()}</time>.</>} Status checked {new Date(agent.checked_at * 1000).toLocaleTimeString()}.</div>}
      {state === 'disconnected' && <p>New requests are paused; your items remain staged. Open disky on the laptop and check its network connection, then check again. A computer restart should not be necessary.</p>}
      {state === 'connected' && <p>Accepting requests. A check-in confirms the agent is reachable; each run reports its own start and completion below.</p>}
      {error && <p role="alert">Could not check the cleanup agent: {error.message}. Check Runs before retrying a submission.</p>}
    </div>
  )
}
