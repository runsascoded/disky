import { expect, it } from 'vitest'
import { DAY_S, expDay } from './sign'
import { fullTier, mint, NEVER_DAY, tokenDays, TOKEN_DAYS_DEFAULT } from './tokens'
import { pageView } from './routes'

const now = Date.UTC(2026, 9, 13) / 1000

async function minted(days: number, token: string) {
  const { sqliteD1 } = await import('../testD1')
  const { db } = await sqliteD1('cw')
  const m = (await mint(db, 'Demo', new URL('https://demo.example/bucket'), 'demo', now, days, token))!
  const view = pageView(new URL(m.url), 'Demo')!
  return { m, valid: (t: number) => fullTier(db, view.kind, view.params, m.token, t) }
}

it('a request body\'s days: 0 means never, any positive whole number is kept, anything else is the default', () => {
  expect([0, 1, 7.9, 90, 365, 3650, -1, NaN, '30', null, undefined].map(tokenDays))
    .toEqual([0, 1, 7, 90, 365, 3650, TOKEN_DAYS_DEFAULT, TOKEN_DAYS_DEFAULT, TOKEN_DAYS_DEFAULT, TOKEN_DAYS_DEFAULT, TOKEN_DAYS_DEFAULT])
})

it('a token minted for longer than 90 days lasts that long', async () => {
  const { m, valid } = await minted(365, 'LongTokAAA')
  expect([m.expDay, await valid(now + 364 * DAY_S), await valid(now + 366 * DAY_S)])
    .toEqual([expDay(now, 365), { day: expDay(now, 365) }, null])
})

it('a token minted with days 0 never expires', async () => {
  const { m, valid } = await minted(0, 'NeverTokAA')
  expect([m.expDay, await valid(now + 100 * 365 * DAY_S)]).toEqual([NEVER_DAY, { day: NEVER_DAY }])
})
