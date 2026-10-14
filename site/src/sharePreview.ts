// Sharing a page (specs/done/dogi.md): a plain link (its unfurl follows the
// deployment's anonymous preview policy), a link whose unfurl card shows details
// (`og=`, a per-view preview token; it grants no access), or, for admins, a
// link that also grants access (`key=`, a share-link grant; its card is full
// since its bearer gets in anyway).
import { useCallback, useState } from 'react'
import { DEFAULT_STORE } from './stores'
import { shareSection } from './shareSection'

export const SHARE_DAYS = 30
/** The Share dialog's expiry choices, in days; 0 = never expires (revocable either way). */
export const SHARE_DAY_CHOICES = [1, 7, 30, 90, 365, 0] as const
export const fmtShareDays = (days: number): string =>
  days === 0 ? 'never expires' : days === 365 ? '1 year' : `${days} day${days === 1 ? '' : 's'}`

/** `href` without its `og=` preview token (null when it has none): the
 * token only matters to link unfurlers, so the address bar drops it on load
 * and a re-share doesn't carry someone else's preview. Everything else
 * (path, other params, hash) is kept, params in order. */
export function withoutOg(href: string): string | null {
  const u = new URL(href)
  if (!u.searchParams.has('og')) return null
  u.searchParams.delete('og')
  return `${u.pathname}${u.searchParams.size ? `?${u.searchParams}` : ''}${u.hash}`
}

/** The page link to share: `href` minus any `og=` / `key=` it carried, plus
 * the ones given. */
export function shareUrl(href: string, add: { og?: string; key?: string } = {}): string {
  const u = shareSection(new URL(href))
  u.searchParams.delete('og')
  u.searchParams.delete('key')
  if (add.og) u.searchParams.set('og', add.og)
  if (add.key) u.searchParams.set('key', add.key)
  return u.href
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, { method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
  const j = (await r.json().catch(() => ({}))) as T & { error?: string; detail?: string }
  if (!r.ok) throw new Error(j.detail ? `${j.error}: ${j.detail}` : j.error ?? `${r.status}`)
  return j
}

/** A link whose unfurl card shows details (labels, sizes, owners), for `days` (0 = never expires). */
export async function mintPreviewLink(href: string, days = SHARE_DAYS): Promise<string> {
  const { token } = await postJson<{ token: string }>('/api/og/mint', { url: shareUrl(href), days })
  return shareUrl(href, { og: token })
}

/** Admin: a link that grants read-only access to whoever holds it (a
 * share-link grant), expiring after `days` (0 = never). */
export async function mintAccessLink(href: string, days = SHARE_DAYS): Promise<string> {
  const path = new URL(href).pathname
  const { token } = await postJson<{ token: string }>('/api/auth/grants', {
    note: `shared from ${path}`,
    scopes: [`${DEFAULT_STORE.key}:read`],
    expiresInS: days === 0 ? null : days * 86400,
  })
  return shareUrl(href, { key: token })
}

export type ShareMode = 'plain' | 'preview' | 'access'

/** Build (minting as needed) the link for `mode` and copy it. */
export async function copyShare(href: string, mode: ShareMode, days = SHARE_DAYS): Promise<string> {
  const url = mode === 'access' ? await mintAccessLink(href, days) : mode === 'preview' ? await mintPreviewLink(href, days) : shareUrl(href)
  await navigator.clipboard.writeText(url)
  return url
}

export type ShareStatus = { state: 'idle' } | { state: 'busy' } | { state: 'copied'; url: string } | { state: 'error'; error: string }

export function useShare() {
  const [status, setStatus] = useState<ShareStatus>({ state: 'idle' })
  const share = useCallback(async (mode: ShareMode, days = SHARE_DAYS) => {
    setStatus({ state: 'busy' })
    try {
      setStatus({ state: 'copied', url: await copyShare(window.location.href, mode, days) })
    } catch (e) {
      setStatus({ state: 'error', error: (e as Error).message })
    }
  }, [])
  return { share, status, reset: () => setStatus({ state: 'idle' }) }
}
