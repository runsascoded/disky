// The scanned machine itself, from the site (specs/laptop-file-tunnel.md,
// specs/laptop-app-links.md): whether its file tunnel answers now, and the
// disky app's actions on a path — select it in Finder, or open it there.
import { useQuery } from '@tanstack/react-query'
import { OPEN_FILE_URL, REVEAL_URL, inApp, onMac } from './appLink'
import type { ProxyInfo } from './objects'
import { useStore, useStoreFetch } from './store'
import { Tooltip } from './Tooltip'

/** `/api/store`: the store's file proxy, and (for an admin) whether its files
 *  open through the machine's tunnel (`fileTunnel`). */
export function useProxyInfo(enabled = true) {
  const store = useStore()
  const sfetch = useStoreFetch()
  return useQuery<ProxyInfo>({
    queryKey: ['store-proxy', store.key],
    queryFn: async () => {
      const r = await sfetch('/api/store')
      if (!r.ok) throw new Error(`store: ${r.status}`)
      return r.json()
    },
    enabled,
    staleTime: Infinity,
  })
}

export interface LaptopStatus { online: boolean; reason?: string }

/** The tunnel's health (`/v1/laptop-files/status`), refetched on focus and every minute. */
export function useLaptopStatus(enabled: boolean) {
  return useQuery<LaptopStatus>({
    queryKey: ['laptop-status'],
    queryFn: async () => {
      const r = await fetch('/v1/laptop-files/status')
      if (!r.ok) throw new Error(`laptop status: ${r.status}`)
      return r.json()
    },
    enabled,
    staleTime: 15_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
}

/** Why the laptop reads offline, in words. */
const OFFLINE_WHY: Record<string, string> = {
  'file serving off': 'the disky app is up, with file previews turned off',
  'tunnel 530': 'asleep, the disky app not running, or its file previews off',
  timeout: 'it didn’t answer within 3 s',
}

/** Header dot beside "scanned …": an admin on a store whose files open through the tunnel. */
export function LaptopIndicator() {
  const proxy = useProxyInfo()
  const tunnel = !!proxy.data?.fileTunnel
  const q = useLaptopStatus(tunnel)
  if (!tunnel || !q.data) return null
  const { online, reason } = q.data
  return (
    <Tooltip content={<>
      File previews reach this Mac through the disky app’s tunnel.
      {online ? <> It’s answering now.</> : <> Offline: {OFFLINE_WHY[reason ?? ''] ?? reason ?? 'no answer'}. The map still shows the last scan.</>}
    </>}>
      <span className={`tb-laptop ${online ? 'on' : 'off'}`} tabIndex={0} aria-label={online ? 'laptop online' : 'laptop offline'}>
        <span className="dot" aria-hidden />{online ? 'laptop online' : 'laptop offline'}
      </span>
    </Tooltip>
  )
}

/** "Show in Finder" / "Open on this Mac" for a store path (`Users/ryan/x.pdf`), via the
 *  app's `disky://` links; a directory gets only the first. Shown on a Mac — in the app's
 *  window plainly, in a browser with a note that it needs the app. */
export function AppActions({ path, dir = false }: { path: string; dir?: boolean }) {
  if (!onMac(navigator, window)) return null
  const note = inApp(navigator, window) ? null : ' (needs the disky app on this Mac)'
  return (
    <span className="app-actions">
      <Tooltip content={<>Select it in a Finder window{note}</>}>
        <a href={REVEAL_URL(path)}>Show in Finder</a>
      </Tooltip>
      {!dir && (
        <Tooltip content={<>Open it in its default app; anything that can run code asks first{note}</>}>
          <a href={OPEN_FILE_URL(path)}>Open on this Mac</a>
        </Tooltip>
      )}
    </span>
  )
}
