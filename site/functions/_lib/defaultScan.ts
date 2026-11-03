/** The Functions' side of the default scan (`src/defaultScan.ts`, the one resolver): the deployment's mode
 *  (`DEFAULT_SCAN`) and the scans the static name index covers (`/api/filter-scans`' list, the one `indexedGate`
 *  checks). */
import { defaultScan, defaultScanMode, prefersIndexed, type DefaultScanMode } from '../../src/defaultScan.js'
import { type StaticGenEnv, withStaticGen } from './index.js'
import { staticFilterStore, type StaticFilterEnv } from './staticFilter.js'

/** The deployment's default-scan mode (`DEFAULT_SCAN`: `newest`, unset, or `searchable`). */
export const scanMode = (env: { DEFAULT_SCAN?: string }): DefaultScanMode => defaultScanMode(env.DEFAULT_SCAN)

/** The scans the static name index covers, ascending; null when the static filter is off. */
export async function coveredScans(env: StaticGenEnv): Promise<string[] | null> {
  const s = staticFilterStore(await withStaticGen(env) as unknown as StaticFilterEnv)
  return s ? [...await s.scans()].sort() : null
}

/** The default scan among `scans` (newest first) for a view with no `?d=`, `filtered` when it carries `?f=`: the one
 *  resolver (`src/defaultScan.ts`), with the covered list read only when the view prefers it. */
export async function serverDefaultScan(env: StaticGenEnv & { DEFAULT_SCAN?: string }, scans: readonly string[], filtered: boolean): Promise<string | null> {
  const mode = scanMode(env)
  const covered = prefersIndexed(mode, filtered) ? await coveredScans(env) : null
  return defaultScan(scans, covered, mode, filtered).scan
}
