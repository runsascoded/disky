import type { ColorMode } from './types'

const CODES: Record<ColorMode, string> = { tree: 't', date: 'w', read: 'r', user: 'u' }
const ALIASES: Record<string, ColorMode> = {
  t: 'tree', tree: 'tree',
  w: 'date', written: 'date', age: 'date', date: 'date',
  r: 'read', read: 'read',
  u: 'user', user: 'user',
}

/** One URL codec for the interactive map and its server-rendered preview. */
export const colorModeCodec = {
  encode: (v: string | undefined) => v === undefined ? undefined : CODES[v as ColorMode] ?? v,
  decode: (v: string | undefined) => v === undefined ? undefined : ALIASES[v] ?? v,
}

/** Unsupported axes fall back exactly as the dashboard's color menu does. */
export function mapColorMode(requested: string | undefined, available: ColorMode[], fallback: ColorMode): ColorMode {
  const decoded = colorModeCodec.decode(requested)
  const mode = decoded && Object.hasOwn(CODES, decoded) ? decoded as ColorMode : fallback
  return available.includes(mode) ? mode : available.includes('user') ? 'user' : 'tree'
}
