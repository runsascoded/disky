import { useEffect, useState } from 'react'

/** A button that asks once more before it acts: the first click arms it (its label becomes `confirm`), the second acts. It
 * disarms on its own after 15 s, long enough to read the prompt. No browser dialogs. */
export function ArmedButton({ label, confirm, onClick, disabled, className }: {
  label: string
  confirm: string
  onClick: () => void
  disabled?: boolean
  className?: string
}) {
  const [armed, setArmed] = useState(false)
  useEffect(() => { if (armed) { const t = setTimeout(() => setArmed(false), 15000); return () => clearTimeout(t) } }, [armed])
  const cls = [className, armed && 'armed'].filter(Boolean).join(' ') || undefined
  return <button type="button" disabled={disabled} className={cls} onClick={() => (armed ? (setArmed(false), onClick()) : setArmed(true))}>{armed ? confirm : label}</button>
}
