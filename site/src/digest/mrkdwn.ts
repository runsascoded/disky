/** The digest OP's markdown as Slack shows it, tokenized (`/admin/slack` renders the tokens): `**bold**` / `*bold*`,
 * `_italic_`, `[text](url)` links, `![alt](url)` images, `:arrow_degN:` (the digest's trend-arrow emoji), and the
 * quota marks' stock emoji (`EMOJI`). Only
 * what the digest writes; anything else is text. Pure. */
export type Tok =
  | { t: 'text'; s: string }
  | { t: 'bold' | 'italic'; s: string }
  | { t: 'link'; s: string; href: string }
  | { t: 'image'; alt: string; src: string }
  | { t: 'arrow'; deg: number }
  | { t: 'emoji'; name: string; char: string }

/** The stock Slack emoji the digest writes (the quota line's marks), by shortcode. */
export const EMOJI: Record<string, string> = { red_circle: '🔴', large_orange_circle: '🟠' }

const RE = /!\[([^\]]*)\]\(([^)\s]+)\)|\[([^\]]+)\]\(([^)\s]+)\)|:arrow_deg(-?\d+):|:(red_circle|large_orange_circle):|\*\*([^*]+)\*\*|\*([^*]+)\*|(?<![\w])_([^_]+)_(?![\w])/g

export function tokenize(line: string): Tok[] {
  const out: Tok[] = []
  let at = 0
  for (const m of line.matchAll(RE)) {
    if (m.index! > at) out.push({ t: 'text', s: line.slice(at, m.index) })
    if (m[2] !== undefined) out.push({ t: 'image', alt: m[1], src: m[2] })
    else if (m[4] !== undefined) out.push({ t: 'link', s: m[3], href: m[4] })
    else if (m[5] !== undefined) out.push({ t: 'arrow', deg: Number(m[5]) })
    else if (m[6] !== undefined) out.push({ t: 'emoji', name: m[6], char: EMOJI[m[6]] })
    else if (m[7] !== undefined || m[8] !== undefined) out.push({ t: 'bold', s: m[7] ?? m[8] })
    else out.push({ t: 'italic', s: m[9] })
    at = m.index! + m[0].length
  }
  if (at < line.length) out.push({ t: 'text', s: line.slice(at) })
  return out
}
