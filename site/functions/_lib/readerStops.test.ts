import { expect, it } from 'vitest'
import { ivMarkBroken } from './index'
import { ReaderStopped } from './readerRuntime'

it('does not quarantine an immutable run when the caller exhausted its own execution budget', () => {
  const stopped = new ReaderStopped('caller deadline')
  let caught: unknown
  try { ivMarkBroken({}, 'deltas/scan-1', stopped) } catch (error) { caught = error }
  expect(caught).toBe(stopped)
})

it('does not quarantine a run when the read failed only because another request it joined has ended', () => {
  const ended = new Error('This promise was created by a request that has since ended, so it can never settle.')
  let caught: unknown
  try { ivMarkBroken({}, 'deltas/scan-1', ended) } catch (error) { caught = error }
  expect(caught).toBe(ended)
})
