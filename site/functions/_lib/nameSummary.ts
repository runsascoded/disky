/** `/api/name-summary` and `/api/name-summary-registry`: request validation shared by both routes. The static name
 *  index (`nameSummaryStatic.ts`) answers them; the routes are on with `NAME_SUMMARY_STATIC=1` and `INDEX_R2`. */
import type { Env } from './auth.js'
import { datedNameRequest } from '../../src/nameModel.js'
import { isScanId } from '../../src/scanSlug.js'
import { type StaticNameEnv, staticEnabled } from './nameSummaryStatic.js'

export type NameSummaryEnv = Env & StaticNameEnv
export const namesEnabled = (env: NameSummaryEnv): boolean => staticEnabled(env)

export class NameQueryError extends Error {}

/** URLSearchParams tolerates malformed UTF-8; decode strictly before constructing it. */
export function nameParams(url: URL): URLSearchParams {
  const parts = url.search.slice(1).split('&')
  if (!url.search || parts.length > 3) throw new NameQueryError('Use date, name and optional from only.')
  const params = new URLSearchParams()
  for (const part of parts) {
    const equals = part.indexOf('=')
    if (equals < 0) throw new NameQueryError('Invalid query parameters.')
    let key: string, value: string
    try {
      key = decodeURIComponent(part.slice(0, equals).replace(/\+/g, ' '))
      value = decodeURIComponent(part.slice(equals + 1).replace(/\+/g, ' '))
    } catch {
      throw new NameQueryError('Invalid query parameters.')
    }
    if (!['date', 'name', 'from'].includes(key)) throw new NameQueryError('Use date, name and optional from only.')
    if (params.has(key)) throw new NameQueryError('Duplicate query parameter.')
    params.set(key, value)
  }
  const date = params.get('date') ?? '', name = params.get('name') ?? '', from = params.get('from')
  if (!isScanId(date)) throw new NameQueryError('A valid scan date is required.')
  if (!name || name.includes('/') || name.includes('\0') || Array.from(name).length > 512) {
    throw new NameQueryError('Use one nonempty NUL/slash-free literal of at most 512 characters.')
  }
  if (from !== null && (!isScanId(from) || from >= date)) throw new NameQueryError('from must be a valid earlier scan date.')
  return params
}

export function nameSummaryParams(url: URL): URLSearchParams {
  const params = nameParams(url)
  try { datedNameRequest(params) } catch (error) { throw new NameQueryError((error as Error).message) }
  return params
}

export function nameRegistryParams(url: URL): void {
  if (url.search) throw new NameQueryError('The name-summary scan registry accepts no query parameters.')
}
