/** `DigestParams` in a URL (`/admin/slack`'s candidate columns, `?dc=w30,t0.7-1,dw,hmw,kw;w45,…`): `w<days>`,
 * `t<p0>-<p1>`, `d<w|m>` (diff: window | month-to-date), `h<m|w…>` (headline figures, in order; `h` alone: none),
 * `k<w|m>` (weeks: window | month), `q<o|c|r|h>` (the quota line: off | circles | runway | runway-hue), `p<u|p>` (the plot: usage | pools),
 * `a<pct>` (the red mark's share), `l<r|p>` (layout: primary | pools); the page's alone, which `parseParams` refuses: `q<a|b|s|d>`
 * (the `ArrowMark`s), `m<w|c|r|h|a|b|s|d>` (reply marks), `r<t|p>` (reply headline: total | primary), `ul` (a reply's pools on their own lines), `sc` (a reply's avatar: a collage of its pools). A field left out is the default's; an unreadable one is dropped. */
import { DEFAULT_PARAMS, type DigestParams } from './common'
import type { ReplyMarks } from './cwReply'
import { ARROW_MARKS } from './pools'

export function encodeParams(p: DigestParams): string {
  const d = DEFAULT_PARAMS
  const out: string[] = []
  if (p.windowDays !== d.windowDays) out.push(`w${p.windowDays}`)
  if (p.todayAt[0] !== d.todayAt[0] || p.todayAt[1] !== d.todayAt[1]) out.push(`t${p.todayAt[0]}-${p.todayAt[1]}`)
  if (p.diff !== d.diff) out.push(`d${p.diff[0]}`)
  if (p.headline.join() !== d.headline.join()) out.push(`h${p.headline.map(f => (f === 'mtd' ? 'm' : 'w')).join('')}`)
  if (p.weeks !== d.weeks) out.push(`k${p.weeks[0]}`)
  if (p.quotas !== d.quotas) out.push(`q${letter(QUOTAS, p.quotas)}`)
  if (p.plot !== d.plot) out.push(`p${p.plot[0]}`)
  if (p.alertAt !== d.alertAt) out.push(`a${p.alertAt}`)
  if (p.layout !== d.layout) out.push(`l${p.layout === 'pools' ? 'p' : 'r'}`)
  if (p.replyMarks) out.push(`m${letter(MARKS, p.replyMarks)}`)
  if (p.replyTotal !== undefined) out.push(`r${p.replyTotal ? 't' : 'p'}`)
  if (p.replyLines) out.push('ul')
  if (p.replyCollage) out.push('sc')
  return out.join(',')
}

/** Applies field `f` to `p`; false when it can't be read (left as it was). */
function readField(p: DigestParams, f: string): boolean {
  const [k, v] = [f[0], f.slice(1)]
  if (k === 'w' && /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 92) p.windowDays = Number(v)
  else if (k === 't') {
    const [a, b] = v.split('-').map(Number)
    if (!(0 < a && a <= b && b <= 1)) return false
    p.todayAt = [a, b]
  } else if (k === 'd' && (v === 'w' || v === 'm')) p.diff = v === 'w' ? 'window' : 'mtd'
  else if (k === 'h' && /^[mw]{0,2}$/.test(v) && new Set(v).size === v.length) p.headline = [...v].map(c => (c === 'm' ? 'mtd' : 'window'))
  else if (k === 'k' && (v === 'w' || v === 'm')) p.weeks = v === 'w' ? 'window' : 'month'
  else if (k === 'q' && Object.hasOwn(QUOTAS, v)) p.quotas = QUOTAS[v]
  else if (k === 'p' && (v === 'u' || v === 'p')) p.plot = v === 'u' ? 'usage' : 'pools'
  else if (k === 'a' && /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 100) p.alertAt = Number(v)
  else if (k === 'l' && (v === 'r' || v === 'p')) p.layout = v === 'p' ? 'pools' : 'primary'
  else if (k === 'm' && Object.hasOwn(MARKS, v)) p.replyMarks = MARKS[v]
  else if (k === 'r' && (v === 't' || v === 'p')) p.replyTotal = v === 't'
  else if (f === 'ul') p.replyLines = true
  else if (f === 'sc') p.replyCollage = true
  else return false
  return true
}

const ARROWS = { a: 'arrow', b: 'arrow-disc', s: 'arrow-chip', d: 'arrow-circle' } as const
const QUOTAS: Record<string, DigestParams['quotas']> = { o: 'off', c: 'circles', r: 'runway', h: 'runway-hue', ...ARROWS }
const MARKS: Record<string, ReplyMarks> = { w: 'warning', c: 'circles', r: 'runway', h: 'runway-hue', ...ARROWS }
const PAGE_FIELDS = 'mrus'
const letter = <T>(m: Record<string, T>, v: T): string => Object.keys(m).find(k => m[k] === v)!

/** What of a candidate the job can render under: its OP fields, without the page's reply fields. */
export function jobParams(s: string): string {
  return s.split(',').filter(f => f && !PAGE_FIELDS.includes(f[0])).join(',')
}

/** A candidate's OP marks have no emoji in Slack, so the job can't post them. */
export const previewMarks = (p: DigestParams): boolean => (ARROW_MARKS as string[]).includes(p.quotas)

function defaults(): DigestParams {
  return { ...DEFAULT_PARAMS, todayAt: [...DEFAULT_PARAMS.todayAt], headline: [...DEFAULT_PARAMS.headline] }
}

export function decodeParams(s: string): DigestParams {
  const p = defaults()
  for (const f of s.split(',').filter(Boolean)) readField(p, f)
  return p
}

/** `decodeParams` for a caller that must not guess (the job's `/api/digest-op`): throws on a field it can't read, an
 * empty field or a repeated key, where `decodeParams` would drop or override it. */
export function parseParams(s: string): DigestParams {
  const p = defaults()
  const seen = new Set<string>()
  for (const f of s === '' ? [] : s.split(',')) {
    if (!f || !readField(p, f)) throw new Error(`digest params: unreadable field ${JSON.stringify(f)} in ${JSON.stringify(s)}`)
    if (PAGE_FIELDS.includes(f[0]) || previewMarks(p)) throw new Error(`digest params: ${JSON.stringify(f)} is for the preview page only`)
    if (seen.has(f[0])) throw new Error(`digest params: repeated field ${JSON.stringify(f[0])} in ${JSON.stringify(s)}`)
    seen.add(f[0])
  }
  return p
}
