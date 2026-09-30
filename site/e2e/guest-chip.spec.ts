import { expect, test } from '@playwright/test'

// The header chip for a share-link (guest) session must show the *subject* the
// link was minted with — the custom name + avatar — not an owner-registry lookup
// (an external guest isn't in the registry, so that path fell back to an
// email-derived initial + a "ping Ryan" warning: the exact bug this guards).
//
// We mock `/api/auth/whoami` with a grant-shaped payload (what the real route
// returns: `json(gate.whoami(auth))`, read verbatim by `useWhoami`) so the test
// is deterministic and needs no token or fixture grant. The full mint→redeem
// path against a seeded local DB is the Tier-2 follow-up (specs/local-db-dev-mode.md).

// A 1×1 transparent PNG: a data URL always loads, so the <img> never falls back
// to the initial — the assertion is on the rendered src, not on the network.
const AVATAR =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='

const GUEST_WHOAMI = {
  kind: 'grant',
  id: 'test-grant',
  name: null,
  subject: { first: 'Rob Howley', last: null, email: 'howley.robert@gmail.com', avatar: AVATAR },
  email: 'howley.robert@gmail.com',
  scopes: ['gcs:read'],
  admin: false,
  expiresAt: null,
}

test('guest chip shows the grant subject (avatar + name), not registry initials', async ({ page }) => {
  await page.route('**/api/auth/whoami', route => route.fulfill({ json: GUEST_WHOAMI }))
  await page.goto('/')

  // The chip: an <img> avatar (the subject's), not the colored-initial fallback.
  const chip = page.locator('button.tb-avatar')
  await expect(chip).toHaveAttribute('aria-label', 'Signed in as Rob Howley')
  const avi = chip.locator('img.user-avatar')
  await expect(avi).toHaveAttribute('src', AVATAR)
  await expect(avi).toHaveAttribute('alt', 'Rob Howley')
  await expect(chip.locator('span.user-avatar.fallback')).toHaveCount(0)

  // The menu card: the subject name + bound email + a plain "guest share link"
  // tag — and none of the SSO/owner framing (no registry warning, no estate link).
  await chip.click()
  const card = page.locator('.user-menu .user-card')
  await expect(card.locator('.uc-head b')).toHaveText('Rob Howley')
  await expect(card.locator('.uc-sub')).toHaveText('howley.robert@gmail.com')
  await expect(card.getByText('guest share link')).toBeVisible()
  await expect(card.locator('.uc-warn')).toHaveCount(0)
  await expect(card.getByText('storage breakdown')).toHaveCount(0)
})
