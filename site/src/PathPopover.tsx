import {
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  shift,
  useClick,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from '@floating-ui/react'
import { useState } from 'react'
import { FaRegCopy } from 'react-icons/fa6'
import { copyText } from './CopyName'

// The deepest breadcrumb (the current directory) as a tap-to-open path card,
// not a hover tooltip: a hover tip stuck open after the tap that drilled here,
// so every fresh page landed with the full path already showing. This opens
// only on a deliberate tap (never on load), holds the full `gs://…/` prefix and
// a copy button, and dismisses on Esc / tap-away. Ancestor crumbs stay plain
// navigating links.
export function PathPopover({ label, fullPath }: { label: string; fullPath: string }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    placement: 'bottom-start',
    middleware: [offset(6), flip(), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  })
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useClick(context),
    useDismiss(context),
    useRole(context, { role: 'dialog' }),
  ])
  return (
    <>
      <button type="button" className="here path-pop-btn" ref={refs.setReference} {...getReferenceProps()} title="Show / copy full path">{label}</button>
      {open && (
        <FloatingPortal>
          <div className="path-pop" ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()}>
            <code className="full">{fullPath}</code>
            <button
              type="button" className="path-copy" aria-label="Copy path to clipboard" title={copied ? 'copied ✓' : 'Copy path to clipboard'}
              onClick={() => copyText(fullPath).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200) })}
            >{copied ? <span className="copied">✓</span> : <FaRegCopy aria-hidden />}</button>
          </div>
        </FloatingPortal>
      )}
    </>
  )
}
