/** `DigestParams` in a URL (`/admin/slack`'s candidate columns, `?dc=w30,t0.7-1,dw,hmw,kw;w45,…`): `w<days>`,
 * `t<p0>-<p1>`, `d<w|m>` (diff: window | month-to-date), `h<m|w…>` (headline figures, in order; `h` alone: none),
 * `k<w|m>` (weeks: window | month), `q<o|c>` (the quota line: off | circles), `p<u|p>` (the plot: usage | pools),
 * `a<pct>` (the red mark's share). A field left out is the default's; an unreadable one is dropped. */
import { DEFAULT_PARAMS, type DigestParams } from './common'

export function encodeParams(p: DigestParams): string {
  const d = DEFAULT_PARAMS
  const out: string[] = []
  if (p.windowDays !== d.windowDays) out.push(`w${p.windowDays}`)
  if (p.todayAt[0] !== d.todayAt[0] || p.todayAt[1] !== d.todayAt[1]) out.push(`t${p.todayAt[0]}-${p.todayAt[1]}`)
  if (p.diff !== d.diff) out.push(`d${p.diff[0]}`)
  if (p.headline.join() !== d.headline.join()) out.push(`h${p.headline.map(f => (f === 'mtd' ? 'm' : 'w')).join('')}`)
  if (p.weeks !== d.weeks) out.push(`k${p.weeks[0]}`)
  if (p.quotas !== d.quotas) out.push(`q${p.quotas[0]}`)
  if (p.plot !== d.plot) out.push(`p${p.plot[0]}`)
  if (p.alertAt !== d.alertAt) out.push(`a${p.alertAt}`)
  return out.join(',')
}

export function decodeParams(s: string): DigestParams {
  const p: DigestParams = { ...DEFAULT_PARAMS, todayAt: [...DEFAULT_PARAMS.todayAt], headline: [...DEFAULT_PARAMS.headline] }
  for (const f of s.split(',').filter(Boolean)) {
    const [k, v] = [f[0], f.slice(1)]
    if (k === 'w' && /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 92) p.windowDays = Number(v)
    else if (k === 't') {
      const [a, b] = v.split('-').map(Number)
      if (0 < a && a <= b && b <= 1) p.todayAt = [a, b]
    } else if (k === 'd' && (v === 'w' || v === 'm')) p.diff = v === 'w' ? 'window' : 'mtd'
    else if (k === 'h' && /^[mw]{0,2}$/.test(v) && new Set(v).size === v.length) p.headline = [...v].map(c => (c === 'm' ? 'mtd' : 'window'))
    else if (k === 'k' && (v === 'w' || v === 'm')) p.weeks = v === 'w' ? 'window' : 'month'
    else if (k === 'q' && (v === 'o' || v === 'c')) p.quotas = v === 'o' ? 'off' : 'circles'
    else if (k === 'p' && (v === 'u' || v === 'p')) p.plot = v === 'u' ? 'usage' : 'pools'
    else if (k === 'a' && /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 100) p.alertAt = Number(v)
  }
  return p
}
