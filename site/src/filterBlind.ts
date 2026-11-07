/** The page's panels that don't follow the map's name filter (`f=`): the creation-date histogram (its age index
 *  is per path, not per match), the storage-class mix and the ownership share (a filtered view carries neither
 *  for its matches: a static answer has no class split, a rollup no owners), and the buckets' lifecycle rules.
 *  One treatment for all of them: hidden while a filter is set, with one muted line in their place naming them —
 *  never an unfiltered panel under a filtered map. */
export type BlindPanel = 'age' | 'classes' | 'ownership' | 'lifecycle'

const NAMES: Record<BlindPanel, string> = {
  age: 'Bytes by creation date',
  classes: 'storage classes',
  ownership: 'ownership',
  lifecycle: 'bucket lifecycle',
}
const ORDER: BlindPanel[] = ['age', 'lifecycle', 'classes', 'ownership']

/** Whether a panel shows: never under a filter. */
export const blindShown = (filtered: boolean): boolean => !filtered

/** The muted line in the hidden panels' place (those the page would show — `present`), or null: no filter, or
 *  none of them here. */
export function blindNote(filtered: boolean, present: readonly BlindPanel[]): string | null {
  if (!filtered) return null
  const names = ORDER.filter(p => present.includes(p)).map(p => NAMES[p])
  if (!names.length) return null
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
  const cap = list[0].toUpperCase() + list.slice(1)
  return `${cap} ${names.length === 1 ? 'doesn’t' : 'don’t'} follow the name filter — hidden while it is set.`
}
