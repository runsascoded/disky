/** Inline-SVG glyphs for the control-bar buttons (export + fullscreen), so the
 *  core stays dependency-free (no icon lib) while looking crisper than the bare
 *  Unicode glyphs. `1em`-sized, `currentColor`-stroked — they inherit the
 *  button's font-size and colour. */
import type { SVGProps } from 'react'

const base: SVGProps<SVGSVGElement> = {
  width: '1em',
  height: '1em',
  viewBox: '0 0 16 16',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.4,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
}

/** Two overlapping sheets — copy to clipboard. */
export const CopyIcon = () => (
  <svg {...base}>
    <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
    <path d="M10.5 5.5V4A1.5 1.5 0 0 0 9 2.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5" />
  </svg>
)

/** Down arrow onto a tray line — download. */
export const DownloadIcon = () => (
  <svg {...base}>
    <path d="M8 2.5v7" />
    <path d="M5 6.5 8 9.5l3-3" />
    <path d="M3 12.5h10" />
  </svg>
)

/** Check mark — transient "copied / saved ✓" confirmation. */
export const CheckIcon = () => (
  <svg {...base}>
    <path d="M3.5 8.5 6.5 11.5 12.5 4.5" />
  </svg>
)

/** Four corner brackets — toggle fullscreen. */
export const FullscreenIcon = () => (
  <svg {...base}>
    <path d="M3 6V3.5A.5.5 0 0 1 3.5 3H6" />
    <path d="M10 3h2.5a.5.5 0 0 1 .5.5V6" />
    <path d="M13 10v2.5a.5.5 0 0 1-.5.5H10" />
    <path d="M6 13H3.5a.5.5 0 0 1-.5-.5V10" />
  </svg>
)
