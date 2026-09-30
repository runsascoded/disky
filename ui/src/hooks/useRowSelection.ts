import { useEffect, useRef } from 'react'
import { useRowSelection as useKbdRowSelection, useRowSelectionKeys as useKbdRowSelectionKeys } from 'use-kbd'
import type { RowSelectionRowProps, UseRowSelectionResult } from 'use-kbd'

// Multi-row selection for the scan-details table: use-kbd's `useRowSelection` —
// an anchor/cursor range plus pinned keys, resolved against `key(row)` so the
// selection survives paging and sort — with a few table conventions on top:
// rows are `.sel` / `.cur`, a click on a link/button/input inside a row doesn't
// select the row, the cursor row scrolls into view, and the header checkbox
// toggles the page without dropping rows pinned on other pages.
//
// Replaces a ~150-line hand-rolled anchor/cursor/pinned model (plus six
// `useAction` blocks) that predated this primitive landing in use-kbd. This thin
// wrapper is nearly identical to marin-gcs-usage's `site/src/rowSelection.ts`;
// both are candidates to move into use-kbd itself so neither app copies it.

export interface RowSelection<T> extends UseRowSelectionResult<T> {
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

/** use-kbd's selection bindings under the table's ShortcutsModal groups, with
 *  the same keys the hand-rolled model used (j/k + arrows move, ⇧ extends, meta+a
 *  selects the page, Esc clears) plus use-kbd's first/last/numeric variants. */
export function useRowSelectionKeys<T>(sel: RowSelection<T>) {
  useKbdRowSelectionKeys(sel, {
    idPrefix: 'table',
    group: 'Table: Navigation',
    selectionGroup: 'Table: Selection',
    bindings: {
      up: ['k', 'arrowup'],
      down: ['j', 'arrowdown'],
      'extend-up': ['shift+k', 'shift+arrowup'],
      'extend-down': ['shift+j', 'shift+arrowdown'],
      all: ['meta+a'],
      clear: ['escape'],
    },
  })
}
