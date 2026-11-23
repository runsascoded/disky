/** What the fleet's cards (`fleet/functions/_lib/card.ts`, `views.ts`) and the deployment status card (`deployCard.ts`)
 *  share: the two themes' palette, the outcome colours, colour mixing, an SVG text node and the short formatters. Each
 *  card sits on its own opaque background, so it reads the same in Slack's light and dark themes. Pure and DOM-free. */
import { esc } from './text.js'
import type { Outcome } from './runOutcome.js'

export const DAY = 86400

export type Theme = 'dark' | 'light'
export interface Palette { bg: string; panel: string; ink: string; ink2: string; ink3: string; grid: string; border: string }
export const THEMES: Record<Theme, Palette> = {
  dark: { bg: '#1a1a19', panel: '#232322', ink: '#f4f4f5', ink2: '#c3c2b7', ink3: '#9a988d', grid: '#33332f', border: '#3a3a36' },
  light: { bg: '#ffffff', panel: '#f6f6f3', ink: '#1a1a19', ink2: '#45443e', ink3: '#6b6a62', grid: '#e2e1da', border: '#d9d8d0' },
}

/** Each outcome's colour, per theme: the fleet page's `--ok` / `--warn` / `--bad` / `--accent`, and a grey for a cancel. */
export const OUTCOME_COLOR: Record<Theme, Record<Outcome, string>> = {
  dark: { ok: '#4ade80', issues: '#e3b341', failed: '#f26b6b', running: '#6ea2f5', cancelled: '#8b8a80' },
  light: { ok: '#1f8a4c', issues: '#a86200', failed: '#c62828', running: '#2f6fd6', cancelled: '#8b8a80' },
}

/** `#rrggbb` mixed toward `toward` (`#rrggbb`) by `f` ∈ [0, 1]. */
export function mix(color: string, toward: string, f: number): string {
  const p = (c: string, i: number) => parseInt(c.slice(1 + 2 * i, 3 + 2 * i), 16)
  return `#${[0, 1, 2].map(i => Math.round(p(color, i) + (p(toward, i) - p(color, i)) * f).toString(16).padStart(2, '0')).join('')}`
}

export const r1 = (n: number) => Math.round(n * 10) / 10

interface TextOpts { fill: string; weight?: 400 | 600; anchor?: 'start' | 'middle' | 'end' }
export function text(x: number, y: number, s: string, px: number, o: TextOpts): string {
  return `<text x="${r1(x)}" y="${r1(y)}" font-size="${px}" font-weight="${o.weight ?? 400}" fill="${o.fill}"${o.anchor && o.anchor !== 'start' ? ` text-anchor="${o.anchor}"` : ''}>${esc(s)}</text>`
}

/** An object count, compact: `278K`, `153M`, `1.2B`. */
export const fmtCount = (n: number): string => new Intl.NumberFormat('en-US', { notation: 'compact', maximumSignificantDigits: 3 }).format(n)
export const fmtUsd = (usd: number): string => `$${usd < 10 ? usd.toFixed(2) : usd.toFixed(0)}`
export const md = (t: number) => { const d = new Date(t * 1000); return `${d.getUTCMonth() + 1}/${d.getUTCDate()}` }
export const stamp = (t: number) => new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ')
