import { useEffect, useRef, type FocusEvent } from 'react'
import { useRowSelection as useKbdRowSelection, useRowSelectionKeys as useKbdRowSelectionKeys, useActions, useMode } from 'use-kbd'
import type { ActionConfig, RowSelectionRowProps, UseRowSelectionResult } from 'use-kbd'

// Multi-row selection for every table on the site (the /sweep console, the
// treemap's children table): use-kbd's `useRowSelection` — an anchor/cursor
// range plus pinned keys, resolved against `key(row)` so the selection
// survives paging and sort (the range freezes into pins when the page's rows
// change) — with the site's few conventions on top: rows are `.sel` / `.cur`,
// a click on a link, button or input inside a row doesn't select the row, the
// cursor row scrolls into view, and the header checkbox toggles the page
// without dropping rows pinned on other pages.

export interface RowSelection<T> extends UseRowSelectionResult<T> {
  cursorRow: T | undefined
  /** `<tr ref>` collector so the cursor row scrolls into view. */
  rowRef: (i: number) => (el: HTMLTableRowElement | null) => void
  /** Every row on the page is selected (the header checkbox). */
  pageAll: boolean
  togglePage: () => void
}

export function useRowSelection<T>(pageRows: readonly T[], key: (row: T) => string): RowSelection<T> {
  const sel = useKbdRowSelection(pageRows, key, { cursorClassName: 'cur', selectedClassName: 'sel' })
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([])
  useEffect(() => { rowRefs.current[sel.cursor]?.scrollIntoView({ block: 'nearest' }) }, [sel.cursor])
  const pageAll = pageRows.length > 0 && pageRows.every(sel.isSelected)
  const rowProps = (i: number): RowSelectionRowProps => {
    const p = sel.rowProps(i)
    return { ...p, onClick: e => { if (!(e.target as HTMLElement).closest('a, button, input, select')) p.onClick(e) } }
  }
  return {
    ...sel,
    rowProps,
    cursorRow: pageRows[sel.cursor],
    rowRef: i => el => { rowRefs.current[i] = el },
    pageAll,
    // Per-row toggles rather than `selectPage`, which replaces the pinned set
    // (and so would forget rows selected on other pages); the commit pins the
    // result and drops the cursor the last toggle left behind.
    togglePage: () => {
      pageRows.forEach((r, i) => { if (sel.isSelected(r) === pageAll) sel.toggle(i) })
      sel.commit()
    },
  }
}

/** The count-prefixed moves (`3j`, `5⇧k`, `2↑`, …), as use-kbd's
 *  `useRowSelectionKeys` registers them (the same ids, labels and bindings, so
 *  a user's rebinding still applies), but scoped to the use-kbd mode `mode`. A
 *  global `\d+ j` makes every digit key a sequence prefix: the Map's `1`–`6`
 *  (Color by …) would wait for a following key, and the ShortcutsModal flags
 *  them as conflicts. Scoped, they're live only while the table has focus, and
 *  the modal sees different scopes, so no conflict. */
export function countMoves(id: string, group: string, mode: string, sel: Pick<UseRowSelectionResult<unknown>, 'moveCursor'>): Record<string, ActionConfig> {
  const n = (captures?: number[]) => captures?.[0] ?? 1
  const move = (label: string, bindings: string[], sign: 1 | -1, extend: boolean): ActionConfig =>
    ({ label, group, mode, defaultBindings: bindings, handler: (_e, captures) => sel.moveCursor(sign * n(captures), extend) })
  return {
    [`${id}:up-n`]: move('Up N rows', ['\\d+ k', '\\d+ arrowup'], -1, false),
    [`${id}:down-n`]: move('Down N rows', ['\\d+ j', '\\d+ arrowdown'], 1, false),
    [`${id}:extend-up-n`]: move('Extend up N rows', ['\\d+ shift+k', '\\d+ shift+arrowup'], -1, true),
    [`${id}:extend-down-n`]: move('Extend down N rows', ['\\d+ shift+j', '\\d+ shift+arrowdown'], 1, true),
  }
}

/** Spread on the table's container: it takes focus on a click inside
 *  (`tabIndex` -1: out of the tab order), and focus anywhere within it is what
 *  turns its count-prefixed moves on. */
export interface TableFocus {
  tabIndex: -1
  onFocus: (e: FocusEvent) => void
  onBlur: (e: FocusEvent) => void
}

/** use-kbd's selection bindings (j/k move, ⇧j/⇧k extend, …) under a
 *  ShortcutsModal group, with the site's page toggle on ⇧x in place of
 *  use-kbd's ⌃a select-page (which drops other pages' rows). `id` namespaces
 *  the action ids (`sweep`, `tbl`, …).
 *
 *  The plain moves are global, as before; the count-prefixed ones
 *  (`countMoves`) belong to the mode `<id>:focused`, active while focus is
 *  within the element the returned `TableFocus` is spread on. The mode lets
 *  global keys through (`passthrough`) and leaves Esc alone (`escapeExits`
 *  off), so Esc stays the selection's `clear` / the treemap's drill-up.
 *
 *  Esc is use-kbd's own `clear`, enabled only while something is selected
 *  (use-kbd ≥ dist `ef820a1`: a disabled binding no longer consumes its key,
 *  and toggling `enabled` doesn't re-register), so with nothing selected the
 *  key falls through to the treemap's Esc/Backspace drill-up. */
export function useRowSelectionKeys<T>(sel: RowSelection<T>, id: string, group: string): TableFocus {
  useKbdRowSelectionKeys(sel, { idPrefix: id, group, numeric: false, bindings: { all: false } })
  const focused = useMode(`${id}:focused`, { label: `${group} (focused)`, escapeExits: false })
  useActions({
    [`${id}:toggle-page`]: { label: 'Select / deselect every row on this page', group, defaultBindings: ['shift+x'], handler: sel.togglePage },
    ...countMoves(id, group, focused.id, sel),
  })
  const active = useRef(focused.active)
  active.current = focused.active
  // Running one of these from the omnibar activates the mode with the table unfocused (use-kbd turns an action's
  // mode on to run it): turn it back off, or the Map's digit keys would wait on a sequence again.
  const box = useRef<Element | null>(null)
  const { deactivate } = focused
  useEffect(() => {
    if (focused.active && !box.current?.contains(document.activeElement)) deactivate()
  }, [focused.active, deactivate])
  return {
    tabIndex: -1,
    onFocus: e => { box.current = e.currentTarget; if (!active.current) focused.activate() },
    // Only when focus leaves the container (not moving between its rows' controls), and only this mode.
    onBlur: e => { if (active.current && !e.currentTarget.contains(e.relatedTarget as Node | null)) focused.deactivate() },
  }
}
