import { describe, expect, it } from 'vitest'
import { colorModeCodec, mapColorMode } from './colorMode'

describe('shared map/preview color selection', () => {
  it('canonicalizes every accepted alias and retains unknown URL values for fallback', () => {
    expect(['t', 'tree', 'w', 'written', 'age', 'date', 'r', 'read', 'u', 'user', 'invalid', undefined]
      .map(c => colorModeCodec.encode(colorModeCodec.decode(c))))
      .toEqual(['t', 't', 'w', 'w', 'w', 'w', 'r', 'r', 'u', 'u', 'invalid', undefined])
  })

  it('explicit selections win; unsupported axes use the same available-axis fallback', () => {
    expect([
      mapColorMode(undefined, ['date', 'tree'], 'date'),
      mapColorMode('t', ['date', 'tree'], 'date'),
      mapColorMode('r', ['user', 'date', 'tree'], 'date'),
      mapColorMode('u', ['date', 'tree'], 'date'),
      mapColorMode('invalid', ['date', 'tree'], 'date'),
    ]).toEqual(['date', 'tree', 'user', 'tree', 'date'])
  })
})
