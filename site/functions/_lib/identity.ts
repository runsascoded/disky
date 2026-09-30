/** Canonical user ids server-side — the same rule the client applies
 * (`src/UserChip.tsx`): sanitize an actor string (email or raw id) to a
 * handle, then follow the deployment's registry (built from its published
 * `snapshots/rules.json`, as the client does) to the canonical id. */
import { S3Store } from '@rdub/file-tree/stores/s3'
import { type Registry, buildRegistry, canonIn } from '../../src/identityRegistry.js'
import type { Rules } from '../../src/types.js'
import type { Env } from './auth.js'
import { storeCreds, storeReady, storeTarget } from './index.js'
import { shared } from './shared.js'

export { whoToHandle } from '../../src/identityRegistry.js'

export const canonId = (who: string, reg: Registry = {}): string => canonIn(reg, who)

// Rules change daily at most: rebuild at most every 10 min per isolate.
const TTL_MS = 10 * 60_000
const memo = new Map<string, Promise<Registry>>()

/** The deployment's registry, or `{}` when its store publishes no rules (the
 * public demo, a plan-first store): ids then stand as they are. */
export async function loadRegistry(env: Env): Promise<Registry> {
  if (!storeReady(env)) return {}
  const key = `rules:${Math.floor(Date.now() / TTL_MS)}`
  for (const k of memo.keys()) if (k !== key) memo.delete(k)
  return shared(memo, key, async () => {
    const store = S3Store({ ...storeTarget(env), prefixes: ['snapshots/'], ...storeCreds(env) })
    try {
      const { bytes } = await store.get('snapshots/rules.json')
      const rules = JSON.parse(new TextDecoder().decode(bytes)) as Rules
      return buildRegistry(rules.users)
    } catch (e) {
      if ((e as Error).name === 'NotFoundError') return {}
      throw e
    }
  }, 10_000)
}
