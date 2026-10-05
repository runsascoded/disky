// Personal agent-token panel (specs/actions-ledger.md § API).
//
// Opens from the header chip. Shows whether the signed-in user has an active
// token, mints/rotates one (the server stores only its hash), and revokes.
// The raw value is also cached in this browser's localStorage at mint, so
// *this device* can re-show it — any other device sees status only.
import { useCallback, useEffect, useState } from 'react'
import { useStore } from './store'

interface Status {
  active: boolean
  created: number | null
}

// Device-local token cache. The server can never re-show a token (hash-only
// storage); the browser that minted it can. Keyed with the mint time so a
// rotation from another device is detectable as stale.
const LS_KEY = 'gcs_agent_token'

const lsGet = (): { token: string; created: number } | null => {
  try {
    const raw = localStorage.getItem(LS_KEY)
    return raw ? (JSON.parse(raw) as { token: string; created: number }) : null
  } catch {
    return null
  }
}

const lsSet = (token: string, created: number) => {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({ token, created }))
  } catch { /* private mode etc. — re-show just won't work */ }
}

const lsClear = () => {
  try {
    localStorage.removeItem(LS_KEY)
  } catch { /* ignore */ }
}

type Phase =
  | { k: 'loading' }
  | { k: 'status'; s: Status }
  | { k: 'minted'; token: string; created: number }
  | { k: 'error'; msg: string }

const fmt = (unixS: number): string =>
  new Date(unixS * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC'

async function call(method: 'GET' | 'POST' | 'DELETE'): Promise<Response> {
  return fetch('/api/token', { method, credentials: 'include' })
}

export default function TokenModal({ onClose }: { onClose: () => void }) {
  const store = useStore()
  const [phase, setPhase] = useState<Phase>({ k: 'loading' })
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  const loadStatus = useCallback(async () => {
    try {
      const r = await call('GET')
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error ?? `HTTP ${r.status}`)
      setPhase({ k: 'status', s: (await r.json()) as Status })
    } catch (e) {
      setPhase({ k: 'error', msg: e instanceof Error ? e.message : String(e) })
    }
  }, [])

  useEffect(() => {
    void loadStatus()
  }, [loadStatus])

  // Escape closes; the backdrop click does too (handler below).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const mint = async () => {
    setBusy(true)
    try {
      const r = await call('POST')
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error ?? `HTTP ${r.status}`)
      const { token, created } = (await r.json()) as { token: string; created: number }
      lsSet(token, created)
      setPhase({ k: 'minted', token, created })
      setCopied(false)
    } catch (e) {
      setPhase({ k: 'error', msg: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const revoke = async () => {
    setBusy(true)
    try {
      const r = await call('DELETE')
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error ?? `HTTP ${r.status}`)
      lsClear()
      await loadStatus()
    } catch (e) {
      setPhase({ k: 'error', msg: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  const copy = (token: string) => {
    void navigator.clipboard?.writeText(token).then(() => setCopied(true))
  }

  return (
    <div className="token-backdrop" onClick={onClose}>
      <div className="token-modal" onClick={e => e.stopPropagation()} role="dialog" aria-label="Agent token">
        <div className="token-head">
          <strong>Agent token</strong>
          <button className="token-x" type="button" onClick={onClose} aria-label="Close">×</button>
        </div>

        {phase.k === 'loading' && <p className="token-muted">Loading…</p>}

        {phase.k === 'error' && (
          <>
            <p className="token-err">{phase.msg}</p>
            <button type="button" onClick={() => void loadStatus()}>Retry</button>
          </>
        )}

        {phase.k === 'status' && (() => {
          // Re-showable only if this browser minted the *current* token.
          const cached = lsGet()
          const showable = phase.s.active && cached != null && cached.created === phase.s.created
          return (
          <>
            <p className="token-muted">
              A personal token lets your agents assign prefixes as you, from any HTTP client.
              It carries only the <code>gcs</code> scope (assign &amp; view) — the server stores only its
              hash, but the browser that minted it can re-show it.
            </p>
            {phase.s.active
              ? <p>Active token, created <strong>{fmt(phase.s.created!)}</strong>.</p>
              : <p className="token-muted">No token yet.</p>}
            <div className="token-actions">
              {showable && (
                <button type="button" onClick={() => setPhase({ k: 'minted', token: cached.token, created: cached.created })}>
                  Show token
                </button>
              )}
              <button type="button" onClick={() => void mint()} disabled={busy}>
                {phase.s.active ? 'Rotate token' : 'Generate token'}
              </button>
              {phase.s.active && (
                <button type="button" className="token-danger" onClick={() => void revoke()} disabled={busy}>
                  Revoke
                </button>
              )}
            </div>
            {phase.s.active && !showable && (
              <p className="token-muted token-small">
                This browser didn’t mint the current token, so it can’t re-show it — rotate to get a fresh one.
              </p>
            )}
            {phase.s.active && (
              <p className="token-muted token-small">
                Rotating revokes the current token immediately — anything using it stops working until
                you paste the new one.
              </p>
            )}
          </>
          )
        })()}

        {phase.k === 'minted' && (
          <>
            <p className="token-warn">
              Copy this — only this browser can re-show it (the server keeps just the hash).
            </p>
            <div className="token-value">
              <code>{phase.token}</code>
              <button type="button" onClick={() => copy(phase.token)}>{copied ? 'Copied' : 'Copy'}</button>
            </div>
            <p className="token-muted token-small">Then, in your agent’s environment:</p>
            <pre className="token-recipe">{`export GCS_USAGE_TOKEN=${phase.token}
curl -sS -H "Authorization: Bearer $GCS_USAGE_TOKEN" -H 'content-type: application/json' \\
  -d '{"pattern": "${store.scheme}${store.buckets[0] ?? 'my-bucket'}/path/to/my-run/", "owner": "@me"}' \\
  ${location.origin}/api/actions`}</pre>
            <div className="token-actions">
              <button type="button" onClick={() => void loadStatus()}>Done</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
