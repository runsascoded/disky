// disky's marks: a stylized treemap per store, the store's own colors, and a store glyph in the
// big tile. Pure functions returning SVG strings; `build.mjs` writes and rasterizes them.
import { readFileSync } from 'node:fs'

const LOGOS = JSON.parse(readFileSync(new URL('./logos.json', import.meta.url), 'utf8'))

// Tiles, largest first: big, top-right, bottom-left, mid-right, bottom-middle, bottom-right.
const LAYOUT = [[0, 0, .58, .62], [.58, 0, .42, .36], [0, .62, .34, .38], [.58, .36, .42, .30], [.34, .62, .24, .38], [.58, .66, .42, .34]]
const tilesSvg = (x0, y0, w, h, colors, gap, r) => LAYOUT.map(([x, y, tw, th], i) =>
  `<rect x="${(x0 + x * w + gap / 2).toFixed(1)}" y="${(y0 + y * h + gap / 2).toFixed(1)}" width="${(tw * w - gap).toFixed(1)}" height="${(th * h - gap).toFixed(1)}" rx="${r}" fill="${colors[i]}"/>`).join('')
export const svg = (body, vb = '0 0 512 512') => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb}">${body}</svg>`
export const inner = s => s.replace(/^<svg[^>]*>|<\/svg>$/g, '')

// A logo fitted into the big tile (x 71–280, y 71–295; center 175, 183): at most 168 wide, 150 tall.
function fit(key, body) {
  const [bx, by, bw, bh] = LOGOS[key].box
  const k = Math.min(168 / bw, 150 / bh)
  return `<g transform="translate(${(175 - (bx + bw / 2) * k).toFixed(1)} ${(183 - (by + bh / 2) * k).toFixed(1)}) scale(${k.toFixed(3)})">${body}</g>`
}
function mono(key, ink) {
  const L = LOGOS[key]
  const body = L.ds
    ? L.ds.map(d => `<path d="${d}" fill="${ink}" fill-rule="evenodd"/>`).join('')
    : `<path d="${L.d}" fill="${ink}"${L.evenodd ? ' fill-rule="evenodd"' : ''}/>`
  return fit(key, body)
}
// A 100×100 glyph centered in the big tile at `size`.
const at = (body, size) => `<g transform="translate(${175 - size / 2} ${183 - size / 2}) scale(${size / 100})">${body}</g>`
// ⌘ as one stroke (a Unicode symbol, not an Apple mark).
const CMD = 'M38 25 V75 A13 13 0 1 1 25 62 H75 A13 13 0 1 1 62 75 V25 A13 13 0 1 1 75 38 H25 A13 13 0 1 1 38 25 Z'
const cmd = ink => at(`<path d="${CMD}" fill="none" stroke="${ink}" stroke-width="7.5" stroke-linejoin="round"/>`, 140)
// A generic three-lobed cloud (neither Google's nor Cloudflare's outline), cut into Google's four colors.
const CLOUD = 'M24 78 A18 18 0 0 1 22 42 A26 26 0 0 1 70 36 A20 20 0 0 1 78 78 Z'
function cloud4() {
  const parts = [['#EA4335', 0, 0, 100, 44], ['#4285F4', 58, 0, 42, 100], ['#34A853', 30, 44, 28, 56], ['#FBBC04', 0, 44, 30, 56]]
  return at(`<clipPath id="c4-clip"><path d="${CLOUD}"/></clipPath><g clip-path="url(#c4-clip)">${parts.map(([c, x, y, w, h]) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${c}"/>`).join('')}</g><path d="M28 70 A10 10 0 0 1 27 50 A18 18 0 0 1 61 46 A12 12 0 0 1 72 70 Z" fill="#FFFFFF"/>`, 170)
}
const S3G = '<linearGradient id="s3g" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#1B660F"/><stop offset="1" stop-color="#6CAE3E"/></linearGradient>'

// Each store in two families: on a dark ground (the default, and favicons) and on a light one.
export const STORES = {
  local: { name: 'disky', glyph: () => '',
    dark: { ground: '#0D1117', tiles: ['#14B8A6', '#5EEAD4', '#0F766E', '#99F6E4', '#2DD4BF', '#CCFBF1'] },
    light: { ground: '#E6F4F1', tiles: ['#14B8A6', '#5EEAD4', '#0F766E', '#99F6E4', '#2DD4BF', '#115E59'] } },
  macos: { name: 'macOS', glyph: () => cmd('#FFFFFF'),
    dark: { ground: '#2C2C2E', tiles: ['#0A84FF', '#5AC8FA', '#8E8E93', '#C7C7CC', '#FF9F0A', '#636366'] },
    light: { ground: '#E3E5E8', tiles: ['#1D1D1F', '#0A84FF', '#8E8E93', '#5AC8FA', '#FF9F0A', '#AEAEB2'] } },
  nas: { name: 'NAS / Linux', glyph: () => mono('linux', '#111111'),
    dark: { ground: '#111111', tiles: ['#FCC624', '#F5A623', '#4B5563', '#9CA3AF', '#E5E7EB', '#6B7280'] },
    light: { ground: '#F3F4F6', tiles: ['#FCC624', '#F5A623', '#4B5563', '#9CA3AF', '#1F2937', '#6B7280'] } },
  r2: { name: 'Cloudflare R2', glyph: () => mono('r2icon', '#FFFFFF'),
    dark: { ground: '#1D1D20', tiles: ['#F38020', '#FAAE40', '#6B6B70', '#FBBF77', '#B45309', '#FDE3C8'] },
    light: { ground: '#FFFFFF', tiles: ['#F38020', '#FAAE40', '#404041', '#FBBF77', '#B45309', '#FDE3C8'] } },
  s3: { name: 'Amazon S3', glyph: () => mono('s3icon', '#FFFFFF'),
    dark: { ground: '#232F3E', tiles: ['url(#s3g)', '#FF9900', '#5A6B80', '#FFB84D', '#EC7211', '#8B9BB0'] },
    light: { ground: '#FFFFFF', tiles: ['url(#s3g)', '#FF9900', '#5A6B80', '#FFB84D', '#EC7211', '#232F3E'] } },
  gcs: { name: 'Google Cloud Storage', glyph: cloud4,
    dark: { ground: '#202124', tiles: ['#FFFFFF', '#4285F4', '#34A853', '#FBBC04', '#EA4335', '#8AB4F8'] },
    light: { ground: '#E8EAED', tiles: ['#FFFFFF', '#4285F4', '#34A853', '#FBBC04', '#EA4335', '#1A56C4'] } },
  cw: { name: 'CoreWeave', glyph: () => mono('cw', '#FFFFFF'),
    dark: { ground: '#0A0F2C', tiles: ['#2741E7', '#6B7CFF', '#1A2BA8', '#A9B4FF', '#4F63F0', '#DDE2FF'] },
    light: { ground: '#FFFFFF', tiles: ['#2741E7', '#6B7CFF', '#1A2BA8', '#A9B4FF', '#4F63F0', '#0A0F2C'] } },
  hf: { name: 'Hugging Face', glyph: () => fit('noto', LOGOS.noto.raw),
    dark: { ground: '#0B0F19', tiles: ['#2A3040', '#FFD21E', '#FF9D0B', '#FFE58A', '#F59E0B', '#FFF4C2'] },
    light: { ground: '#FFFFFF', tiles: ['#0B0F19', '#FFD21E', '#FF9D0B', '#FFE58A', '#F59E0B', '#3A3B45'] } },
}
export const STORE_KEYS = Object.keys(STORES).filter(k => k !== 'local')

// `bleed`: a full square ground (apple-touch icons; iOS rounds the corners itself).
// `ring`: an outline, for a mark whose ground matches what it sits on.
export function mark(key, { theme = 'dark', bleed = false, ring = null } = {}) {
  const S = STORES[key]
  if (!S) throw new Error(`no mark "${key}" (one of ${Object.keys(STORES).join(', ')})`)
  const T = S[theme]
  const edge = ring ? `<rect x="3" y="3" width="506" height="506" rx="109" fill="none" stroke="${ring}" stroke-width="6"/>` : ''
  return svg(`<defs>${S3G}</defs><rect width="512" height="512" rx="${bleed ? 0 : 112}" fill="${T.ground}"/>${tilesSvg(64, 64, 384, 384, T.tiles, 14, 16)}${S.glyph()}${edge}`)
}

// The fallback for a deployment with no mark: its label's first one or two characters on disky's dark square.
export function labelMark(label) {
  const text = [...(label || 'd').trim()].slice(0, 2).join('')
  const esc = text.replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`)
  const size = [...text].length > 1 ? 250 : 340
  return svg(`<rect width="512" height="512" rx="112" fill="#0D1117"/><text x="256" y="${256 + size * .36}" text-anchor="middle" font-family="system-ui, -apple-system, Helvetica, Arial, sans-serif" font-weight="700" font-size="${size}" fill="#5EEAD4">${esc}</text>`)
}

// "disky" (Fredoka SemiBold) and the tagline (IBM Plex Sans), both OFL, as outlines: a README's SVG can't load fonts.
const TEXT = JSON.parse(readFileSync(new URL('./text.json', import.meta.url), 'utf8'))
// The README banner (1200 × 400): disky's mark, the wordmark, and every store's mark. `dark`: the dark banner carries
// the light family, the light banner the dark family, so every mark contrasts with the banner it sits on.
export function banner(dark) {
  const bg = dark ? '#0D1117' : '#F2F7F6', fg = dark ? '#E6F4F1' : '#0F2A27', mut = dark ? '#8FB3AC' : '#3E6660'
  const theme = dark ? 'light' : 'dark'
  const lineup = STORE_KEYS.map((k, i) => `<g transform="translate(${470 + i * 92} 258) scale(.14)">${inner(mark(k, { theme }))}</g>`).join('')
  return svg(`<rect width="1200" height="400" rx="28" fill="${bg}"/><g transform="translate(64 64) scale(.53)">${inner(mark('local', { theme }))}</g>`
    + `<path transform="translate(466 168)" d="${TEXT.wordmark.d}" fill="${fg}"/><path transform="translate(470 222)" d="${TEXT.tagline.d}" fill="${mut}"/>${lineup}`, '0 0 1200 400')
}
