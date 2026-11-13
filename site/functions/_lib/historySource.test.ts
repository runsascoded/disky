import { describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { historySourceScans } from './historySource'
import { storeHeldScans } from './index'
vi.mock('./index', () => ({ storeHeldScans: vi.fn() }))

describe('history planning source metadata', () => {
  it('reads writer metadata without inheriting reader generation, namespace or release pins', async () => {
    vi.mocked(storeHeldScans).mockResolvedValueOnce(['2026-11-01', '2026-11-02'])
    const env: Env = { PATH_STORE: 'opt-in', INTERVAL_STORE_GEN: 'reader', INTERVAL_STORE_PREFIX: 'reader-prefix',
      HISTORY_CURRENT_KEY: 'history/environments/dev/CURRENT.json', HISTORY_VERSION_CACHE: { key: 'history/environments/dev/CURRENT.json', store: 'primary', pointer: null, version: null },
      STATIC_GEN: 'reader-static', STORE_KEY: 'primary' }
    expect(await historySourceScans(env)).toEqual(['2026-11-01', '2026-11-02'])
    expect(vi.mocked(storeHeldScans).mock.calls).toEqual([[{ ...env, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: undefined,
      INTERVAL_STORE_PREFIX: undefined, HISTORY_CURRENT_KEY: undefined, HISTORY_VERSION_CACHE: undefined }]])
    expect(env.INTERVAL_STORE_GEN).toEqual('reader')
  })
})
