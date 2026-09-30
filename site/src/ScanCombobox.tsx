import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { fmtScan, scanGroups } from './scan'

/**
 * A scan chooser as an ARIA combobox (a filterable listbox), replacing the
 * native `<select>` + `<optgroup>`: the grouped list of every scan is long and
 * a native select can't be typed-to-filtered by its human label. Follows the
 * APG combobox-with-listbox pattern — a button shows the current scan; opening
 * reveals a filter input (`role="combobox"`, `aria-activedescendant`) over a
 * `role="listbox"` grouped by day. Arrow keys move the active option, Enter
 * selects, Esc closes, typing filters against the rendered label ("8/19 6:08a").
 */
export function ScanCombobox({ value, scans, onChange, label, className }: {
  value: string
  scans: string[]
  onChange: (id: string) => void
  label: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [activeId, setActiveId] = useState<string | null>(value)
  const wrapRef = useRef<HTMLDivElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const baseId = useId()
  const listId = `${baseId}-list`
  const optId = (id: string) => `${baseId}-opt-${id.replace(/[^a-zA-Z0-9]/g, '')}`

  // Filter over the human label (what the reader sees), so typing "6:08" or
  // "8/19" narrows the list; empty query shows everything.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? scans.filter(id => fmtScan(id).toLowerCase().includes(q)) : scans
  }, [scans, query])
  const groups = useMemo(() => scanGroups(filtered), [filtered])

  const close = useCallback((focusBtn = true) => {
    setOpen(false)
    setQuery('')
    if (focusBtn) btnRef.current?.focus()
  }, [])

  const commit = useCallback((id: string) => {
    onChange(id)
    close()
  }, [onChange, close])

  // Open: seed the active option with the current value (or the first row) and
  // focus the filter input.
  useEffect(() => {
    if (!open) return
    setActiveId(scans.includes(value) ? value : scans[0] ?? null)
    const t = setTimeout(() => inputRef.current?.focus(), 0)
    return () => clearTimeout(t)
  }, [open, value, scans])

  // Keep the active option in view as it changes.
  useEffect(() => {
    if (!open || !activeId) return
    listRef.current?.querySelector(`#${CSS.escape(optId(activeId))}`)?.scrollIntoView({ block: 'nearest' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, activeId])

  // If the query narrows past the active option, move it to the first match.
  useEffect(() => {
    if (open && activeId && !filtered.includes(activeId)) setActiveId(filtered[0] ?? null)
  }, [open, filtered, activeId])

  // Close on an outside pointer-down.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) close(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open, close])

  const move = (delta: number) => {
    if (!filtered.length) return
    const i = activeId ? filtered.indexOf(activeId) : -1
    const next = Math.max(0, Math.min(filtered.length - 1, (i < 0 ? (delta > 0 ? 0 : filtered.length - 1) : i + delta)))
    setActiveId(filtered[next])
  }

  const onInputKey = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); move(1); break
      case 'ArrowUp': e.preventDefault(); move(-1); break
      case 'Home': e.preventDefault(); setActiveId(filtered[0] ?? null); break
      case 'End': e.preventDefault(); setActiveId(filtered[filtered.length - 1] ?? null); break
      case 'Enter': e.preventDefault(); if (activeId) commit(activeId); break
      case 'Escape': e.preventDefault(); close(); break
      case 'Tab': close(false); break
    }
  }

  return (
    <div ref={wrapRef} className={`scan-picker${className ? ` ${className}` : ''}`}>
      <button
        ref={btnRef}
        type="button"
        className="tb-select scan sp-trigger"
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        onKeyDown={e => {
          if (!open && (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setOpen(true) }
        }}
      >
        <span className="sp-value">{fmtScan(value)}</span>
        <span className="sp-caret" aria-hidden>▾</span>
      </button>
      {open && (
        <div className="sp-pop" role="dialog" aria-label={label}>
          <input
            ref={inputRef}
            type="text"
            className="sp-filter"
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={activeId ? optId(activeId) : undefined}
            aria-autocomplete="list"
            placeholder="filter scans…"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onInputKey}
          />
          <ul ref={listRef} className="sp-list" role="listbox" id={listId} aria-label={label}>
            {groups.length === 0 && <li className="sp-empty" role="presentation">no scans match</li>}
            {groups.map(g => (
              <li key={g.day} role="presentation" className="sp-group">
                <div className="sp-day" role="presentation">{g.day}</div>
                <ul role="presentation">
                  {g.scans.map(s => (
                    <li
                      key={s.id}
                      id={optId(s.id)}
                      role="option"
                      aria-selected={s.id === value}
                      className={'sp-opt' + (s.id === activeId ? ' active' : '') + (s.id === value ? ' sel' : '')}
                      // pointer-down (not click) so the outside-close handler
                      // doesn't fire first and swallow the selection.
                      onMouseDown={e => { e.preventDefault(); commit(s.id) }}
                      onMouseEnter={() => setActiveId(s.id)}
                    >
                      {s.label}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
