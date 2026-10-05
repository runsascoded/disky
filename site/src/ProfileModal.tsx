// "Profile…" in the user menu: the package's `<ProfilePanel>` (display name +
// face — a GitHub/Bluesky/Mastodon profile, an image address, an upload, or
// Gravatar), saved via `PUT /api/auth/profile` and copied server-side. The
// header chip reads it back through whoami's `subject`. Google sign-in seeds
// it from the account's `picture` the first time, when it can; this is how a
// person sets or replaces it.
import { useEffect } from 'react'
import { ProfilePanel, useWhoami } from '@open-athena/auth/react'
import { WHOAMI_SOURCE, devIdentity } from './auth'

export default function ProfileModal({ onClose }: { onClose: () => void }) {
  const { whoami } = useWhoami(WHOAMI_SOURCE, { devIdentity: devIdentity() })
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="token-backdrop" onClick={onClose}>
      <div className="token-modal profile-modal" onClick={e => e.stopPropagation()} role="dialog" aria-label="Profile">
        <div className="token-head">
          <strong>Profile</strong>
          <button className="token-x" type="button" onClick={onClose} aria-label="Close">×</button>
        </div>
        <p className="token-muted">Your name and picture, as this site shows them (the header, and anything you stage or share).</p>
        <ProfilePanel
          whoami={whoami}
          classNames={{
            form: 'pf-form', preview: 'pf-preview', field: 'pf-field', label: 'pf-label', input: 'pf-input', button: 'pf-save', message: 'pf-msg',
            avatar: { row: 'pf-avatar-row', input: 'pf-input', button: 'pf-btn', hint: 'pf-hint' },
          }}
        />
      </div>
    </div>
  )
}
