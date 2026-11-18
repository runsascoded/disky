// Sticky table headers, site-wide: once a table's header row scrolls up under the
// top bar, a copy of it is pinned just below the bar until the table itself
// scrolls past (and is pushed up with the table's end, as CSS `position: sticky`
// would be).
//
// Plain `position: sticky` on `th` can't do this here: nearly every table sits in
// an `overflow-x: auto` box (so it scrolls sideways on a phone, not the page), and
// sticky only pins against the nearest scrolling ancestor, which is that box. So
// the pinned header is a clone of the real `thead`, in a `position: fixed` holder
// beside the table (fixed escapes the box's clipping, and staying in place keeps
// the page's scoped styles applying to it). The holder is clipped to the box, its
// cells take the real cells' widths, and its table is shifted by the box's
// horizontal scroll, so columns line up while the table is scrolled sideways.
//
// No React state: one shared, rAF-coalesced listener (scroll, captured, so a box's
// sideways scroll counts too; resize) reads each table's rect and writes the
// holder's transform only when it changes. A `MutationObserver` on the real header
// re-clones it; a `ResizeObserver` re-measures widths. Clicks on the copy are
// forwarded to the matching real element (sort headers, the select-page box).
import { createElement, useCallback, type RefCallback, type TableHTMLAttributes } from 'react'

/** Where the pinned copy goes, from rects in viewport px: `y` is its top (the pin
 *  line, or above it while the table's end pushes it up), `null` = not shown (the
 *  real header is in view, or the table has scrolled past the pin line). */
export function pinTop({ pin, headTop, headH, tableBottom }: {
  /** The pin line: the top bar's bottom (or an enclosing pinned header's). */
  pin: number
  /** The real header row's top. */
  headTop: number
  headH: number
  tableBottom: number
}): number | null {
  if (headH <= 0 || headTop >= pin || tableBottom <= pin) return null
  return Math.min(pin, tableBottom - headH)
}

interface Inst {
  table: HTMLTableElement
  holder: HTMLDivElement
  copy: HTMLTableElement
  /** The nearest horizontally clipping ancestor (the `overflow-x: auto` box), if any. */
  clip: HTMLElement | null
  /** The pinned header of an enclosing sticky table (a table nested in a row). */
  outer: HTMLTableElement | null
  dirty: boolean
  /** The pinned copy's height, measured when built. */
  h: number
  /** Real / copied checkboxes, whose `checked` React sets as a property (no mutation to observe). */
  inputs: [HTMLInputElement, HTMLInputElement][]
  last: string
  mo: MutationObserver
  ro: ResizeObserver
}

const insts = new Map<HTMLTableElement, Inst>()
let raf = 0

/** `v` CSS px snapped (by `snap`) to the device pixel grid. */
const px = (v: number, snap: (x: number) => number) => snap(v * devicePixelRatio) / devicePixelRatio

const schedule = () => { if (!raf) raf = requestAnimationFrame(frame) }

/** The top bar's visible bottom: its sticky row, or the phone controls overlay under it while shown. */
function barBottom(): number {
  const bar = document.querySelector<HTMLElement>('.topbar')
  if (!bar) return 0
  const row2 = bar.querySelector<HTMLElement>('.tb-row2')
  return Math.max(bar.getBoundingClientRect().bottom, row2 ? row2.getBoundingClientRect().bottom : 0)
}

function frame() {
  raf = 0
  const bar = barBottom()
  // Per frame: each table's pinned bottom, so a nested table pins under its enclosing table's copy.
  const done = new Map<HTMLTableElement, number>()
  const place = (inst: Inst): number => {
    const memo = done.get(inst.table)
    if (memo !== undefined) return memo
    const outer = inst.outer && insts.get(inst.outer)
    const pin = outer ? place(outer) : bar
    const head = inst.table.tHead
    const t = inst.table.getBoundingClientRect()
    const h = head?.getBoundingClientRect()
    // The copy's own height (its rule under it included) once built, so the table's end pushes all of it up.
    const y = h && h.height > 0 ? pinTop({ pin, headTop: h.top, headH: inst.h || h.height, tableBottom: t.bottom }) : null
    const s = inst.holder.style
    if (!h || y === null) {
      if (inst.last !== 'off') { s.display = 'none'; inst.last = 'off' }
      done.set(inst.table, pin)
      return pin
    }
    if (inst.last === 'off') s.display = ''
    if (inst.dirty) rebuild(inst)
    for (const [real, copy] of inst.inputs) {
      if (copy.checked !== real.checked) copy.checked = real.checked
      if (copy.indeterminate !== real.indeterminate) copy.indeterminate = real.indeterminate
    }
    const c = inst.clip?.getBoundingClientRect()
    // The holder on whole device pixels (its top rounded toward the bar): at a fractional
    // offset it composites resampled, and rows show through its edges.
    const left = px(c && inst.clip ? c.left + inst.clip.clientLeft : t.left, Math.round)
    const top = px(Math.min(y, t.bottom - inst.h), Math.floor)
    const width = c && inst.clip ? inst.clip.clientWidth : t.width
    const key = `${left},${top},${width},${t.left - left}`
    if (key !== inst.last) {
      s.transform = `translate(${left}px, ${top}px)`
      s.width = `${width}px`
      inst.copy.style.transform = `translateX(${t.left - left}px)`
      inst.last = key
    }
    const bottom = top + inst.h
    done.set(inst.table, bottom)
    return bottom
  }
  for (const inst of insts.values()) place(inst)
}

/** Re-copy the real header and give each copied cell its real cell's width. */
function rebuild(inst: Inst) {
  inst.dirty = false
  const { table, copy } = inst
  const head = table.tHead
  if (!head) return
  copy.className = table.className
  const clone = head.cloneNode(true) as HTMLTableSectionElement
  // The copy must not duplicate ids, join a radio group, or relabel a real input.
  for (const el of clone.querySelectorAll('[id], [name], [for]')) {
    el.removeAttribute('id'); el.removeAttribute('name'); el.removeAttribute('for')
  }
  for (const el of clone.querySelectorAll<HTMLElement>('a, button, input, select, textarea, [tabindex]')) el.tabIndex = -1
  const real = [...head.querySelectorAll<HTMLTableCellElement>('th, td')]
  const widths = real.map(c => c.getBoundingClientRect().width)
  clone.querySelectorAll<HTMLTableCellElement>('th, td').forEach((c, i) => {
    const w = `${widths[i]}px`
    c.style.width = w; c.style.minWidth = w; c.style.maxWidth = w; c.style.boxSizing = 'border-box'
  })
  copy.style.width = `${table.getBoundingClientRect().width}px`
  copy.style.margin = '0'
  copy.replaceChildren(clone)
  const realInputs = head.querySelectorAll<HTMLInputElement>('input')
  const copyInputs = clone.querySelectorAll<HTMLInputElement>('input')
  inst.inputs = [...realInputs].map((r, i) => [r, copyInputs[i]])
  inst.h = inst.holder.getBoundingClientRect().height
  inst.last = ''
}

/** Column widths follow the real header cells (and the table's own width). Observed
 *  here, never in `rebuild`: a fresh `observe` reports at once, which would re-dirty it. */
function observeSizes({ table, ro }: Inst) {
  ro.disconnect()
  ro.observe(table)
  for (const c of table.tHead?.querySelectorAll('th, td') ?? []) ro.observe(c)
}

/** The element in `to` at the same child-index path as `node` has under `from`. */
function counterpart(node: Node, from: Node, to: Node): Node | null {
  const path: number[] = []
  for (let n: Node | null = node; n && n !== from; n = n.parentNode) {
    const p = n.parentNode
    if (!p) return null
    path.push([...p.childNodes].indexOf(n as ChildNode))
  }
  let m: Node | null = to
  for (const i of path.reverse()) m = m?.childNodes[i] ?? null
  return m
}

function clipOf(table: HTMLTableElement): HTMLElement | null {
  for (let el = table.parentElement; el && el !== document.body; el = el.parentElement) {
    if (getComputedStyle(el).overflowX !== 'visible') return el
  }
  return null
}

let listening = false
function listen() {
  if (listening) return
  listening = true
  document.addEventListener('scroll', schedule, { capture: true, passive: true })
  window.addEventListener('resize', schedule, { passive: true })
  // The phone controls overlay slides in and out under the bar after a scroll stops.
  document.addEventListener('transitionend', schedule, { passive: true })
}

/** Pin `table`'s header under the top bar while it's scrolled past; returns the detach. */
export function stickyHead(table: HTMLTableElement): () => void {
  const holder = document.createElement('div')
  holder.className = 'sticky-head'
  holder.setAttribute('aria-hidden', 'true')
  holder.style.display = 'none'
  const copy = document.createElement('table')
  copy.className = table.className
  const style = table.getAttribute('style')
  if (style) copy.setAttribute('style', style)
  holder.append(copy)
  table.after(holder)
  const inst: Inst = {
    table, holder, copy,
    clip: clipOf(table),
    outer: table.parentElement?.closest('table') ?? null,
    dirty: true,
    h: 0,
    inputs: [],
    last: 'off',
    mo: new MutationObserver(() => { observeSizes(inst); inst.dirty = true; schedule() }),
    ro: new ResizeObserver(() => { inst.dirty = true; schedule() }),
  }
  if (table.tHead) inst.mo.observe(table.tHead, { subtree: true, childList: true, characterData: true, attributes: true })
  observeSizes(inst)
  const onClick = (e: MouseEvent) => {
    // Never let a click on the copy reach React's root listener: the copy has no
    // fibers, so React would hand it to the nearest real ancestor's handlers.
    e.stopPropagation()
    e.preventDefault()
    const head = table.tHead
    const from = copy.tHead
    if (!head || !from) return
    const real = counterpart(e.target as Node, from, head)
    if (!(real instanceof HTMLElement)) return
    real.click()
    inst.dirty = true
    schedule()
  }
  holder.addEventListener('click', onClick)
  // A row checkbox (or anything else in the table) changing the header's checkboxes.
  table.addEventListener('change', schedule)
  insts.set(table, inst)
  listen()
  schedule()
  return () => {
    insts.delete(table)
    inst.mo.disconnect()
    inst.ro.disconnect()
    table.removeEventListener('change', schedule)
    holder.remove()
  }
}

/** A callback ref for a `<table>` whose header should pin under the top bar. */
export function useStickyHead(): RefCallback<HTMLTableElement> {
  return useCallback((table: HTMLTableElement | null) => table ? stickyHead(table) : undefined, [])
}

/** A box of its own height (`max-height`) scrolls vertically itself: a table in one keeps the
 *  box's own sticky header (`top: 0`), never a copy pinned to the page. */
function inOwnScrollBox(table: HTMLTableElement, root: HTMLElement): boolean {
  for (let el = table.parentElement; el && el !== root; el = el.parentElement) {
    if (getComputedStyle(el).maxHeight !== 'none') return true
  }
  return false
}

/** A callback ref for a container whose tables come from code we can't put a ref in (a
 *  package's component): every `<table>` with a header under it pins, as tables come and go. */
export function useStickyHeads(): RefCallback<HTMLElement> {
  return useCallback((root: HTMLElement | null) => {
    if (!root) return undefined
    const attached = new Map<HTMLTableElement, () => void>()
    const scan = () => {
      for (const [table, detach] of attached) if (!root.contains(table)) { detach(); attached.delete(table) }
      for (const table of root.querySelectorAll('table')) {
        if (attached.has(table) || !table.tHead || table.closest('.sticky-head') || inOwnScrollBox(table, root)) continue
        attached.set(table, stickyHead(table))
      }
    }
    // The holders' own mutations rescan too, harmlessly: the copies in them are skipped by `closest`.
    const mo = new MutationObserver(scan)
    mo.observe(root, { childList: true, subtree: true })
    scan()
    return () => {
      mo.disconnect()
      for (const detach of attached.values()) detach()
    }
  }, [])
}

/** A `<table>` whose header pins under the top bar (`useStickyHead`); props pass through. */
export function StickyTable(props: TableHTMLAttributes<HTMLTableElement>) {
  return createElement('table', { ...props, ref: useStickyHead() })
}
