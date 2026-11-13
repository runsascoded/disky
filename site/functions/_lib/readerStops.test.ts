import { expect, it } from 'vitest'
import { ivMarkBroken } from './index'
import { ReaderStopped } from './readerRuntime'

it('does not quarantine an immutable run when the caller exhausted its own execution budget', () => {
  const stopped = new ReaderStopped('caller deadline')
  let caught: unknown
  try { ivMarkBroken({}, 'deltas/scan-1', stopped) } catch (error) { caught = error }
  expect(caught).toBe(stopped)
})
