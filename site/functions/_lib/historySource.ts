/** The writer's current scan roster, independent of a reader environment's optional history pin.
 * This reads only interval metadata. Planning rechecks this roster and its manifest fingerprint. */
import type { Env } from './auth.js'
import { storeHeldScans } from './index.js'

export async function historySourceScans(env: Env): Promise<string[]> {
  return storeHeldScans({ ...env, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: undefined,
    INTERVAL_STORE_PREFIX: undefined, HISTORY_CURRENT_KEY: undefined, HISTORY_VERSION_CACHE: undefined })
}
