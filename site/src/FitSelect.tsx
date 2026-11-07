import { useLayoutEffect, useRef, useState, type ChangeEventHandler, type ReactNode } from 'react'

// A native <select> sized to its *selected* option, not its widest one. The
// several unused characters each native select reserves for its longest option
// are what wrapped the top bar to five rows on a phone ("shade: storage class"
// widening the "none" box, etc.). `appearance:none` + the CSS arrow (app.scss)
// give a known right-padding, so the selected label's text width plus the box
// padding is an exact fit — and it stays a real <select>, so mobile still opens
// the OS picker. `max-width` (app.scss) still caps a long username.
export function FitSelect({ className, value, onChange, ariaLabel, children }: {
  className?: string
  value: string
  onChange: ChangeEventHandler<HTMLSelectElement>
  ariaLabel?: string
  children: ReactNode
}) {
  const ref = useRef<HTMLSelectElement>(null)
  const [w, setW] = useState<number>()
  useLayoutEffect(() => {
    const sel = ref.current
    if (!sel) return
    const cs = getComputedStyle(sel)
    const span = document.createElement('span')
    span.style.cssText = `position:absolute;visibility:hidden;white-space:pre;font:${cs.font};letter-spacing:${cs.letterSpacing}`
    span.textContent = sel.options[sel.selectedIndex]?.text ?? ''
    document.body.appendChild(span)
    const textW = span.getBoundingClientRect().width
    span.remove()
    const box = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight)
      + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth)
    setW(Math.ceil(textW + box + 1))
  }, [value, children])
  return (
    <select ref={ref} className={className} value={value} onChange={onChange} aria-label={ariaLabel} style={{ width: w }}>
      {children}
    </select>
  )
}
