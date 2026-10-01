import { describe, expect, it } from 'vitest'
import { APP_SCHEME_URL, offerAppLink } from './appLink'

const SAFARI = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
const CHROME_MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
const CHROME_WIN = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
const APP = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) disky/0.2.0'

describe('offerAppLink — desktop Mac browsers only, never inside the app', () => {
  it('decides per browser', () => {
    expect({
      safari: offerAppLink({ userAgent: SAFARI, platform: 'MacIntel', maxTouchPoints: 0 }),
      chromeMac: offerAppLink({ userAgent: CHROME_MAC, platform: 'MacIntel', maxTouchPoints: 0 }),
      noPlatform: offerAppLink({ userAgent: CHROME_MAC }),
      windows: offerAppLink({ userAgent: CHROME_WIN, platform: 'Win32', maxTouchPoints: 0 }),
      iphone: offerAppLink({ userAgent: IPHONE, platform: 'iPhone', maxTouchPoints: 5 }),
      ipad: offerAppLink({ userAgent: SAFARI, platform: 'MacIntel', maxTouchPoints: 5 }),
      appUa: offerAppLink({ userAgent: APP, platform: 'MacIntel', maxTouchPoints: 0 }),
      tauriGlobals: offerAppLink({ userAgent: SAFARI, platform: 'MacIntel', maxTouchPoints: 0 }, { __TAURI_INTERNALS__: {} }),
    }).toEqual({
      safari: true,
      chromeMac: true,
      noPlatform: true,
      windows: false,
      iphone: false,
      ipad: false,
      appUa: false,
      tauriGlobals: false,
    })
  })

  it('wraps the link in the app scheme, percent-encoded', () => {
    expect(APP_SCHEME_URL('https://disk.example/auth/app-link?token=ab_c-1&next=%2Fc'))
      .toBe('disky://open?link=https%3A%2F%2Fdisk.example%2Fauth%2Fapp-link%3Ftoken%3Dab_c-1%26next%3D%252Fc')
  })
})
