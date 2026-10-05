import type { Store } from './stores'

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** A dir prefix in one of the store's buckets: `<scheme><bucket>/<path>/`. A
 * store that lists no buckets admits any bucket name. */
export function prefixPattern(store: Pick<Store, 'scheme' | 'buckets'>): RegExp {
  const bucket = store.buckets.length ? `(?:${store.buckets.map(esc).join('|')})` : '[^/\\s]+'
  return new RegExp(`^${esc(store.scheme)}${bucket}\\/(?:[^\\s]*\\/)?$`)
}
