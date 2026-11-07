import { useState } from 'react'
import type { ReactNode } from 'react'
import { Tooltip } from './Tooltip'

/** An elided name's full text on hover; click copies it to the clipboard. */
export function CopyName({ text, note, children }: { text: string; note?: string; children: ReactNode }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    copyText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200) })
  }
  return (
    <Tooltip content={<><code className="elide-full">{text}</code><span className="copy-hint">{copied ? 'copied ✓' : note ? `${note} · click to copy` : 'click to copy'}</span></>}>
      <span onClick={copy} role="button" tabIndex={-1}>{children}</span>
    </Tooltip>
  )
}

/** Clipboard write that also works off a secure origin (a tailnet dev
 *  server): `navigator.clipboard` is undefined there, so fall back to the
 *  selection-based copy. */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard) return navigator.clipboard.writeText(text)
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  document.execCommand('copy')
  ta.remove()
}

/** Elide a long name from the middle, keeping its tail — a hash or step
 *  number is usually the part that tells siblings apart. */
export function elideMid(s: string, max: number, tail = 10): string {
  if (s.length <= max) return s
  return s.slice(0, max - tail - 1) + '…' + s.slice(-tail)
}
