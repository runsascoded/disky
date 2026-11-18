import { expect, it } from 'vitest'
import { pinTop } from './stickyHead'

// A 30px header under a bar whose bottom is at 48px, its table ending at `tableBottom`.
const at = (headTop: number, tableBottom: number) => pinTop({ pin: 48, headTop, headH: 30, tableBottom })

it('pins the copy at the bar once the real header slides under it, pushed up by the table\'s end, gone past it', () => {
  expect([
    at(200, 900), // header in view: no copy
    at(48, 900),  // header exactly at the bar: still in view
    at(47, 900),  // starting under the bar: pinned
    at(-500, 900),
    at(-500, 78), // the table's end reaches the copy's bottom: still at the pin
    at(-500, 60), // pushed up with the table's end
    at(-500, 48), // the table has scrolled past the bar: no copy
  ]).toEqual([null, null, 48, 48, 48, 30, null])
})

it('shows nothing for a header with no height (a collapsed `<details>`, `display: none`)', () => {
  expect(pinTop({ pin: 48, headTop: 0, headH: 0, tableBottom: 0 })).toBe(null)
})
