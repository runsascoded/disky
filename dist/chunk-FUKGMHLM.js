import { parseColor } from './chunk-JJOWQXW4.js';

// src/colors.ts
var DEFAULT_PALETTE = [
  "hsl(210 70% 55%)",
  "hsl(30 80% 55%)",
  "hsl(160 55% 45%)",
  "hsl(350 65% 55%)",
  "hsl(280 55% 55%)",
  "hsl(50 75% 55%)",
  "hsl(180 50% 45%)",
  "hsl(120 45% 50%)"
];
function slotColor(i, palette = DEFAULT_PALETTE) {
  if (i < palette.length) return palette[i];
  return `hsl(${Math.round(i * 137.508 % 360)} 60% 52%)`;
}
var CONTAINER_BG = [32, 32, 36];
function parseHsl(c) {
  const m = c.match(/^hsla?\(([^)]+)\)$/);
  if (!m) return null;
  const [h, s, l] = m[1].replace(/\//g, " ").split(/[\s,]+/).filter(Boolean);
  const out = [parseFloat(h), parseFloat(s) / 100, parseFloat(l) / 100];
  return out.some((n) => Number.isNaN(n)) ? null : out;
}
function hashUnit(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = Math.imul(h, 31) + s.charCodeAt(i) | 0;
  return (h % 1e3 + 1e3) % 1e3 / 1e3;
}
function hierColor(base, l2Key, hasKids, opts) {
  const p = parseHsl(base);
  if (!p) return base;
  let [h, s, l] = p;
  if (l2Key) {
    const j = 2 * hashUnit(l2Key) - 1;
    h = ((h + j * (opts?.hueJitter ?? 16)) % 360 + 360) % 360;
    l = Math.max(0.22, Math.min(0.82, l + j * (opts?.lightJitter ?? 0.1)));
  }
  if (hasKids) {
    s *= 0.55;
    l *= 0.5;
  }
  return `hsl(${Math.round(h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`;
}
function contrastEdge(bg, fade = 1, base = CONTAINER_BG) {
  if (!bg) return null;
  const p = parseColor(bg);
  if (!p) return null;
  const [r, g, b, a] = p;
  const eff = a * fade;
  const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) * eff + (0.2126 * base[0] + 0.7152 * base[1] + 0.0722 * base[2]) * (1 - eff);
  return lum > 96 ? "rgba(0, 0, 0, 0.55)" : "rgba(255, 255, 255, 0.42)";
}
function ageFade(style, age012, opts) {
  const t = Math.max(0, Math.min(1, age012));
  const floor = opts?.floor ?? 0.72;
  const p = t * floor * 100;
  if (p < 0.5 || !style.bg) return style;
  const panel = opts?.panel ?? "var(--dt-treemap-fade-panel, #1a1a1f)";
  const mix = (c, pct) => `color-mix(in oklch, ${c}, ${panel} ${pct.toFixed(0)}%)`;
  return {
    ...style,
    bg: mix(style.bg, p),
    ...style.ink && { ink: mix(style.ink, p / 2) }
  };
}
function ageDomain(nodes, getAge) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const n of nodes) {
    const a = getAge(n);
    if (a == null) continue;
    if (a < lo) lo = a;
    if (a > hi) hi = a;
  }
  return lo <= hi ? [lo, hi] : null;
}
function age01(age, [oldest, newest]) {
  if (newest <= oldest) return 0;
  return Math.max(0, Math.min(1, (newest - age) / (newest - oldest)));
}

export { CONTAINER_BG, DEFAULT_PALETTE, age01, ageDomain, ageFade, contrastEdge, hierColor, slotColor };
//# sourceMappingURL=chunk-FUKGMHLM.js.map
//# sourceMappingURL=chunk-FUKGMHLM.js.map