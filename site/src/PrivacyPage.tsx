import { Link } from 'react-router-dom'
import { DEFAULT_STORE } from './stores'

// `/privacy` — the public privacy notice Google requires on a consent screen
// published to production (an External audience can't leave "Testing" without
// one). Deployment-agnostic: it describes what `@open-athena/auth` keeps (a
// session, the sign-in identity, an access log), which is the same on every
// store. Public on purpose (no <AuthGate>): Google reads it, and so does anyone
// deciding whether to sign in.
export function PrivacyPage() {
  const { title } = DEFAULT_STORE
  return (
    <div className="authwall">
      <div className="card prose privacy">
        <h1>Privacy</h1>
        <p>{title} is a private dashboard. This page says what it keeps about the people who sign in.</p>
        <h2>What is collected</h2>
        <p>
          <b>Sign-in identity.</b> Signing in with Google gives the site your email address, and, the first time, your name and
          profile picture. The picture is copied once and stored as a small image; the site never links to Google's copy.
          Signing in with an emailed code gives it your email address only.
        </p>
        <p>
          <b>A session cookie</b>, so you stay signed in. It holds no personal data beyond a signed session id.
        </p>
        <p>
          <b>An access log</b>: which pages a signed-in identity or share link opened, and when. It exists so the person who
          shared a link can see it was used, and to revoke it.
        </p>
        <h2>What is not collected</h2>
        <p>
          No analytics, advertising or tracking scripts. No data is sold or shared with third parties. Google is used only to
          confirm who you are at sign-in; the site requests the basic <code>openid email profile</code> scopes and nothing else.
        </p>
        <h2>Removal</h2>
        <p>
          Signing out ends the session. To have your identity, profile and log entries removed, write to the support address on
          the Google consent screen; removal is a row delete, done on request.
        </p>
        <p><Link to="/">← back</Link></p>
      </div>
    </div>
  )
}
