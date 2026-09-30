import { useState } from 'react'

// User avatars. We show a real photo ONLY when we have an explicit GitHub handle
// (github.com/<handle>.png) — curated in the deployment's identity map. We deliberately do NOT
// guess the handle from the email local-part: a guess that resolves to a
// different person's GitHub would paint the wrong face. No handle → colored
// initial. See identityRegistry.ts / specs/avatar-sources.md.

export const avatarHue = (s: string): number => {
  let h = 0
  for (const c of s) h = (h * 31 + c.codePointAt(0)!) % 360
  return h
}

export { whoToHandle } from './identityRegistry'

// `github` → github.com/<handle>.png (curated, never guessed). `src` → an
// explicit image URL (a share link's `subject.avatar` — Slack/arbitrary, chosen
// by the admin at mint time, so it's as trusted as a curated handle). Either
// falls back to the colored initial if absent or if the image 404s.
export function Avatar({ github, src, name, size = 20 }: { github?: string; src?: string; name: string; size?: number }) {
  const [failed, setFailed] = useState(false)
  const label = name.trim()
  const initial = label ? label[0].toUpperCase() : '?'
  const url = src ?? (github ? `https://github.com/${github}.png?size=${size * 2}` : undefined)
  if (failed || !url) {
    return (
      <span
        className="user-avatar fallback"
        style={{ width: size, height: size, fontSize: Math.round(size * 0.48), background: `hsl(${avatarHue(label)} 55% 42%)` }}
        aria-hidden
      >
        {initial}
      </span>
    )
  }
  return (
    <img
      className="user-avatar"
      src={url}
      width={size}
      height={size}
      alt={label}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  )
}
