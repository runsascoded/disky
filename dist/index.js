import { flattenPlaced, hitTest, fitCandidate, candidates, inlineRoom, isFolded, layoutCells, edgeEmphFactor, layoutChildren, categoricalStyle, resolveRing, resolveCellStyle } from './chunk-2FTUTDQU.js';
export { NAME_MIN_PX, candidates, fitCandidate, flattenPlaced, foldedOf, hitTest, inlineRoom, isFolded, layoutCells, resolveCellStyle } from './chunk-2FTUTDQU.js';
import { foldSmall, squarifyRemainder, squarify, foldThin } from './chunk-TWK3KLKY.js';
export { foldSmall, foldThin, squarify, squarifyRemainder } from './chunk-TWK3KLKY.js';
import { useHoverPin } from './chunk-HMG5G7YN.js';
export { useHoverPin } from './chunk-HMG5G7YN.js';
import { CONTAINER_BG, slotColor, DEFAULT_PALETTE, contrastEdge } from './chunk-FUKGMHLM.js';
export { CONTAINER_BG, DEFAULT_PALETTE, age01, ageDomain, ageFade, contrastEdge, slotColor } from './chunk-FUKGMHLM.js';
import { parseColor } from './chunk-JJOWQXW4.js';
export { parseColor } from './chunk-JJOWQXW4.js';
import { divergingColor } from './chunk-2DHCBMZJ.js';
export { divergingColor, divergingInk } from './chunk-2DHCBMZJ.js';
import { useRef, useLayoutEffect, useMemo, useEffect, useState, useCallback } from 'react';
import { jsx, jsxs, Fragment } from 'react/jsx-runtime';

function dustOffsets(len, n, ratio) {
  if (n < 1 || len <= 0) return [];
  const g = ratio === 1 ? len / n : len * (ratio - 1) / (ratio ** n - 1);
  const out = [];
  let at = 0;
  for (let k = 0; k < n; k++) {
    at += g * ratio ** k;
    if (at >= len) break;
    out.push(at);
  }
  return out;
}
function dustLineCount(len, count, base2) {
  const byCount = Math.round(base2 * Math.log2(count + 1));
  return Math.max(2, Math.min(byCount, Math.floor(len / 4)));
}
function drawDust(ctx, ox, oy, w, h, count, color = "rgba(150, 150, 165, 0.5)", baseLines = 3, ratio = 1.32) {
  if (w <= 0 || h <= 0) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (const d of dustOffsets(w, dustLineCount(w, count, baseLines), ratio)) {
    const x = Math.round(w - d) + 0.5;
    ctx.moveTo(ox + x, oy);
    ctx.lineTo(ox + x, oy + h);
  }
  for (const d of dustOffsets(h, dustLineCount(h, count, baseLines), ratio)) {
    const y = Math.round(h - d) + 0.5;
    ctx.moveTo(ox, oy + y);
    ctx.lineTo(ox + w, oy + y);
  }
  ctx.stroke();
}
function DustHatch({ w, h, count, color, baseLines = 3, ratio = 1.32 }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const cv = ref.current;
    if (!cv || w <= 0 || h <= 0) return;
    const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    drawDust(ctx, 0, 0, w, h, count, color ?? "rgba(150, 150, 165, 0.5)", baseLines, ratio);
  }, [w, h, count, color, baseLines, ratio]);
  return /* @__PURE__ */ jsx(
    "canvas",
    {
      ref,
      width: Math.max(1, Math.round(w)),
      height: Math.max(1, Math.round(h)),
      style: { position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }
    }
  );
}

// src/cssColor.ts
function cssColor(el, color) {
  const m = /^\s*var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)\s*$/.exec(color);
  if (!m) return color;
  const v = getComputedStyle(el).getPropertyValue(m[1]).trim();
  return v || (m[2] ?? "").trim() || color;
}
function colorResolver(el) {
  const cache = /* @__PURE__ */ new Map();
  return (color) => {
    if (!color.startsWith("var(")) return color;
    let v = cache.get(color);
    if (v === void 0) {
      v = cssColor(el, color);
      cache.set(color, v);
    }
    return v;
  };
}
var CONTAINER_RGB = `rgb(${CONTAINER_BG[0]}, ${CONTAINER_BG[1]}, ${CONTAINER_BG[2]})`;
var SYNC_BUDGET_MS = 8;
var FRAME_BUDGET_MS = 10;
var resolveVar = (c) => c;
var lblFs = 13.5;
var lblFsSm = 11.5;
function readLabelSizes(el) {
  const cs = getComputedStyle(el);
  const px = (v, dflt) => {
    const m = /^([\d.]+)(px|rem)?$/.exec(v.trim());
    if (!m) return dflt;
    return m[2] === "rem" ? parseFloat(m[1]) * parseFloat(getComputedStyle(document.documentElement).fontSize || "16") : parseFloat(m[1]);
  };
  lblFs = px(cs.getPropertyValue("--dt-treemap-lbl-fs"), 13.5);
  lblFsSm = px(cs.getPropertyValue("--dt-treemap-lbl-fs-sm"), 11.5);
}
function rgbaAt(c, alpha) {
  const p = parseColor(resolveVar(c ?? ""));
  if (!p) return null;
  return `rgba(${p[0]}, ${p[1]}, ${p[2]}, ${p[3] * alpha})`;
}
function TreemapCanvas({
  cells,
  width,
  height,
  styleOpts,
  getSize,
  getLabel,
  formatSize,
  formatCell,
  sizeAlign,
  inlineSizeMinWidth,
  idFor,
  expandable,
  dustTexture,
  cellHref,
  a11yLinks,
  a11yMaxCells,
  a11yMinSide,
  pinnedKey,
  canvasRef,
  onHover,
  onClick,
  onLeave
}) {
  const ref = useRef(null);
  const setCanvas = (el) => {
    ref.current = el;
    if (canvasRef) canvasRef.current = el;
  };
  const flat = useMemo(() => {
    const f = flattenPlaced(cells);
    f.sort((a, b) => b.w * b.h - a.w * a.h);
    return f;
  }, [cells]);
  useLayoutEffect(() => {
    const cv = ref.current;
    if (!cv || width <= 0 || height <= 0) return;
    const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
    cv.width = Math.round(width * dpr);
    cv.height = Math.round(height * dpr);
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = CONTAINER_RGB;
    ctx.fillRect(0, 0, width, height);
    resolveVar = colorResolver(cv);
    readLabelSizes(cv);
    const opts = { styleOpts, getSize, getLabel, formatSize, formatCell, sizeAlign, inlineSizeMinWidth, dustTexture };
    let i = 0;
    let raf = 0;
    const paintUntil = (budgetMs) => {
      const start = performance.now();
      let n = 0;
      while (i < flat.length) {
        paintCell(ctx, flat[i], opts);
        i++;
        if (++n >= 256) {
          n = 0;
          if (performance.now() - start >= budgetMs) return;
        }
      }
    };
    paintUntil(SYNC_BUDGET_MS);
    const step = () => {
      paintUntil(FRAME_BUDGET_MS);
      if (i < flat.length) raf = requestAnimationFrame(step);
    };
    if (i < flat.length) raf = requestAnimationFrame(step);
    return () => {
      if (raf) cancelAnimationFrame(raf);
    };
  }, [flat, width, height, styleOpts, getSize, getLabel, formatSize, formatCell, sizeAlign, inlineSizeMinWidth, dustTexture]);
  const dustChildAt = (cell, lx, ly) => {
    const fn = cell.node;
    if (!dustTexture || fn.children.length <= 1 || fn.children.length > 4e3) return null;
    if (Math.min(cell.w, cell.h) < 10) return null;
    const rects = squarify(fn.children, 0, 0, cell.boxW, cell.boxH, getSize);
    const rx = lx - cell.x;
    const ry = ly - cell.y;
    const hit = rects.find((rc) => rx >= rc.x && rx < rc.x + rc.w && ry >= rc.y && ry < rc.y + rc.h);
    if (!hit) return null;
    const path = [...cell.path, hit.it];
    return { node: hit.it, path, key: idFor(hit.it, path) };
  };
  const cellHit = (cell) => {
    const node = cell.node;
    return {
      node,
      path: cell.path,
      key: idFor(node, cell.path),
      drillable: expandable(node, cell.path),
      branch: cell.hasKids,
      foldChild: false
    };
  };
  const toHit = (cell, lx, ly) => {
    if (cell.folded) {
      const child = dustChildAt(cell, lx, ly);
      if (!child) return null;
      return { node: child.node, path: child.path, key: child.key, drillable: false, branch: false, foldChild: true };
    }
    return cellHit(cell);
  };
  const localXY = (e) => {
    const box = e.currentTarget.getBoundingClientRect();
    return { lx: e.clientX - box.left, ly: e.clientY - box.top };
  };
  const overlayCells = useMemo(() => {
    if (!a11yLinks) return [];
    const out = [];
    for (const c of flat) {
      if (out.length >= a11yMaxCells) break;
      if (c.folded) continue;
      if (!(c.showLbl || c.hasKids)) continue;
      if (Math.min(c.w, c.h) < a11yMinSide) continue;
      out.push(c);
    }
    return out;
  }, [flat, a11yLinks, a11yMaxCells, a11yMinSide]);
  const pinnedCell = useMemo(
    () => pinnedKey ? flat.find((c) => !c.folded && idFor(c.node, c.path) === pinnedKey) ?? null : null,
    [flat, pinnedKey, idFor]
  );
  return /* @__PURE__ */ jsxs(Fragment, { children: [
    /* @__PURE__ */ jsx(
      "canvas",
      {
        ref: setCanvas,
        width: Math.max(1, Math.round(width)),
        height: Math.max(1, Math.round(height)),
        style: { position: "absolute", inset: 0, width: "100%", height: "100%", cursor: "default" },
        onMouseMove: (e) => {
          const cv = e.currentTarget;
          const { lx, ly } = localXY(e);
          const cell = hitTest(cells, lx, ly);
          if (!cell) {
            cv.style.cursor = "default";
            return;
          }
          const hit = toHit(cell, lx, ly);
          if (!hit) {
            cv.style.cursor = "default";
            return;
          }
          const interactive = hit.drillable || !!(cellHref && cellHref(hit.node, hit.path));
          cv.style.cursor = interactive ? "pointer" : "default";
          const box = cv.getBoundingClientRect();
          onHover(hit, box.left + cell.x, box.top + cell.y);
        },
        onMouseLeave: onLeave,
        onClick: (e) => {
          const { lx, ly } = localXY(e);
          const cell = hitTest(cells, lx, ly);
          if (!cell) return;
          const hit = toHit(cell, lx, ly);
          if (hit) onClick(hit, e);
        }
      }
    ),
    pinnedCell && /* @__PURE__ */ jsx(
      "div",
      {
        style: {
          position: "absolute",
          left: pinnedCell.x,
          top: pinnedCell.y,
          width: pinnedCell.w,
          height: pinnedCell.h,
          boxSizing: "border-box",
          border: "2px solid var(--dt-treemap-pin, rgba(120, 170, 255, 0.95))",
          borderRadius: pinnedCell.mode === "shared" ? 0 : 2,
          pointerEvents: "none"
        }
      }
    ),
    overlayCells.length > 0 && /* @__PURE__ */ jsx("div", { style: { position: "absolute", inset: 0, pointerEvents: "none" }, children: overlayCells.map((cell) => {
      const node = cell.node;
      const href = cellHref?.(node, cell.path);
      const hit = cellHit(cell);
      const commonStyle = {
        position: "absolute",
        left: cell.x,
        top: cell.y,
        width: cell.w,
        height: cell.h,
        margin: 0,
        padding: 0,
        border: 0,
        background: "transparent",
        color: "transparent",
        font: "inherit",
        textAlign: "start",
        pointerEvents: "none",
        overflow: "hidden"
      };
      const ariaLabel = `${getLabel(node)}, ${formatSize(getSize(node))}`;
      const focusHover = () => {
        const r = ref.current?.getBoundingClientRect();
        onHover(hit, (r?.left ?? 0) + cell.x + cell.w / 2, (r?.top ?? 0) + cell.y + 8);
      };
      const activate = (e) => {
        const me = e;
        if (!(href && (me.metaKey || me.ctrlKey || me.shiftKey || me.altKey || me.button === 1))) {
          e.preventDefault();
          onClick(hit, me);
        }
      };
      const onKeyDown = (e) => {
        if (e.key === "Enter" || e.key === " ") activate(e);
      };
      return href ? /* @__PURE__ */ jsx("a", { className: "dt-treemap-a11y-cell", href, "aria-label": ariaLabel, style: commonStyle, onFocus: focusHover, onClick: activate, onKeyDown }, hit.key) : /* @__PURE__ */ jsx("button", { className: "dt-treemap-a11y-cell", type: "button", "aria-label": ariaLabel, style: commonStyle, onFocus: focusHover, onClick: activate, onKeyDown }, hit.key);
    }) })
  ] });
}
function renderMapToCanvas(canvas, cells, width, height, opts, resolveEl) {
  const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
  canvas.width = Math.max(1, Math.round(width * dpr));
  canvas.height = Math.max(1, Math.round(height * dpr));
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = CONTAINER_RGB;
  ctx.fillRect(0, 0, width, height);
  resolveVar = colorResolver(resolveEl);
  readLabelSizes(resolveEl);
  const flat = flattenPlaced(cells);
  flat.sort((a, b) => b.w * b.h - a.w * a.h);
  for (const c of flat) paintCell(ctx, c, opts);
}
function paintCell(ctx, cell, o) {
  const { styleOpts, getSize, getLabel, formatSize, formatCell, sizeAlign, inlineSizeMinWidth, dustTexture } = o;
  const { x, y, w, h, depth, mode, edge, dust, folded, showLbl, hasKids } = cell;
  const shared = mode === "shared";
  const cw = shared ? w : w - (dust ? 1 : 2);
  const ch = shared ? h : h - (dust ? 1 : 2);
  if (cw <= 0 || ch <= 0) return;
  const { style, builtinEdge } = resolveCellStyle(cell, styleOpts);
  const fade = styleOpts.fadeAt(depth);
  const alpha = fade * (style.opacity ?? 1);
  ctx.fillStyle = CONTAINER_RGB;
  ctx.fillRect(x, y, cw, ch);
  const inset = shared ? edge : 0;
  const bx = x + inset;
  const by = y + inset;
  const bw = cw - 2 * inset;
  const bh = ch - 2 * inset;
  if (bw > 0 && bh > 0) {
    const fill = rgbaAt(style.bg, alpha);
    if (fill) {
      ctx.fillStyle = fill;
      ctx.fillRect(bx, by, bw, bh);
    }
  }
  if (shared && edge > 0) {
    const stroke = style.edge ? rgbaAt(style.edge, 1) : builtinEdge ? rgbaAt(builtinEdge, 1) : null;
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.lineWidth = edge;
      ctx.strokeRect(x + edge / 2, y + edge / 2, cw - edge, ch - edge);
    }
  }
  if (folded && dustTexture && Math.min(w, h) >= 6) {
    if (Math.min(w, h) >= 8) {
      ctx.strokeStyle = "rgba(150, 150, 165, 0.55)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 2]);
      ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1);
      ctx.setLineDash([]);
    }
    drawDust(ctx, bx, by, bw, bh, cell.node.count);
  }
  const segs = style.segments;
  if (segs && segs.length > 1 && !hasKids && !dust && Math.min(w, h) >= 18) {
    const segInset = shared ? Math.max(1, 2 * edge) : 3;
    const gap = 1;
    const horiz = w >= h;
    const span = (horiz ? cw : ch) - 2 * segInset - gap * (segs.length - 1);
    if (span >= segs.length * 2) {
      const total = segs.reduce((s, x2) => s + x2.frac, 0) || 1;
      let at = segInset;
      for (const seg of segs) {
        const len = seg.frac / total * span;
        const fill = rgbaAt(seg.color, alpha);
        if (fill) {
          ctx.fillStyle = fill;
          ctx.beginPath();
          if (horiz) roundRect(ctx, x + at, y + segInset, len, ch - 2 * segInset - 2, 2);
          else roundRect(ctx, x + segInset, y + at, cw - 2 * segInset - 2, len, 2);
          ctx.fill();
        }
        at += len + gap;
      }
    }
  }
  if (showLbl) {
    const ink = rgbaAt(style.ink, 1) ?? "rgba(230, 230, 238, 1)";
    const fs = w < 64 ? lblFsSm : lblFs;
    ctx.font = `${fs}px system-ui, -apple-system, sans-serif`;
    ctx.textBaseline = "top";
    ctx.fillStyle = ink;
    const label = folded ? `(+${cell.node.count})` : cell.chainLabels ? cell.chainLabels.length > 3 ? `${cell.chainLabels[0]}/\u2026/${cell.chainLabels[cell.chainLabels.length - 1]}` : cell.chainLabels.join("/") : getLabel(cell.node);
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y, cw, ch);
    ctx.clip();
    const pad = 4;
    const kidSize = folded ? cell.node.size : getSize(cell.node);
    const cands = folded || !formatCell ? [formatSize(kidSize)] : candidates(formatCell(cell.node));
    const inlineSize = (hasKids || h <= 34) && w > inlineSizeMinWidth;
    const gap = 6;
    let nameMax = cw - 2 * pad;
    const szText = inlineSize ? fitCandidate(cands, inlineRoom(cw, pad, gap, ctx.measureText(label).width), (t) => ctx.measureText(t).width) : "";
    const szW = inlineSize ? ctx.measureText(szText).width : 0;
    if (inlineSize) nameMax -= szW + gap;
    const name = fit(ctx, label, Math.max(0, nameMax));
    ctx.fillText(name, x + pad, y + 2);
    if (inlineSize) {
      const sx = sizeAlign === "right" ? x + cw - pad - szW : x + pad + ctx.measureText(name).width + gap;
      ctx.globalAlpha = 0.75;
      ctx.fillText(szText, sx, y + 2);
      ctx.globalAlpha = 1;
    }
    if (!hasKids && h > 34) {
      ctx.globalAlpha = 0.75;
      ctx.font = `${lblFsSm}px system-ui, -apple-system, sans-serif`;
      ctx.fillText(fit(ctx, fitCandidate(cands, cw - 2 * pad, (t) => ctx.measureText(t).width), cw - 2 * pad), x + pad, y + 2 + fs + 3);
      ctx.globalAlpha = 1;
    }
    ctx.restore();
  }
  const ring = resolveRing(style.ring);
  if (ring && ring.width > 0) {
    const off = ring.inset ? ring.width / 2 : -ring.width / 2;
    const rw = cw - 2 * off;
    const rh = ch - 2 * off;
    if (rw > 0 && rh > 0) {
      ctx.strokeStyle = ring.color;
      ctx.lineWidth = ring.width;
      ctx.beginPath();
      roundRect(ctx, x + off, y + off, rw, rh, shared ? 0 : dust ? 1.5 : 3);
      ctx.stroke();
    }
  }
}
function roundRect(ctx, x, y, w, h, r) {
  if (w <= 0 || h <= 0) return;
  if (typeof ctx.roundRect === "function") ctx.roundRect(x, y, w, h, Math.min(r, w / 2, h / 2));
  else ctx.rect(x, y, w, h);
}
function fit(ctx, text, maxW) {
  if (maxW <= 0) return "";
  if (ctx.measureText(text).width <= maxW) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = lo + hi + 1 >> 1;
    if (ctx.measureText(text.slice(0, mid) + "\u2026").width <= maxW) lo = mid;
    else hi = mid - 1;
  }
  return lo > 0 ? text.slice(0, lo) + "\u2026" : "";
}

// src/outlines.ts
function mergeIntervals(ivs) {
  if (ivs.length === 0) return [];
  const sorted = [...ivs].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const out = [[...sorted[0]]];
  for (let i = 1; i < sorted.length; i++) {
    const cur = sorted[i];
    const last = out[out.length - 1];
    if (cur[0] <= last[1]) last[1] = Math.max(last[1], cur[1]);
    else out.push([...cur]);
  }
  return out;
}
function symDiff(a, b) {
  const pts = /* @__PURE__ */ new Set();
  for (const [lo, hi] of a) {
    pts.add(lo);
    pts.add(hi);
  }
  for (const [lo, hi] of b) {
    pts.add(lo);
    pts.add(hi);
  }
  const xs = [...pts].sort((p, q2) => p - q2);
  const inSet = (ivs, mid) => ivs.some(([lo, hi]) => lo < mid && mid < hi);
  const out = [];
  for (let i = 0; i + 1 < xs.length; i++) {
    const lo = xs[i];
    const hi = xs[i + 1];
    if (hi <= lo) continue;
    const mid = (lo + hi) / 2;
    const inA = inSet(a, mid);
    const inB = inSet(b, mid);
    if (inA === inB) continue;
    const side = inA ? "a" : "b";
    const last = out[out.length - 1];
    if (last && last.side === side && last.hi === lo) last.hi = hi;
    else out.push({ lo, hi, side });
  }
  return out;
}
var q = (v) => Math.round(v * 8) / 8;
function unionOutline(rects) {
  const segs = [];
  const byX = /* @__PURE__ */ new Map();
  const atX = (x) => {
    let e = byX.get(x);
    if (!e) {
      e = { left: [], right: [] };
      byX.set(x, e);
    }
    return e;
  };
  for (const r of rects) {
    const iv = [q(r.y), q(r.y + r.h)];
    atX(q(r.x)).right.push(iv);
    atX(q(r.x + r.w)).left.push(iv);
  }
  for (const [x, { left, right }] of byX) {
    for (const { lo, hi, side } of symDiff(mergeIntervals(left), mergeIntervals(right))) {
      if (hi <= lo) continue;
      segs.push({ x1: x, y1: lo, x2: x, y2: hi, nx: side === "a" ? -1 : 1, ny: 0 });
    }
  }
  const byY = /* @__PURE__ */ new Map();
  const atY = (y) => {
    let e = byY.get(y);
    if (!e) {
      e = { above: [], below: [] };
      byY.set(y, e);
    }
    return e;
  };
  for (const r of rects) {
    const iv = [q(r.x), q(r.x + r.w)];
    atY(q(r.y)).below.push(iv);
    atY(q(r.y + r.h)).above.push(iv);
  }
  for (const [y, { above, below }] of byY) {
    for (const { lo, hi, side } of symDiff(mergeIntervals(above), mergeIntervals(below))) {
      if (hi <= lo) continue;
      segs.push({ x1: lo, y1: y, x2: hi, y2: y, nx: 0, ny: side === "a" ? -1 : 1 });
    }
  }
  return segs;
}
function groupRects(cells, key) {
  const groups = /* @__PURE__ */ new Map();
  const walk = (cell, openKeys) => {
    const k = isFolded(cell.node) ? null : key(cell.node, cell.path);
    let open = openKeys;
    if (k !== null && !openKeys.has(k)) {
      let rects = groups.get(k);
      if (!rects) {
        rects = [];
        groups.set(k, rects);
      }
      rects.push({ x: cell.x, y: cell.y, w: cell.w, h: cell.h });
      open = new Set(openKeys).add(k);
    }
    for (const c of cell.children) walk(c, open);
  };
  const empty = /* @__PURE__ */ new Set();
  for (const c of cells) walk(c, empty);
  return groups;
}
function groupOutlines(cells, opts) {
  const out = [];
  for (const [key, rects] of groupRects(cells, opts.key)) {
    out.push({ key, color: opts.color(key), segs: unionOutline(rects) });
  }
  return out;
}
function OutlineOverlay({ cells, width, height, groups }) {
  const ref = useRef(null);
  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const dpr = (typeof window !== "undefined" ? window.devicePixelRatio : 1) || 1;
    cv.width = Math.round(width * dpr);
    cv.height = Math.round(height * dpr);
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const lw = groups.width ?? 2;
    ctx.lineWidth = lw;
    ctx.lineCap = "square";
    ctx.lineJoin = "miter";
    const off = groups.inset === false ? 0 : lw / 2;
    const resolve = colorResolver(cv);
    const drawn = [];
    for (const { key, color, segs } of groupOutlines(cells, groups)) {
      if (segs.length === 0) continue;
      drawn.push(key);
      ctx.strokeStyle = resolve(color);
      ctx.beginPath();
      for (const s of segs) {
        const dx = s.nx * off;
        const dy = s.ny * off;
        ctx.moveTo(s.x1 + dx, s.y1 + dy);
        ctx.lineTo(s.x2 + dx, s.y2 + dy);
      }
      ctx.stroke();
    }
    groups.onDrawn?.(drawn);
  }, [cells, width, height, groups]);
  return /* @__PURE__ */ jsx(
    "canvas",
    {
      ref,
      className: "dt-treemap-outlines",
      "aria-hidden": true,
      style: { position: "absolute", left: 0, top: 0, width, height, pointerEvents: "none" }
    }
  );
}

// src/exportImage.ts
var TITLE_PAD = 10;
var TITLE_LINE_H = 22;
var TITLE_FONT = "600 14px system-ui, -apple-system, sans-serif";
function fit2(ctx, text, maxW) {
  if (maxW <= 0) return "";
  if (ctx.measureText(text).width <= maxW) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = lo + hi + 1 >> 1;
    if (ctx.measureText(text.slice(0, mid) + "\u2026").width <= maxW) lo = mid;
    else hi = mid - 1;
  }
  return lo > 0 ? text.slice(0, lo) + "\u2026" : "";
}
function composeExport(source, o) {
  if (!o.title) return source;
  const { dpr } = o;
  const cssW = source.width / dpr;
  const barH = TITLE_LINE_H + TITLE_PAD;
  const out = document.createElement("canvas");
  out.width = source.width;
  out.height = source.height + Math.round(barH * dpr);
  const ctx = out.getContext("2d");
  if (!ctx) return source;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = o.bg;
  ctx.fillRect(0, 0, cssW, barH + source.height / dpr);
  ctx.fillStyle = o.ink;
  ctx.font = TITLE_FONT;
  ctx.textBaseline = "middle";
  ctx.fillText(fit2(ctx, o.title, cssW - 2 * TITLE_PAD), TITLE_PAD, barH / 2);
  ctx.drawImage(source, 0, barH, cssW, source.height / dpr);
  return out;
}
function canvasToPngBlob(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((b) => b ? resolve(b) : reject(new Error("canvas.toBlob returned null")), "image/png");
  });
}
async function copyPng(blob) {
  if (typeof ClipboardItem === "undefined" || !navigator.clipboard?.write) return false;
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    return true;
  } catch {
    return false;
  }
}
function downloadPng(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename.endsWith(".png") ? filename : `${filename}.png`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
function defaultExportFilename(label, when = /* @__PURE__ */ new Date()) {
  const p2 = (n) => String(n).padStart(2, "0");
  const stamp = `${when.getFullYear()}${p2(when.getMonth() + 1)}${p2(when.getDate())}-${p2(when.getHours())}${p2(when.getMinutes())}`;
  const base2 = (label.replace(/\/+$/, "").split("/").pop() || "treemap").replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "");
  return `${base2 || "treemap"}-${stamp}.png`;
}
var base = {
  width: "1em",
  height: "1em",
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.4,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true
};
var CopyIcon = () => /* @__PURE__ */ jsxs("svg", { ...base, children: [
  /* @__PURE__ */ jsx("rect", { x: "5.5", y: "5.5", width: "8", height: "8", rx: "1.5" }),
  /* @__PURE__ */ jsx("path", { d: "M10.5 5.5V4A1.5 1.5 0 0 0 9 2.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5" })
] });
var DownloadIcon = () => /* @__PURE__ */ jsxs("svg", { ...base, children: [
  /* @__PURE__ */ jsx("path", { d: "M8 2.5v7" }),
  /* @__PURE__ */ jsx("path", { d: "M5 6.5 8 9.5l3-3" }),
  /* @__PURE__ */ jsx("path", { d: "M3 12.5h10" })
] });
var CheckIcon = () => /* @__PURE__ */ jsx("svg", { ...base, children: /* @__PURE__ */ jsx("path", { d: "M3.5 8.5 6.5 11.5 12.5 4.5" }) });
var FullscreenIcon = () => /* @__PURE__ */ jsxs("svg", { ...base, children: [
  /* @__PURE__ */ jsx("path", { d: "M3 6V3.5A.5.5 0 0 1 3.5 3H6" }),
  /* @__PURE__ */ jsx("path", { d: "M10 3h2.5a.5.5 0 0 1 .5.5V6" }),
  /* @__PURE__ */ jsx("path", { d: "M13 10v2.5a.5.5 0 0 1-.5.5H10" }),
  /* @__PURE__ */ jsx("path", { d: "M6 13H3.5a.5.5 0 0 1-.5-.5V10" })
] });
var DEFAULT_SLOTS = DEFAULT_PALETTE;
var defaultFormat = (n) => n.toLocaleString("en-US");
var defaultBorderWidth = (depth) => Math.max(1, 2 - depth);
var medianArea = (rs) => {
  if (!rs.length) return 0;
  const a = rs.map((r) => r.w * r.h).sort((x, y) => x - y);
  return a[Math.floor(a.length / 2)];
};
var defaultId = (_n, path, getLabel) => path.map(getLabel).join("/");
var STATUS_STYLE = {
  position: "absolute",
  inset: 0,
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  gap: 2,
  textAlign: "center",
  padding: 12,
  color: "var(--dt-treemap-ink, #d0d0d8)",
  background: "var(--dt-treemap-status-bg, rgba(0,0,0,0.35))",
  pointerEvents: "auto"
};
var LBL_PX = 13.6;
var LBL_PX_SM = 11.5;
var estText = (t, px) => t.length * px * 0.58;
function Treemap({
  root,
  initialPath,
  path: pathProp,
  getSize,
  getChildren,
  hasChildren,
  loadChildren,
  onChildrenLoaded,
  renderLoading,
  renderLoadError,
  getLabel,
  getId,
  formatSize = defaultFormat,
  formatCell,
  collapseChains = false,
  reclaimTitleSpace = false,
  branchOutline,
  colorForCell,
  lens,
  renderCellExtra,
  renderTooltip,
  tipMode = "float",
  renderTipDefault,
  renderRollup,
  renderLegend,
  renderCrumbSuffix,
  renderFooter,
  renderCellSubtitle,
  onCellClick,
  cellHref,
  onPathChange,
  onCellHover,
  minCellArea = 16,
  minCellSide = 7,
  mergeSmall,
  fullscreen = true,
  exportable,
  onExport,
  renderTip,
  chrome = true,
  showLabels = true,
  sizeAlign = "left",
  inlineSizeMinWidth = 90,
  className,
  mapStyle,
  depthFade = 0.82,
  rootFade = 0.92,
  fadeFloor = 0.75,
  tiling = "gaps",
  borderWidth = defaultBorderWidth,
  edgeEmphasis = 0,
  edgeContrast = true,
  dustTexture = true,
  nestedHues = false,
  foldControl = false,
  remainderTail = false,
  renderer = "dom",
  a11yLinks = true,
  a11yMaxCells = 400,
  a11yMinSide = 0,
  outlineGroups
}) {
  const [foldMul, setFoldMul] = useState(1);
  const effMinCellArea = minCellArea == null ? null : minCellArea * foldMul;
  const effMinCellSide = minCellSide == null ? null : minCellSide * Math.sqrt(foldMul);
  const [pathState, setPathState] = useState(initialPath?.[0] === root ? initialPath : [root]);
  const controlled = pathProp !== void 0;
  const path = controlled && pathProp[0] === root ? pathProp : pathState;
  const [tip, setTip] = useState(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const mapRef = useRef(null);
  const wrapRef = useRef(null);
  const titleProbeRef = useRef(null);
  const [titleMetrics, setTitleMetrics] = useState(null);
  useLayoutEffect(() => {
    const probe = titleProbeRef.current;
    if (!reclaimTitleSpace || !probe) return;
    const css = getComputedStyle(probe);
    const sizeCss = getComputedStyle(probe.firstElementChild);
    const metrics = {
      font: css.font || `${css.fontWeight} ${css.fontSize} ${css.fontFamily}`,
      sizeFont: sizeCss.font || `${sizeCss.fontWeight} ${sizeCss.fontSize} ${sizeCss.fontFamily}`,
      canvasFont: `${css.fontSize} system-ui, -apple-system, sans-serif`,
      padding: (parseFloat(css.paddingLeft) || 0) + (parseFloat(css.paddingRight) || 0) + 2,
      tabularNums: sizeCss.fontVariantNumeric.includes("tabular-nums")
    };
    setTitleMetrics((previous) => {
      if (previous && previous.font === metrics.font && previous.sizeFont === metrics.sizeFont && previous.canvasFont === metrics.canvasFont && previous.padding === metrics.padding && previous.tabularNums === metrics.tabularNums) return previous;
      const ctx = previous?.ctx ?? document.createElement("canvas").getContext("2d");
      return ctx ? { ctx, ...metrics } : previous;
    });
  });
  const getTitleWidth = useCallback((n, labels, w, boxW) => {
    if (!titleMetrics || sizeAlign === "right" || renderCellSubtitle) return boxW;
    const name = labels ? labels.length > 3 ? `${labels[0]}/\u2026/${labels[labels.length - 1]}` : labels.join("/") : getLabel(n);
    const sizeText = w > inlineSizeMinWidth ? formatCell ? fitCandidate(candidates(formatCell(n)), inlineRoom(boxW, 4, 6, estText(name, LBL_PX)), (t) => estText(t, LBL_PX)) : formatSize(getSize(n)) : "";
    const { ctx, font, sizeFont, canvasFont, padding, tabularNums } = titleMetrics;
    const measure = (nameFont, valueFont) => {
      ctx.font = nameFont;
      const nameWidth = ctx.measureText(name).width;
      ctx.font = valueFont;
      const digitWidth = tabularNums ? Math.max(..."0123456789".split("").map((d) => ctx.measureText(d).width)) : 0;
      const sizeWidth = tabularNums ? [...sizeText].reduce((sum, ch) => sum + (/\d/.test(ch) ? digitWidth : ctx.measureText(ch).width), 0) : ctx.measureText(sizeText).width;
      return nameWidth + (sizeText ? 6 + sizeWidth : 0);
    };
    return Math.min(boxW, padding + Math.max(measure(font, sizeFont), measure(canvasFont, canvasFont)));
  }, [titleMetrics, sizeAlign, renderCellSubtitle, getLabel, inlineSizeMinWidth, formatSize, formatCell, getSize]);
  const tipRef = useRef(null);
  const canvasElRef = useRef(null);
  const [flash, setFlash] = useState(null);
  const flashTimer = useRef(void 0);
  useEffect(() => () => {
    if (flashTimer.current) clearTimeout(flashTimer.current);
  }, []);
  const tipClear = useRef(void 0);
  const hoverKeyRef = useRef(null);
  const cancelTipClear = () => {
    if (tipClear.current) {
      clearTimeout(tipClear.current);
      tipClear.current = void 0;
    }
  };
  const pin = useHoverPin({ excludeRefs: [tipRef] });
  const [pinnedTip, setPinnedTip] = useState(null);
  useEffect(() => {
    if (pin.pinned === null) setPinnedTip(null);
  }, [pin.pinned]);
  const node = path[path.length - 1];
  const go = useCallback(
    (p) => {
      if (!controlled) setPathState(p);
      setTip(null);
      if (controlled) onPathChange?.(p);
    },
    [controlled, onPathChange]
  );
  const mountedRoot = useRef(root);
  useEffect(() => {
    if (controlled || mountedRoot.current === root) return;
    mountedRoot.current = root;
    setPathState(initialPath?.[0] === root ? initialPath : [root]);
    setTip(null);
  }, [root]);
  useEffect(() => {
    if (!controlled) onPathChange?.(path);
  }, [controlled, path, onPathChange]);
  useLayoutEffect(() => {
    const el = mapRef.current;
    if (!el) return;
    setSize({ w: el.clientWidth, h: el.clientHeight });
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    const onKey = (e) => {
      if (e.defaultPrevented) return;
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if ((e.key === "Backspace" || e.key === "Escape") && path.length > 1) {
        go(path.slice(0, -1));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [path, go]);
  const idFor = useCallback(
    (n, p) => getId ? getId(n, p) : defaultId(n, p, getLabel),
    [getId, getLabel]
  );
  const [fetched, setFetched] = useState(() => /* @__PURE__ */ new Map());
  const [pending, setPending] = useState(null);
  const [failed, setFailed] = useState(null);
  const [retries, setRetries] = useState(0);
  useEffect(() => {
    setFetched(/* @__PURE__ */ new Map());
    setPending(null);
    setFailed(null);
  }, [root]);
  const childrenOf = useCallback(
    (n, p) => getChildren(n) ?? fetched.get(idFor(n, p)),
    [getChildren, fetched, idFor]
  );
  const expandable = useCallback(
    (n, p) => (childrenOf(n, p)?.length ?? 0) > 0 || !!loadChildren && !!hasChildren?.(n),
    [childrenOf, loadChildren, hasChildren]
  );
  const viewKey = idFor(node, path);
  const viewNeedsLoad = !!loadChildren && !getChildren(node)?.length && !fetched.has(viewKey) && !!hasChildren?.(node);
  useEffect(() => {
    if (!viewNeedsLoad || !loadChildren) return;
    let live = true;
    setPending(viewKey);
    setFailed(null);
    loadChildren(node, path).then(
      (kids) => {
        if (!live) return;
        setFetched((m) => m.has(viewKey) ? m : new Map(m).set(viewKey, kids));
        setPending((p) => p === viewKey ? null : p);
        onChildrenLoaded?.(node, path, kids);
      },
      (e) => {
        if (!live) return;
        setPending((p) => p === viewKey ? null : p);
        setFailed({ key: viewKey, error: e instanceof Error ? e : new Error(String(e)) });
      }
    );
    return () => {
      live = false;
    };
  }, [viewKey, viewNeedsLoad, retries]);
  const retry = useCallback(() => {
    setFailed(null);
    setRetries((r) => r + 1);
  }, []);
  const topLevelSlot = useMemo(() => {
    const kids = childrenOf(root, [root]) ?? [];
    return new Map(kids.map((k, i) => [getLabel(k), slotColor(i, DEFAULT_SLOTS)]));
  }, [root, childrenOf, getLabel]);
  const mergeItems = useCallback(
    (small) => {
      if (mergeSmall) return mergeSmall(small);
      const flat = [];
      let sum = 0;
      for (const s of small) {
        if (isFolded(s)) {
          flat.push(...s.children);
          sum += s.size;
        } else {
          flat.push(s);
          sum += getSize(s);
        }
      }
      return { __folded: true, count: flat.length, size: sum, children: flat };
    },
    [getSize, mergeSmall]
  );
  const fold = useCallback(
    (raw, w, h) => {
      if (effMinCellArea == null) return raw;
      const sz = (it) => isFolded(it) ? it.size : getSize(it);
      return foldSmall(raw, w, h, sz, mergeItems, effMinCellArea);
    },
    [getSize, effMinCellArea, mergeItems]
  );
  const children = useMemo(
    () => fold((childrenOf(node, path) ?? []).slice(), size.w, size.h),
    [node, path, size, childrenOf, fold]
  );
  const layTiles = useCallback(
    (items, x, y, w, h) => {
      const sz = (n) => isFolded(n) ? n.size : getSize(n);
      if (remainderTail) {
        const frac = typeof remainderTail === "number" ? remainderTail : 0.14;
        return squarifyRemainder(items, x, y, w, h, sz, effMinCellSide ?? 7, frac);
      }
      return squarify(items, x, y, w, h, sz);
    },
    [getSize, remainderTail, effMinCellSide]
  );
  const rects = useMemo(
    () => {
      const laid = layTiles(children, 0, 0, size.w, size.h);
      if (!remainderTail && effMinCellSide != null) {
        const sz = (n) => isFolded(n) ? n.size : getSize(n);
        const refolded = foldThin(laid, effMinCellSide, mergeItems);
        if (refolded) return squarify(refolded, 0, 0, size.w, size.h, sz);
      }
      return laid;
    },
    [children, size, getSize, remainderTail, effMinCellSide, mergeItems, layTiles]
  );
  const fadeAt = useCallback(
    (d) => Math.max(rootFade * depthFade ** d, fadeFloor),
    [rootFade, depthFade, fadeFloor]
  );
  const tilingFor = useCallback(
    (n, p, depth, w, h, rs) => typeof tiling === "function" ? tiling(n, p, depth, { w, h, nChildren: rs.length, medianChildArea: medianArea(rs) }) : tiling,
    [tiling]
  );
  const rootMode = tilingFor(node, path, 0, size.w, size.h, rects);
  const placedCells = useMemo(() => {
    if (renderer !== "canvas" && !outlineGroups && !branchOutline) return [];
    const cfg = {
      getSize,
      getLabel,
      childrenOf,
      showLabels,
      collapseChains,
      borderWidth,
      edgeEmphasis,
      fold,
      layTiles,
      tilingFor,
      getTitleWidth: reclaimTitleSpace ? getTitleWidth : void 0
    };
    return layoutCells(rects, path, rootMode, cfg);
  }, [
    renderer,
    !!outlineGroups,
    branchOutline,
    rects,
    path,
    rootMode,
    getSize,
    getLabel,
    childrenOf,
    showLabels,
    collapseChains,
    borderWidth,
    edgeEmphasis,
    fold,
    layTiles,
    tilingFor,
    reclaimTitleSpace,
    getTitleWidth
  ]);
  const styleOpts = useMemo(
    () => ({ colorForCell, lens, getLabel, topLevelSlot, defaultSlots: DEFAULT_SLOTS, dustTexture, edgeContrast, nestedHues, fadeAt }),
    [colorForCell, lens, getLabel, topLevelSlot, dustTexture, edgeContrast, nestedHues, fadeAt]
  );
  const activateHover = (node2, path2, key, x, y) => {
    cancelTipClear();
    pin.hover(key);
    if (hoverKeyRef.current !== key) {
      hoverKeyRef.current = key;
      onCellHover?.(node2, path2);
    }
    setTip((prev) => prev?.key === key ? prev : { x, y, key, node: node2, path: path2 });
  };
  const clearHover = () => {
    if (hoverKeyRef.current !== null) {
      hoverKeyRef.current = null;
      onCellHover?.(null, []);
    }
  };
  useEffect(() => {
    if (!tip || pinnedTip) return;
    const onMove = (e) => {
      const t = e.target;
      if (t?.closest?.(".dt-treemap-cell, .dt-treemap-tip, .dt-treemap-map, .dt-treemap-canvas")) return;
      cancelTipClear();
      pin.hover(null);
      clearHover();
      setTip(null);
    };
    document.addEventListener("mousemove", onMove);
    return () => document.removeEventListener("mousemove", onMove);
  }, [!!tip, !!pinnedTip]);
  const activatePin = (node2, path2, key, x, y) => {
    const ax = tip?.key === key ? tip.x : x;
    const ay = tip?.key === key ? tip.y : y;
    pin.togglePin(key);
    setPinnedTip((p) => p?.key === key ? null : { x: ax, y: ay, key, node: node2, path: path2 });
  };
  const activateClick = (node2, path2, key, drillable, x, y, e) => {
    if (onCellClick && onCellClick(node2, path2, e)) return;
    if (drillable && !e.altKey) {
      pin.clearPin();
      go(path2);
    } else activatePin(node2, path2, key, x, y);
  };
  const cell = (kid0, kidPath0, r, depth, mode) => {
    const folded = isFolded(kid0);
    const chained = collapseChains && !folded ? (() => {
      let cur = kid0;
      let p = kidPath0;
      const labels = [getLabel(cur)];
      for (; ; ) {
        const only = childrenOf(cur, p);
        if (!only || only.length !== 1) break;
        cur = only[0];
        p = [...p, cur];
        labels.push(getLabel(cur));
      }
      return labels.length > 1 ? { node: cur, path: p, labels } : null;
    })() : null;
    const kid = chained ? chained.node : kid0;
    const kidPath = chained ? chained.path : kidPath0;
    const chainLabels = chained?.labels ?? null;
    const kidSize = isFolded(kid) ? kid.size : getSize(kid);
    const kidLabel = folded ? `(+${kid.count})` : chainLabels ? chainLabels.length > 3 ? `${chainLabels[0]}/\u2026/${chainLabels[chainLabels.length - 1]}` : chainLabels.join("/") : getLabel(kid);
    const sizeCands = folded || !formatCell ? [formatSize(kidSize)] : candidates(formatCell(kid));
    const lblPx = r.w < 64 ? LBL_PX_SM : LBL_PX;
    const sizeInline = fitCandidate(sizeCands, inlineRoom(r.w, 4, 6, estText(kidLabel, lblPx)), (t) => estText(t, lblPx));
    const sizeSecond = fitCandidate(sizeCands, r.w - 8, (t) => estText(t, LBL_PX_SM));
    const kidChildren = folded ? void 0 : childrenOf(kid, kidPath);
    const kidDrillable = !folded && expandable(kid, kidPath);
    const showLbl = showLabels && r.w > 36 && r.h > 13;
    const dust = Math.min(r.w, r.h) < 14;
    const shared = mode === "shared";
    const bw = shared ? Math.min(borderWidth(depth, { w: r.w, h: r.h }) * edgeEmphFactor(depth, edgeEmphasis), dust ? 1 : Infinity) : 0;
    const edge = bw / 2;
    let builtinEdge = null;
    const boxW = shared ? r.w : r.w - (dust ? 1 : 2);
    const boxH = shared ? r.h : r.h - (dust ? 1 : 2);
    const { rects: kids, mode: kidsMode, titleWidth } = layoutChildren(
      kid,
      kidPath,
      chainLabels,
      kidChildren,
      { w: r.w, h: r.h, boxW, boxH, edge, showLbl, depth },
      {
        getSize,
        getLabel,
        childrenOf,
        showLabels,
        collapseChains,
        borderWidth,
        edgeEmphasis,
        fold,
        layTiles,
        tilingFor,
        getTitleWidth: reclaimTitleSpace ? getTitleWidth : void 0
      }
    );
    const explicit = folded ? null : colorForCell?.(kid, kidPath, depth, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0 });
    let style;
    if (explicit) {
      style = explicit;
    } else if (folded) {
      style = dustTexture ? { bg: "var(--dt-treemap-folded-ground, rgba(120, 120, 135, 0.12))", ink: "var(--dt-treemap-folded-ink, #d0d0d8)" } : { bg: "var(--dt-treemap-folded, #4a4a52)", ink: "var(--dt-treemap-folded-ink, #d0d0d8)" };
    } else {
      style = categoricalStyle(kidPath, kids.length > 0, getLabel, topLevelSlot, DEFAULT_SLOTS, nestedHues);
    }
    if (lens && !folded) {
      style = lens(kid, kidPath, depth, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0 }, style) ?? style;
    }
    if (shared && edgeContrast && !style.edge) {
      builtinEdge = contrastEdge(style.bg, fadeAt(depth));
    }
    const ring = resolveRing(style.ring);
    const cellKey = folded ? `__folded_${depth}_${r.x}_${r.y}` : idFor(kid, kidPath);
    const foldedNode = folded ? kid : null;
    const dustHits = foldedNode && dustTexture && foldedNode.children.length > 1 && Math.min(r.w, r.h) >= 10 && foldedNode.children.length <= 4e3 ? squarify(foldedNode.children, 0, 0, boxW, boxH, getSize) : null;
    const dustHitAt = (e) => {
      if (!dustHits) return null;
      const box = e.currentTarget.getBoundingClientRect();
      const lx = e.clientX - box.left;
      const ly = e.clientY - box.top;
      const hit = dustHits.find((rc) => lx >= rc.x && lx < rc.x + rc.w && ly >= rc.y && ly < rc.y + rc.h);
      if (!hit) return null;
      const p = [...kidPath, hit.it];
      return { it: hit.it, path: p, key: idFor(hit.it, p) };
    };
    const showTip = (e) => {
      e.stopPropagation();
      const b = e.currentTarget.getBoundingClientRect();
      if (folded) {
        const hit = dustHitAt(e);
        if (!hit) return;
        activateHover(hit.it, hit.path, hit.key, b.left, b.top);
        return;
      }
      activateHover(kid, kidPath, cellKey, b.left, b.top);
    };
    const href = cellHref && !folded && kids.length === 0 ? cellHref(kid, kidPath) : void 0;
    const onClick = (e) => {
      e.stopPropagation();
      if (folded) {
        const hit = dustHitAt(e);
        if (!hit) return;
        activatePin(hit.it, hit.path, hit.key, e.clientX, e.clientY);
        return;
      }
      if (href) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1) return;
        e.preventDefault();
      }
      activateClick(kid, kidPath, cellKey, kidDrillable, e.clientX, e.clientY, e);
    };
    const chromeOk = Math.min(r.w, r.h) >= 28;
    const CellTag = href ? "a" : "div";
    return /* @__PURE__ */ jsxs(
      CellTag,
      {
        ...href && { href },
        className: "dt-treemap-cell" + (kidDrillable && (kids.length > 0 || chromeOk) ? " branch" : "") + (dust ? " dust" : "") + (chainLabels && chromeOk ? " chain" : "") + (shared ? " shared" : "") + (cellKey === pinnedTip?.key ? " pinned" : ""),
        style: {
          position: "absolute",
          left: r.x,
          top: r.y,
          // Gaps: 2px gutter (1px for dust) around the squarify rect.
          // Shared: the exact rect — neighbors abut, the stroke is the gutter.
          width: Math.max(0, shared ? r.w : r.w - (dust ? 1 : 2)),
          height: Math.max(0, shared ? r.h : r.h - (dust ? 1 : 2)),
          // Opaque base under the faded paint layer: a cell's fade recedes
          // toward the container color, and ancestor bg never shows through
          // descendants — it surfaces only in title bars and gutters. Stays
          // the container color in both modes: the paint layer is translucent
          // (depth fade), so a tinted base would wash every cell toward it.
          background: "var(--dt-treemap-container-bg, #202024)",
          // Shared: this cell's half of the shared stroke, painted in the ring
          // the inset paint layer leaves bare (an inset shadow under a
          // full-bleed paint layer would never show). Gaps: an outer ring in
          // the gutter, transparent by default so dark-palette consumers can
          // opt into brighter sibling separation via the var.
          // (boxShadow, not outline — :focus owns the outline.)
          // A consumer `ring` (brush/selection emphasis) stacks first (on top)
          // so it reads over the structural gutter, in either tiling mode; it
          // follows the cell's `borderRadius` automatically.
          boxShadow: [
            ring && `${ring.inset ? "inset " : ""}0 0 0 ${ring.width}px ${ring.color}`,
            shared ? `inset 0 0 0 ${edge}px ${style.edge ?? builtinEdge ?? "var(--dt-treemap-edge, var(--dt-treemap-container-bg, #202024))"}` : "0 0 0 1px var(--dt-treemap-cell-border, transparent)"
          ].filter(Boolean).join(", "),
          // Anchors must not fall through to the page's link color when the
          // consumer sets no ink.
          color: style.ink ?? (href ? "inherit" : void 0),
          borderRadius: shared ? 0 : dust ? 1.5 : 3,
          overflow: "hidden",
          boxSizing: "border-box",
          cursor: href || kidDrillable ? "pointer" : "default",
          ...href && { textDecoration: "none" }
        },
        tabIndex: folded ? -1 : 0,
        onMouseMove: kids.length > 0 ? void 0 : showTip,
        onClick,
        onKeyDown: (e) => e.key === "Enter" && onClick(e),
        children: [
          /* @__PURE__ */ jsxs(
            "div",
            {
              className: "dt-treemap-bg",
              style: {
                position: "absolute",
                // An inset `ring` is a box-shadow on the cell box, under this layer: leave it bare too, or it
                // would only show as far as the shared edge (and not at all in gaps mode).
                inset: Math.max(shared ? edge : 0, ring?.inset ? ring.width : 0),
                background: style.bg,
                ...style.hatch && { backgroundImage: style.hatch },
                // Dashed frame marks the dust tile as not-a-real-cell.
                ...foldedNode && dustTexture && Math.min(r.w, r.h) >= 8 && {
                  border: "1px dashed var(--dt-treemap-folded-edge, rgba(150, 150, 165, 0.55))",
                  boxSizing: "border-box"
                },
                opacity: fadeAt(depth) * (style.opacity ?? 1),
                pointerEvents: "none"
              },
              children: [
                foldedNode && dustTexture && Math.min(r.w, r.h) >= 6 && /* @__PURE__ */ jsx(DustHatch, { w: boxW, h: boxH, count: foldedNode.count }),
                style.segments && style.segments.length > 1 && kids.length === 0 && !dust && Math.min(r.w, r.h) >= 18 && (() => {
                  const inset = shared ? Math.max(1, bw) : 3;
                  const gap = 1;
                  const horiz = r.w >= r.h;
                  const span = (horiz ? r.w : r.h) - 2 * inset - gap * (style.segments.length - 1);
                  if (span < style.segments.length * 2) return null;
                  const total = style.segments.reduce((s, x) => s + x.frac, 0) || 1;
                  let at = inset;
                  return style.segments.map((s, i) => {
                    const len = s.frac / total * span;
                    const rect = horiz ? { left: at, top: inset, width: len, height: Math.max(0, r.h - 2 * inset - 2) } : { left: inset, top: at, width: Math.max(0, r.w - 2 * inset - 2), height: len };
                    at += len + gap;
                    return /* @__PURE__ */ jsx("div", { style: {
                      position: "absolute",
                      ...rect,
                      background: s.color,
                      borderRadius: 2,
                      pointerEvents: "none"
                    } }, i);
                  });
                })()
              ]
            }
          ),
          showLbl && /* @__PURE__ */ jsxs(
            "div",
            {
              className: "dt-treemap-lbl" + (r.w < 64 ? " sm" : ""),
              style: {
                position: "relative",
                width: titleWidth ?? void 0,
                boxSizing: "border-box",
                zIndex: 1,
                // above makeup stripes (positioned siblings)
                padding: "var(--dt-treemap-lbl-pad, 2px 4px)",
                fontSize: r.w < 64 ? "var(--dt-treemap-lbl-fs-sm, 0.72rem)" : "var(--dt-treemap-lbl-fs, 0.85rem)",
                lineHeight: 1.2,
                // Flex, not one nowrap run: a long name ellipsizes while the
                // size span keeps its width, instead of pushing it out of view.
                display: "flex",
                alignItems: "baseline",
                gap: 6,
                overflow: "hidden",
                // branch title bar is the parent's own hover target (leaf labels
                // stay pointer-events:none so the body handles them)
                pointerEvents: kids.length > 0 ? "auto" : "none"
              },
              onMouseMove: kids.length > 0 ? showTip : void 0,
              children: [
                /* @__PURE__ */ jsx("span", { style: { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }, children: kidLabel }),
                (kids.length > 0 || r.h <= 34) && r.w > inlineSizeMinWidth && /* @__PURE__ */ jsxs("span", { className: "sz", style: { opacity: 0.75, whiteSpace: "nowrap", flex: "none", marginLeft: sizeAlign === "right" ? "auto" : void 0 }, children: [
                  sizeInline,
                  !folded && renderCellSubtitle && /* @__PURE__ */ jsx("span", { style: { marginLeft: 4 }, children: renderCellSubtitle(kid, kidPath, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0 }) })
                ] })
              ]
            }
          ),
          showLbl && kids.length === 0 && r.h > 34 && /* @__PURE__ */ jsxs(
            "div",
            {
              className: "dt-treemap-lbl2",
              style: {
                position: "relative",
                zIndex: 1,
                padding: "0 4px",
                fontSize: "var(--dt-treemap-lbl-fs-sm, 0.72rem)",
                lineHeight: 1.2,
                opacity: 0.75,
                whiteSpace: "nowrap",
                overflow: "hidden",
                pointerEvents: "none"
              },
              children: [
                sizeSecond,
                !folded && renderCellSubtitle && /* @__PURE__ */ jsx("span", { style: { marginLeft: 4 }, children: renderCellSubtitle(kid, kidPath, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0 }) })
              ]
            }
          ),
          !folded && renderCellExtra && renderCellExtra(kid, kidPath, { w: r.w, h: r.h, fade: fadeAt(depth), hasKids: kids.length > 0, chain: chainLabels ? chainLabels.length - 1 : 0 }),
          kids.length > 0 && kidChildren && /* @__PURE__ */ jsx(
            "div",
            {
              className: "dt-treemap-inner",
              style: {
                position: "absolute",
                inset: kidsMode === "shared" ? `${showLbl ? 20 : edge}px ${edge}px ${edge}px ${edge}px` : `${showLbl ? 20 : 3}px 3px 3px 3px`
              },
              children: kids.filter((s) => s.w >= 3 && s.h >= 3).map((s) => cell(s.it, isFolded(s.it) ? kidPath : [...kidPath, s.it], s, depth + 1, kidsMode))
            }
          )
        ]
      },
      cellKey
    );
  };
  const goFullscreen = () => {
    const el = wrapRef.current;
    if (!el) return;
    if (document.fullscreenElement) void document.exitFullscreen();
    else void el.requestFullscreen();
  };
  const exportOpts = exportable ? exportable === true ? {} : exportable : null;
  const doExport = useCallback(
    async (kind) => {
      if (!exportOpts || !mapRef.current) return;
      const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
      let src = canvasElRef.current;
      if (!src) {
        if (size.w <= 0 || size.h <= 0) return;
        const cfg = {
          getSize,
          getLabel,
          childrenOf,
          showLabels,
          collapseChains,
          borderWidth,
          edgeEmphasis,
          fold,
          layTiles,
          tilingFor,
          getTitleWidth: reclaimTitleSpace ? getTitleWidth : void 0
        };
        const cells = layoutCells(rects, path, rootMode, cfg);
        const off = document.createElement("canvas");
        renderMapToCanvas(
          off,
          cells,
          size.w,
          size.h,
          { styleOpts, getSize, getLabel, formatSize, formatCell, sizeAlign, inlineSizeMinWidth, dustTexture },
          mapRef.current
        );
        src = off;
      }
      if (branchOutline) {
        const framed = document.createElement("canvas");
        framed.width = src.width;
        framed.height = src.height;
        const ctx = framed.getContext("2d");
        ctx.drawImage(src, 0, 0);
        ctx.scale(src.width / size.w, src.height / size.h);
        ctx.strokeStyle = colorResolver(mapRef.current)(branchOutline);
        ctx.lineWidth = 1;
        for (const c of flattenPlaced(placedCells)) if (c.hasKids) ctx.strokeRect(c.x + 0.5, c.y + 0.5, Math.max(0, c.boxW - 1), Math.max(0, c.boxH - 1));
        src = framed;
      }
      const cs = getComputedStyle(mapRef.current);
      const ink = cs.color || "rgb(230, 230, 238)";
      const transparent = (c) => !c || c === "transparent" || c === "rgba(0, 0, 0, 0)";
      let bg = cs.backgroundColor;
      if (transparent(bg) && typeof document !== "undefined") bg = getComputedStyle(document.body).backgroundColor;
      if (transparent(bg)) bg = `rgb(${CONTAINER_BG[0]}, ${CONTAINER_BG[1]}, ${CONTAINER_BG[2]})`;
      const title = exportOpts.title ? `${path.map(getLabel).join("/")} \u2014 ${formatSize(getSize(node))}` : null;
      const out = composeExport(src, { title, bg, ink, dpr });
      const blob = await canvasToPngBlob(out);
      const filename = exportOpts.filename?.({ node, path }) ?? defaultExportFilename(getLabel(node));
      let outcome = kind;
      if (kind === "copy") {
        const copied = await copyPng(blob);
        if (!copied) {
          downloadPng(blob, filename);
          outcome = "download";
        }
      } else {
        downloadPng(blob, filename);
      }
      setFlash(outcome);
      if (flashTimer.current) clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setFlash(null), 1600);
      onExport?.(blob, { kind: outcome });
    },
    [
      exportOpts,
      path,
      node,
      getLabel,
      getSize,
      formatSize,
      onExport,
      size,
      rects,
      rootMode,
      styleOpts,
      sizeAlign,
      inlineSizeMinWidth,
      dustTexture,
      branchOutline,
      placedCells,
      childrenOf,
      showLabels,
      collapseChains,
      borderWidth,
      edgeEmphasis,
      fold,
      layTiles,
      tilingFor,
      reclaimTitleSpace,
      getTitleWidth
    ]
  );
  const showExport = !!exportOpts;
  const withTip = (label, button) => renderTip ? renderTip(label, button) : button;
  const iconBtn = {
    background: "transparent",
    border: "none",
    color: "inherit",
    cursor: "pointer",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    padding: 2,
    lineHeight: 0,
    fontSize: "1.05em"
  };
  const tipToShow = pinnedTip ?? tip;
  const [tipDims, setTipDims] = useState(null);
  useLayoutEffect(() => {
    const el = tipRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setTipDims((d) => d && Math.abs(d.w - r.width) < 1 && Math.abs(d.h - r.height) < 1 ? d : { w: r.width, h: r.height });
  });
  const tipContent = tipToShow && renderTooltip ? renderTooltip(tipToShow.node, tipToShow.path) : tipToShow ? /* @__PURE__ */ jsxs(Fragment, { children: [
    /* @__PURE__ */ jsx("div", { style: { fontWeight: 500 }, children: getLabel(tipToShow.node) }),
    /* @__PURE__ */ jsx("div", { style: { opacity: 0.75, fontSize: "0.85em" }, children: formatSize(getSize(tipToShow.node)) })
  ] }) : null;
  return /* @__PURE__ */ jsxs(
    "div",
    {
      className: "dt-treemap" + (className ? ` ${className}` : ""),
      ref: wrapRef,
      onKeyDown: showExport ? ((e) => {
        if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === "c" || e.key === "C")) {
          e.preventDefault();
          void doExport("copy");
        }
      }) : void 0,
      style: { display: "flex", flexDirection: "column", width: "100%", height: "100%" },
      children: [
        reclaimTitleSpace && /* @__PURE__ */ jsx("div", { "aria-hidden": "true", className: "dt-treemap-cell branch", style: {
          position: "absolute",
          visibility: "hidden",
          pointerEvents: "none"
        }, children: /* @__PURE__ */ jsx("div", { ref: titleProbeRef, className: "dt-treemap-lbl", style: {
          padding: "var(--dt-treemap-lbl-pad, 2px 4px)",
          fontSize: "var(--dt-treemap-lbl-fs, 0.85rem)",
          lineHeight: 1.2
        }, children: /* @__PURE__ */ jsx("span", { className: "sz" }) }) }),
        chrome && /* @__PURE__ */ jsxs(
          "div",
          {
            className: "dt-treemap-bar",
            style: { display: "flex", alignItems: "center", gap: 8, padding: "2px 6px", minHeight: 22 },
            children: [
              /* @__PURE__ */ jsxs(
                "nav",
                {
                  className: "dt-treemap-crumbs",
                  "aria-label": "Path",
                  style: { display: "flex", gap: 3, alignItems: "center", flexWrap: "wrap", flex: 1, minWidth: 0 },
                  children: [
                    path.map((n, i) => /* @__PURE__ */ jsxs("span", { style: { display: "inline-flex", alignItems: "center", gap: 3 }, children: [
                      i > 0 && /* @__PURE__ */ jsx("span", { style: { opacity: 0.4 }, children: "/" }),
                      i < path.length - 1 ? /* @__PURE__ */ jsx(
                        "a",
                        {
                          tabIndex: 0,
                          role: "link",
                          style: { cursor: "pointer", textDecoration: "underline" },
                          onClick: () => go(path.slice(0, i + 1)),
                          children: getLabel(n)
                        }
                      ) : /* @__PURE__ */ jsx("span", { style: { fontWeight: 500 }, children: getLabel(n) })
                    ] }, i)),
                    /* @__PURE__ */ jsx("span", { style: { opacity: 0.6, marginLeft: 6, whiteSpace: "nowrap" }, children: renderCrumbSuffix ? renderCrumbSuffix(node, path) : /* @__PURE__ */ jsxs(Fragment, { children: [
                      "\u2014 ",
                      formatSize(getSize(node))
                    ] }) })
                  ]
                }
              ),
              renderLegend && /* @__PURE__ */ jsx("div", { className: "dt-treemap-legend", children: renderLegend(node, path) }),
              foldControl && (minCellArea != null || minCellSide != null) && /* @__PURE__ */ jsxs(
                "label",
                {
                  className: "dt-treemap-fold",
                  title: "Detail: fold fewer cells (fine) \u2194 more (coarse)",
                  style: { display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap", fontSize: "0.8em", opacity: 0.75 },
                  children: [
                    /* @__PURE__ */ jsx("span", { children: "detail" }),
                    /* @__PURE__ */ jsx(
                      "input",
                      {
                        type: "range",
                        min: 0,
                        max: 1,
                        step: 0.02,
                        value: (2 - Math.log2(foldMul)) / 4,
                        onChange: (e) => setFoldMul(2 ** (2 - 4 * +e.target.value)),
                        style: { width: 72 }
                      }
                    )
                  ]
                }
              ),
              showExport && (() => {
                const okStyle = { ...iconBtn, color: "var(--dt-treemap-ok, #3fb950)" };
                const copyLabel = flash === "copy" ? "Copied \u2713" : "Copy PNG to clipboard (\u2318/Ctrl+Shift+C)";
                const dlLabel = flash === "download" ? "Saved \u2713" : "Download PNG";
                return /* @__PURE__ */ jsxs(Fragment, { children: [
                  withTip(copyLabel, /* @__PURE__ */ jsx(
                    "button",
                    {
                      className: "dt-treemap-export-copy",
                      onClick: () => void doExport("copy"),
                      title: renderTip ? void 0 : copyLabel,
                      "aria-label": "Copy image to clipboard",
                      style: flash === "copy" ? okStyle : iconBtn,
                      children: flash === "copy" ? /* @__PURE__ */ jsx(CheckIcon, {}) : /* @__PURE__ */ jsx(CopyIcon, {})
                    }
                  )),
                  withTip(dlLabel, /* @__PURE__ */ jsx(
                    "button",
                    {
                      className: "dt-treemap-export-dl",
                      onClick: () => void doExport("download"),
                      title: renderTip ? void 0 : dlLabel,
                      "aria-label": "Download image",
                      style: flash === "download" ? okStyle : iconBtn,
                      children: flash === "download" ? /* @__PURE__ */ jsx(CheckIcon, {}) : /* @__PURE__ */ jsx(DownloadIcon, {})
                    }
                  ))
                ] });
              })(),
              fullscreen && withTip("Toggle fullscreen", /* @__PURE__ */ jsx(
                "button",
                {
                  className: "dt-treemap-fs",
                  onClick: goFullscreen,
                  title: renderTip ? void 0 : "Toggle fullscreen",
                  "aria-label": "Toggle fullscreen",
                  style: iconBtn,
                  children: /* @__PURE__ */ jsx(FullscreenIcon, {})
                }
              ))
            ]
          }
        ),
        (() => {
          const r = renderRollup?.(node, path);
          return r ? /* @__PURE__ */ jsx("div", { className: "dt-treemap-rollup", children: r }) : null;
        })(),
        /* @__PURE__ */ jsxs(
          "div",
          {
            className: "dt-treemap-map",
            ref: mapRef,
            role: "application",
            "aria-label": "Treemap",
            style: { position: "relative", flex: 1, minHeight: 0, ...mapStyle },
            onMouseLeave: () => {
              cancelTipClear();
              tipClear.current = setTimeout(() => {
                pin.hover(null);
                clearHover();
                setTip(null);
              }, 180);
            },
            children: [
              renderer === "canvas" ? size.w > 0 && size.h > 0 && /* @__PURE__ */ jsx(
                TreemapCanvas,
                {
                  cells: placedCells,
                  width: size.w,
                  height: size.h,
                  styleOpts,
                  getSize,
                  getLabel,
                  formatSize,
                  formatCell,
                  sizeAlign,
                  idFor,
                  expandable,
                  dustTexture,
                  cellHref,
                  inlineSizeMinWidth,
                  a11yLinks,
                  a11yMaxCells,
                  a11yMinSide,
                  pinnedKey: pinnedTip?.key ?? null,
                  canvasRef: exportable ? canvasElRef : void 0,
                  onHover: (hit, x, y) => activateHover(hit.node, hit.path, hit.key, x, y),
                  onClick: (hit, e) => hit.foldChild ? activatePin(hit.node, hit.path, hit.key, e.clientX, e.clientY) : activateClick(hit.node, hit.path, hit.key, hit.drillable, e.clientX, e.clientY, e),
                  onLeave: () => {
                    cancelTipClear();
                    tipClear.current = setTimeout(() => {
                      pin.hover(null);
                      clearHover();
                      setTip(null);
                    }, 180);
                  }
                }
              ) : rects.filter((r) => r.w >= 3 && r.h >= 3).map((r) => cell(r.it, isFolded(r.it) ? path : [...path, r.it], r, 0, rootMode)),
              outlineGroups && size.w > 0 && size.h > 0 && /* @__PURE__ */ jsx(OutlineOverlay, { cells: placedCells, width: size.w, height: size.h, groups: outlineGroups }),
              branchOutline && /* @__PURE__ */ jsx("svg", { className: "dt-treemap-branch-frames", width: size.w, height: size.h, "aria-hidden": true, style: { position: "absolute", inset: 0, pointerEvents: "none", zIndex: 1 }, children: flattenPlaced(placedCells).filter((c) => c.hasKids).map((c, i) => /* @__PURE__ */ jsx("rect", { x: c.x + 0.5, y: c.y + 0.5, width: Math.max(0, c.boxW - 1), height: Math.max(0, c.boxH - 1), fill: "none", stroke: branchOutline, strokeWidth: 1 }, i)) }),
              failed?.key === viewKey ? /* @__PURE__ */ jsx("div", { className: "dt-treemap-status error", style: STATUS_STYLE, children: renderLoadError ? renderLoadError(node, path, failed.error, retry) : /* @__PURE__ */ jsxs(Fragment, { children: [
                /* @__PURE__ */ jsxs("div", { children: [
                  "Couldn\u2019t load ",
                  getLabel(node),
                  ": ",
                  failed.error.message
                ] }),
                /* @__PURE__ */ jsx("button", { onClick: retry, style: { marginTop: 6, cursor: "pointer" }, children: "Retry" })
              ] }) }) : pending === viewKey ? /* @__PURE__ */ jsx("div", { className: "dt-treemap-status loading", style: STATUS_STYLE, children: renderLoading ? renderLoading(node, path) : "Loading\u2026" }) : null
            ]
          }
        ),
        (() => {
          const f = renderFooter?.(node, path);
          return f ? /* @__PURE__ */ jsx("div", { className: "dt-treemap-footer", children: f }) : null;
        })(),
        tipMode === "dock" ? (() => {
          const defaultContent = renderTipDefault ? renderTipDefault(node, path) : null;
          const body = tipContent ?? defaultContent ?? /* @__PURE__ */ jsx("span", { className: "dt-treemap-tipdock-hint", children: "Hover a cell for its details." });
          const cls = tipContent ? "" : defaultContent ? " resting" : " empty";
          return /* @__PURE__ */ jsxs(
            "div",
            {
              ref: tipRef,
              className: "dt-treemap-tip dock" + (pinnedTip ? " pinned" : "") + cls,
              onMouseEnter: cancelTipClear,
              onMouseLeave: () => {
                if (!pinnedTip) {
                  pin.hover(null);
                  clearHover();
                  setTip(null);
                }
              },
              style: { position: "relative", width: "100%", pointerEvents: "auto" },
              children: [
                tipContent && pinnedTip && /* @__PURE__ */ jsx(
                  "button",
                  {
                    onClick: () => pin.clearPin(),
                    title: "Unpin (Esc)",
                    className: "dt-treemap-tip-x",
                    style: {
                      position: "absolute",
                      top: 2,
                      right: 4,
                      background: "transparent",
                      border: "none",
                      color: "inherit",
                      cursor: "pointer",
                      fontSize: "1em",
                      opacity: 0.6,
                      pointerEvents: "auto"
                    },
                    children: "\xD7"
                  }
                ),
                body
              ]
            }
          );
        })() : tipContent && tipToShow ? /* @__PURE__ */ jsxs(
          "div",
          {
            ref: tipRef,
            className: "dt-treemap-tip" + (pinnedTip ? " pinned" : ""),
            onMouseEnter: cancelTipClear,
            onMouseLeave: () => {
              if (!pinnedTip) {
                pin.hover(null);
                clearHover();
                setTip(null);
              }
            },
            style: {
              position: "fixed",
              left: Math.max(4, Math.min(tipToShow.x + 14, (typeof window !== "undefined" ? window.innerWidth : 1600) - (tipDims?.w ?? 320) - 8)),
              top: Math.max(4, Math.min(tipToShow.y + 14, (typeof window !== "undefined" ? window.innerHeight : 1200) - (tipDims?.h ?? 80) - 8)),
              background: "var(--dt-treemap-tip-bg, #1a1a1e)",
              color: "var(--dt-treemap-tip-ink, #e6e6ea)",
              border: "1px solid var(--dt-treemap-tip-border, #333)",
              borderRadius: 4,
              padding: "6px 10px",
              fontSize: "0.85rem",
              zIndex: 1e3,
              maxWidth: 320,
              // The tip anchors over a cell's top-left, so it sits on pixels you'd
              // click — to pin this cell, or (when a different cell is already
              // pinned and its tip overlaps) to pin that one. The container must
              // never eat those clicks; only its × button (below) takes pointer
              // events, so clicks fall through to the canvas/cell underneath.
              pointerEvents: "none",
              boxShadow: "0 4px 10px rgba(0,0,0,0.35)"
            },
            children: [
              pinnedTip && /* @__PURE__ */ jsx(
                "button",
                {
                  onClick: () => pin.clearPin(),
                  title: "Unpin (Esc)",
                  style: {
                    position: "absolute",
                    top: 2,
                    right: 4,
                    background: "transparent",
                    border: "none",
                    color: "inherit",
                    cursor: "pointer",
                    fontSize: "1em",
                    opacity: 0.6,
                    pointerEvents: "auto"
                  },
                  children: "\xD7"
                }
              ),
              tipContent
            ]
          }
        ) : null
      ]
    }
  );
}
function TitleSpaceToggle({ checked, onChange }) {
  return /* @__PURE__ */ jsxs("label", { className: "dt-treemap-title-space-toggle", children: [
    /* @__PURE__ */ jsx("input", { type: "checkbox", checked, onChange: (e) => onChange(e.target.checked) }),
    " reclaim title bar space"
  ] });
}

// src/filter.ts
function parseQuery(query, opts = {}) {
  const q2 = query.trim();
  if (!q2) return () => true;
  const flags = opts.caseSensitive ? "" : "i";
  const re = /^\/(.*)\/([gimsuy]*)$/.exec(q2);
  if (re) {
    try {
      const compiled = new RegExp(re[1], [...new Set(re[2] + flags)].join(""));
      return (path) => compiled.test(path);
    } catch {
    }
  }
  if (opts.caseSensitive) return (path) => path.includes(q2);
  const lower = q2.toLowerCase();
  return (path) => path.toLowerCase().includes(lower);
}
function filterNodes(nodes, getPath, query, opts) {
  const match = parseQuery(query, opts);
  return nodes.filter((n) => match(getPath(n)));
}
function dimUnmatched(style, matched, opts) {
  if (matched) return null;
  return { ...style, opacity: opts?.opacity ?? 0.15 };
}

// src/diff/colors.ts
var GREW_GREEN = "#3fb950";
var SHRANK_RED = "#f85149";
var NEUTRAL = "#8b949e";
var UNCHANGED_GREY = "rgba(110, 118, 129, 0.28)";
var TOUCHED_HATCH = "repeating-linear-gradient(45deg, rgba(255, 255, 255, 0.11) 0 3px, transparent 3px 9px)";
var deltaColor = (t) => divergingColor(-t);
var UNCHANGED_SWATCH = (() => {
  const [r, g, b, a] = parseColor(UNCHANGED_GREY) ?? [110, 118, 129, 0.28];
  const mix = (c, base2) => Math.round(c * a + base2 * (1 - a));
  return `rgb(${mix(r, CONTAINER_BG[0])}, ${mix(g, CONTAINER_BG[1])}, ${mix(b, CONTAINER_BG[2])})`;
})();
var deltaTextColor = (d) => d > 0 ? GREW_GREEN : d < 0 ? SHRANK_RED : NEUTRAL;
var statusColors = {
  added: { bg: "rgba(46, 160, 67, 0.15)" },
  removed: { bg: "rgba(248, 81, 73, 0.15)" },
  changed: { bg: "transparent" },
  touched: { bg: "transparent" },
  unchanged: { bg: "transparent" }
};

// src/diff/tree.ts
function buildDiffTree(input, areaMode, showUnchanged) {
  const uriPrefix = input.uri.replace(/\/$/, "") + "/";
  const byPath = /* @__PURE__ */ new Map();
  const pathOf = /* @__PURE__ */ new Map();
  const roots = [];
  const attach = (node, path) => {
    byPath.set(path, node);
    pathOf.set(node, path);
    const i = path.lastIndexOf("/");
    if (i < 0) {
      roots.push(node);
      return;
    }
    const parentPath = path.slice(0, i);
    let parent = byPath.get(parentPath);
    if (!parent) {
      parent = {
        key: uriPrefix + parentPath,
        label: parentPath.split("/").pop(),
        weight: 0,
        delta: 0,
        grew: 0,
        shrank: 0,
        nGrew: 0,
        nShrank: 0,
        status: "unchanged",
        oldSize: 0,
        newSize: 0,
        countDelta: 0,
        kind: "dir",
        uri: uriPrefix + parentPath,
        children: []
      };
      attach(parent, parentPath);
    }
    (parent.children ??= []).push(node);
  };
  const recRows = [
    ...input.recRows,
    ...showUnchanged ? input.unchangedTop : []
  ].sort((a, b) => a.depth - b.depth || a.path.localeCompare(b.path));
  const rest = input.unchangedRest;
  for (const r of recRows) {
    attach({
      key: r.uri,
      label: r.path.split("/").pop() || r.path,
      weight: 0,
      delta: r.delta,
      grew: 0,
      shrank: 0,
      nGrew: 0,
      nShrank: 0,
      status: r.status,
      oldSize: r.oldSize,
      newSize: r.newSize,
      countDelta: r.countDelta,
      kind: r.kind,
      uri: r.uri,
      pruned: r.pruned,
      children: void 0
    }, r.path);
  }
  for (const r of showUnchanged ? input.flatRows : []) {
    if (r.status === "unchanged" && !byPath.has(r.path)) {
      attach({
        key: r.uri,
        label: r.path,
        weight: 0,
        delta: 0,
        grew: 0,
        shrank: 0,
        nGrew: 0,
        nShrank: 0,
        status: "unchanged",
        oldSize: r.oldSize,
        newSize: r.newSize,
        countDelta: 0,
        kind: r.kind,
        uri: r.uri
      }, r.path);
    }
  }
  let maxAbs = 0;
  const finalize = (node) => {
    maxAbs = Math.max(maxAbs, Math.abs(node.delta));
    const own = areaMode === "max" ? Math.max(node.oldSize, node.newSize) : Math.abs(node.delta);
    if (!node.children?.length) {
      node.grew = Math.max(node.delta, 0);
      node.shrank = Math.min(node.delta, 0);
      node.nGrew = Math.max(node.countDelta, 0);
      node.nShrank = Math.min(node.countDelta, 0);
      node.weight = own;
      return node.weight;
    }
    let kidSum = 0;
    for (const k of node.children) {
      kidSum += finalize(k);
      node.grew += k.grew;
      node.shrank += k.shrank;
      node.nGrew += k.nGrew;
      node.nShrank += k.nShrank;
    }
    node.weight = showUnchanged ? Math.max(own, kidSum) : Math.max(kidSum, areaMode === "max" ? 0 : own);
    const gap = node.weight - kidSum;
    if (areaMode === "max" && showUnchanged && gap > Math.max(1e6, node.weight * 2e-3)) {
      const r = rest[pathOf.get(node) ?? ""];
      node.children.push({
        key: `${node.key}/__unchanged__`,
        label: "",
        weight: gap,
        delta: 0,
        grew: 0,
        shrank: 0,
        nGrew: 0,
        nShrank: 0,
        status: "filler",
        oldSize: gap,
        newSize: gap,
        countDelta: 0,
        kind: "filler",
        uri: node.uri,
        nRest: r?.count,
        nDescRest: r === void 0 ? void 0 : r.count + r.n_desc
      });
    }
    node.children.sort((a, b) => b.delta - a.delta || b.weight - a.weight);
    return node.weight;
  };
  for (const r of roots) {
    finalize(r);
  }
  const cells = roots.filter((r) => r.weight > 0);
  cells.sort(areaMode === "max" ? (a, b) => b.delta - a.delta || b.weight - a.weight : (a, b) => b.weight - a.weight);
  return { cells, maxAbsDelta: maxAbs };
}
var MIN_CELL_PX = 16;
var fetchable = (n) => n.kind === "dir" && !n.children?.length && (n.status === "changed" || n.status === "touched") && (!!n.pruned || n.delta !== 0 || n.countDelta !== 0);
function mapMinFrac(ref) {
  const box = ref.current?.getBoundingClientRect();
  if (box && box.width > 0) return MIN_CELL_PX / (box.width * box.height);
  const vw = typeof window === "undefined" ? 1200 : window.innerWidth;
  const vh = typeof window === "undefined" ? 800 : window.innerHeight;
  const w = Math.max(320, Math.min(vw, 1400) - (vw < 600 ? 16 : 48));
  const h = vw < 600 ? Math.min(0.75 * vh, 560) : 340;
  return MIN_CELL_PX / (w * h);
}
function churn(n, totalBytes, formatSize, formatCount, net = true) {
  const both = n.grew > 0 && n.shrank < 0;
  if (!both && n.delta === 0) return null;
  if (!both && !net) return null;
  let fmt;
  if (totalBytes === null) {
    fmt = (v) => formatCount(v);
  } else {
    const vals = both ? [n.grew, -n.shrank, Math.abs(n.delta)] : [Math.abs(n.delta)];
    const unitOf = (b) => formatSize(b).split(" ")[1];
    const shared = vals.every((v) => v === 0 || unitOf(v) === unitOf(totalBytes));
    fmt = (b) => shared ? formatSize(b).split(" ")[0] : formatSize(b).replace(" ", "");
  }
  const d = (n.delta > 0 ? "+" : n.delta < 0 ? "\u2212" : "\xB1") + fmt(Math.abs(n.delta));
  const parts = both ? [["\u25B2", fmt(n.grew), GREW_GREEN], ["\u25BC", fmt(-n.shrank), SHRANK_RED]] : [];
  if (net) parts.push([totalBytes === null ? "" : "\u0394", d, deltaTextColor(n.delta)]);
  return {
    text: parts.map(([g, v]) => g ? `${g} ${v}` : v).join(" "),
    node: parts.map(([g, v, color], i) => /* @__PURE__ */ jsx("span", { style: { color, marginLeft: i ? 6 : 0 }, children: g ? `${g} ${v}` : v }, g || "net"))
  };
}
function DiffTreemap({
  input,
  recState,
  onRecRetry,
  onDrill,
  cellHref,
  fetchSubtree,
  formatSize,
  formatCount,
  tiling,
  setTiling,
  mapRef,
  renderContainer,
  renderOverlay,
  renderEmpty
}) {
  const [areaMode, setAreaMode] = useState("max");
  const [showUnchanged, setShowUnchanged] = useState(true);
  const formatDelta = (bytes) => {
    const sign = bytes < 0 ? "\u2212" : "+";
    return sign + formatSize(Math.abs(bytes)).replace(" ", "");
  };
  const churnFn = (n, totalBytes, net = true) => churn(n, totalBytes, formatSize, formatCount, net);
  const loadChildren = useCallback(
    async (n) => {
      const sub = await fetchSubtree(n);
      const { cells } = buildDiffTree(
        { uri: n.uri, flatRows: [], recRows: sub.recRows, unchangedTop: sub.unchangedTop, unchangedRest: sub.unchangedRest},
        areaMode,
        showUnchanged
      );
      return cells;
    },
    [fetchSubtree, areaMode, showUnchanged]
  );
  const { root, maxAbsDelta } = useMemo(() => {
    const { cells, maxAbsDelta: maxAbs } = buildDiffTree(input, areaMode, showUnchanged);
    const totalWeight = cells.reduce((s, c) => s + c.weight, 0);
    const root2 = {
      key: input.uri,
      label: input.uri,
      weight: totalWeight,
      delta: input.totalDelta,
      grew: cells.reduce((s, c) => s + c.grew, 0),
      shrank: cells.reduce((s, c) => s + c.shrank, 0),
      nGrew: cells.reduce((s, c) => s + c.nGrew, 0),
      nShrank: cells.reduce((s, c) => s + c.nShrank, 0),
      status: "changed",
      oldSize: input.oldRootSize,
      newSize: input.newRootSize,
      countDelta: input.newRootCount - input.oldRootCount,
      kind: "dir",
      uri: input.uri,
      children: cells
    };
    return { root: root2, maxAbsDelta: maxAbs };
  }, [input, areaMode, showUnchanged]);
  if (root.children.length === 0) {
    if (renderEmpty) return /* @__PURE__ */ jsx(Fragment, { children: renderEmpty({ areaMode, showUnchanged, onShowUnchanged: () => setShowUnchanged(true) }) });
    return /* @__PURE__ */ jsxs("div", { style: { padding: 24, textAlign: "center", color: "#8b949e", fontSize: "0.875rem" }, children: [
      areaMode === "max" && showUnchanged ? "Nothing to plot \u2014 no row has any bytes on either side." : "No size deltas to plot \u2014 every row is unchanged.",
      !showUnchanged && /* @__PURE__ */ jsx("div", { style: { marginTop: 8 }, children: /* @__PURE__ */ jsx("button", { onClick: () => setShowUnchanged(true), children: "show unchanged" }) })
    ] });
  }
  const map = (
    // Taller on phones: a 340px strip of a portrait screen is unreadable.
    // (`.dt-diff-map`: height min(75vh,560px), 340px at ≥600px — see styles.css.)
    /* @__PURE__ */ jsxs("div", { ref: mapRef, className: "dt-diff-map", children: [
      /* @__PURE__ */ jsx(
        Treemap,
        {
          root,
          getSize: (n) => n.weight,
          getChildren: (n) => n.children,
          hasChildren: fetchable,
          loadChildren,
          renderLoading: (n) => `Loading \u0394 for ${n.label || "this subtree"}\u2026`,
          getLabel: (n) => n.label,
          mergeSmall: (small) => ({
            key: `${small[0].key}__fold${small.length}`,
            label: `(+${small.length})`,
            weight: small.reduce((s, c) => s + c.weight, 0),
            delta: small.reduce((s, c) => s + c.delta, 0),
            grew: small.reduce((s, c) => s + c.grew, 0),
            shrank: small.reduce((s, c) => s + c.shrank, 0),
            nGrew: small.reduce((s, c) => s + c.nGrew, 0),
            nShrank: small.reduce((s, c) => s + c.nShrank, 0),
            status: "fold",
            oldSize: small.reduce((s, c) => s + c.oldSize, 0),
            newSize: small.reduce((s, c) => s + c.newSize, 0),
            countDelta: small.reduce((s, c) => s + c.countDelta, 0),
            kind: "fold",
            uri: small[0].uri,
            nFolded: small.reduce((s, c) => s + (c.nFolded ?? 1), 0)
          }),
          tiling,
          mapStyle: {
            "--dt-treemap-edge": "rgba(255, 255, 255, 0.34)",
            "--dt-treemap-cell-border": "rgba(255, 255, 255, 0.14)"
          },
          formatSize,
          colorForCell: (n, _path, _depth, ctx) => {
            const withEdge = (s, face) => ({ ...s, edge: contrastEdge(face, ctx.fade) ?? void 0 });
            const hasKids = ctx.hasKids || !!n.children?.length;
            if (areaMode === "max") {
              if (hasKids) {
                if (n.delta === 0) return withEdge({ bg: UNCHANGED_GREY, ink: "#fff", ...n.status === "touched" && { hatch: TOUCHED_HATCH } }, UNCHANGED_GREY);
                const t2 = n.weight === 0 ? 0 : n.delta / n.weight;
                return withEdge({ bg: deltaColor(t2), ink: "#fff" }, deltaColor(t2));
              }
              const f = n.weight === 0 ? 0 : Math.min(1, Math.abs(n.delta) / n.weight);
              if (f === 0) return withEdge({ bg: UNCHANGED_GREY, ink: "#fff", ...n.status === "touched" && { hatch: TOUCHED_HATCH } }, UNCHANGED_GREY);
              const pct = `${(f * 100).toFixed(2)}%`;
              const band = deltaColor(Math.sign(n.delta));
              return withEdge({
                bg: `linear-gradient(to top, ${band} ${pct}, ${UNCHANGED_GREY} ${pct})`,
                ink: "#fff"
                // uniform with every other label
                // The stroke follows whichever half dominates the face.
              }, f > 0.5 ? band : UNCHANGED_GREY);
            }
            if (n.delta === 0) return withEdge({ bg: UNCHANGED_GREY, ink: "#fff" }, UNCHANGED_GREY);
            const t = maxAbsDelta === 0 ? 0 : n.delta / maxAbsDelta;
            return withEdge({ bg: deltaColor(t), ink: "#fff" }, deltaColor(t));
          },
          renderCellExtra: areaMode === "max" ? (n, _path, { w, h, hasKids }) => {
            const branch = hasKids || !!n.children?.length;
            if (branch || w < 56) return null;
            const f = n.weight === 0 ? 0 : Math.min(1, Math.abs(n.delta) / n.weight);
            if (f === 0) return null;
            const bandH = h * f;
            const greyH = h - bandH;
            if (greyH < 22 && h < 44) return null;
            const minBytes = Math.min(n.oldSize, n.newSize);
            const lbl = (top, height, text, style) => /* @__PURE__ */ jsx("div", { style: {
              position: "absolute",
              top,
              left: 0,
              right: 0,
              height,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              pointerEvents: "none",
              fontSize: "0.75rem",
              ...style
            }, children: text });
            return /* @__PURE__ */ jsxs(Fragment, { children: [
              bandH >= 16 && lbl(
                `${(100 - f * 100).toFixed(2)}%`,
                bandH,
                formatDelta(n.delta),
                { color: "#fff", fontWeight: 600 }
              ),
              f < 1 && greyH >= 44 && minBytes > 0 && lbl(
                "0",
                greyH,
                formatSize(minBytes),
                { color: "var(--dt-treemap-ink, #d0d0d8)", opacity: 0.65 }
              )
            ] });
          } : void 0,
          renderCellSubtitle: (n, _path, { w, hasKids }) => {
            if (!hasKids && !n.children?.length) return null;
            const room = w - 8 - n.label.length * 7 - formatSize(n.weight).length * 6.5 - 12;
            const fits = (c) => c.text.length * 7.5 + 12 <= room;
            const full = churnFn(n, n.weight);
            if (!full) return null;
            if (fits(full)) return full.node;
            const net = churnFn({ grew: 0, shrank: 0, delta: n.delta }, n.weight);
            return net && fits(net) ? net.node : null;
          },
          renderCrumbSuffix: (n) => /* @__PURE__ */ jsxs(Fragment, { children: [
            "\u2014 ",
            formatSize(n.weight),
            (() => {
              const c = churnFn(n, n.weight);
              return c && /* @__PURE__ */ jsxs(Fragment, { children: [
                " ",
                c.node
              ] });
            })()
          ] }),
          renderTooltip: (n) => /* @__PURE__ */ jsxs(Fragment, { children: [
            /* @__PURE__ */ jsx("div", { style: { fontWeight: 500 }, children: n.status === "fold" ? `${(n.nFolded ?? 0).toLocaleString()} smaller items` : n.status === "filler" ? n.nRest === void 0 ? "unchanged" : `${n.nRest.toLocaleString()} unchanged children` : n.label || "unchanged" }),
            /* @__PURE__ */ jsxs("div", { style: { color: "rgba(255, 255, 255, 0.75)", fontSize: "0.85em" }, children: [
              n.delta === 0 ? formatSize(n.newSize) : /* @__PURE__ */ jsxs(Fragment, { children: [
                formatSize(n.oldSize),
                " \u2192 ",
                formatSize(n.newSize),
                " (",
                /* @__PURE__ */ jsx("span", { style: { color: deltaTextColor(n.delta), fontWeight: 600, fontSize: "1.1em" }, children: formatDelta(n.delta) }),
                ")"
              ] }),
              n.status === "unchanged" && " \u2014 unchanged",
              n.status === "touched" && " \u2014 touched (same bytes & count, mtime moved)",
              n.status === "filler" && n.nDescRest !== void 0 && n.nDescRest !== n.nRest && /* @__PURE__ */ jsxs(Fragment, { children: [
                " \xB7 ",
                formatCount(n.nDescRest),
                " entries below"
              ] })
            ] }),
            n.grew > 0 && n.shrank < 0 && /* @__PURE__ */ jsx("div", { style: { fontSize: "0.8em" }, children: churnFn(n, n.newSize, false)?.node }),
            (n.countDelta !== 0 || n.nGrew > 0) && /* @__PURE__ */ jsxs("div", { style: { fontSize: "0.8em" }, children: [
              /* @__PURE__ */ jsx("span", { style: { opacity: 0.6 }, children: "count " }),
              churnFn({ grew: n.nGrew, shrank: n.nShrank, delta: n.countDelta }, null)?.node
            ] }),
            (n.status === "filler" || n.status === "fold" || n.status === "added" || n.status === "removed" || n.pruned && input.index?.status !== "done") && /* @__PURE__ */ jsxs("div", { style: { opacity: 0.5, fontSize: "0.75em", marginTop: 2 }, children: [
              n.status === "filler" ? "unchanged bytes the diff never needed to enumerate" : n.status === "fold" ? "children too small to draw, aggregated" : n.status === "added" || n.status === "removed" ? n.status : null,
              n.pruned && input.index?.status !== "done" && /* @__PURE__ */ jsxs(Fragment, { children: [
                (n.status === "added" || n.status === "removed") && " \xB7 ",
                n.delta === 0 && n.countDelta === 0 ? "not descended (walk budget): something inside moved \u2014 click to compare here" : "\u0394 not localized (walk budget) \u2014 click to compare here"
              ] })
            ] })
          ] }),
          renderLegend: () => /* @__PURE__ */ jsxs("span", { style: { display: "inline-flex", alignItems: "center", gap: 6, fontSize: "0.8rem", opacity: 0.85 }, children: [
            /* @__PURE__ */ jsxs(
              "span",
              {
                title: "Cell color = \u0394 as a share of max(old, new): full color when the change is the whole cell, grey when it's a sliver. Leaves paint that share as a band from the bottom; branches tint their title strip by the net share.",
                style: { display: "inline-flex", alignItems: "center", gap: 4 },
                children: [
                  /* @__PURE__ */ jsx("span", { style: { opacity: 0.6 }, children: "\u2212100%" }),
                  /* @__PURE__ */ jsx("span", { style: {
                    display: "inline-block",
                    width: 84,
                    height: 10,
                    borderRadius: 2,
                    background: `linear-gradient(to right, ${deltaColor(-1)}, ${deltaColor(-0.4)}, ${UNCHANGED_SWATCH}, ${deltaColor(0.4)}, ${deltaColor(1)})`
                  } }),
                  /* @__PURE__ */ jsx("span", { style: { opacity: 0.6 }, children: "+100%" })
                ]
              }
            ),
            areaMode === "max" && // Legend-item toggle (plot-legend style): click to hide/show
            // unchanged rows. Hidden = only changed entries plot (at
            // max(old, new) size); parents shrink to their changed
            // contents, fillers and grey context rows drop out.
            /* @__PURE__ */ jsxs(
              "button",
              {
                onClick: (e) => {
                  e.stopPropagation();
                  setShowUnchanged((s) => !s);
                },
                title: showUnchanged ? "Hide unchanged rows: only changed entries plot; parents shrink to their changed contents" : "Show unchanged rows: grey context cells and fillers restore true directory proportions",
                style: {
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  cursor: "pointer",
                  background: "none",
                  border: "none",
                  padding: 0,
                  margin: 0,
                  font: "inherit",
                  color: "inherit",
                  opacity: showUnchanged ? 1 : 0.45,
                  textDecoration: showUnchanged ? "none" : "line-through"
                },
                children: [
                  /* @__PURE__ */ jsx("span", { style: { display: "inline-block", width: 12, height: 12, background: UNCHANGED_SWATCH, border: "1px solid rgba(255,255,255,0.25)", borderRadius: 2, boxSizing: "border-box" } }),
                  "unchanged"
                ]
              }
            ),
            areaMode === "max" && /* @__PURE__ */ jsxs("span", { title: "same bytes & count, mtime moved (rename / net-zero churn / touch)", style: { display: "inline-flex", alignItems: "center", gap: 6 }, children: [
              /* @__PURE__ */ jsx("span", { style: { display: "inline-block", width: 12, height: 12, background: UNCHANGED_SWATCH, backgroundImage: TOUCHED_HATCH, border: "1px solid rgba(255,255,255,0.25)", borderRadius: 2, boxSizing: "border-box" } }),
              "touched"
            ] }),
            input.index && input.index.status !== "done" && /* @__PURE__ */ jsx(
              "span",
              {
                title: input.index.status === "failed" ? `full diff index failed: ${input.index.error ?? "unknown error"} \u2014 showing the budgeted walk` : "showing a budgeted walk while the full diff index builds; the map completes itself when it lands",
                style: { opacity: 0.6, marginLeft: 6, fontStyle: "italic" },
                children: input.index.status === "failed" ? "index failed" : "indexing\u2026"
              }
            ),
            /* @__PURE__ */ jsx("span", { style: { display: "inline-flex", gap: 2, marginLeft: 6 }, children: ["max", "delta"].map((m) => /* @__PURE__ */ jsx(
              "button",
              {
                onClick: (e) => {
                  e.stopPropagation();
                  setAreaMode(m);
                },
                title: m === "max" ? "Area = max(old, new), with |\u0394| painted as a band: deleted subtrees keep their old area; stable structure stays visible" : "Area = |\u0394bytes|: churn only, unchanged rows dropped",
                style: {
                  cursor: "pointer",
                  fontSize: "0.75rem",
                  padding: "1px 7px",
                  borderRadius: 3,
                  border: "1px solid var(--dt-border, #444)",
                  background: areaMode === m ? "var(--dt-accent-bg, #30363d)" : "transparent",
                  color: "inherit",
                  fontWeight: areaMode === m ? 600 : 400
                },
                children: m === "max" ? "max" : "\u0394"
              },
              m
            )) }),
            /* @__PURE__ */ jsx("span", { style: { display: "inline-flex", gap: 2, marginLeft: 4 }, children: ["gaps", "shared"].map((t) => /* @__PURE__ */ jsx(
              "button",
              {
                onClick: (e) => {
                  e.stopPropagation();
                  setTiling(t);
                },
                title: t === "gaps" ? "2px gutters and rounded corners; dense leaf fields under-paint by ~perimeter/area" : "Cells abut, one stroke per boundary \u2014 areas exact (a 6\xD76px cell with 2px gutters paints only 4\xD74)",
                style: {
                  cursor: "pointer",
                  fontSize: "0.75rem",
                  padding: "1px 7px",
                  borderRadius: 3,
                  border: "1px solid var(--dt-border, #444)",
                  background: tiling === t ? "var(--dt-accent-bg, #30363d)" : "transparent",
                  color: "inherit",
                  fontWeight: tiling === t ? 600 : 400
                },
                children: t
              },
              t
            )) })
          ] }),
          onCellClick: (n) => {
            if (n.kind !== "dir" || n.status === "filler" || n.status === "fold") return false;
            onDrill(n.uri);
            return true;
          },
          cellHref: (n) => n.kind === "dir" && n.status !== "filler" && n.status !== "fold" && n.uri ? cellHref?.(n.uri) : void 0
        }
      ),
      recState !== "ready" && (renderOverlay ? renderOverlay(recState, onRecRetry) : /* @__PURE__ */ jsx(
        "div",
        {
          style: {
            position: "absolute",
            inset: 0,
            zIndex: 2,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 12,
            background: "rgba(8, 10, 12, 0.55)",
            pointerEvents: recState === "error" ? "auto" : "none"
          },
          children: recState === "loading" ? /* @__PURE__ */ jsx("span", { style: { opacity: 0.9 }, children: "computing \u0394 detail\u2026" }) : /* @__PURE__ */ jsx("button", { onClick: onRecRetry, children: "\u0394 detail failed \u2014 retry" })
        }
      ))
    ] })
  );
  return /* @__PURE__ */ jsx(Fragment, { children: renderContainer ? renderContainer(map) : map });
}
function SortIcon({ dir }) {
  return /* @__PURE__ */ jsx("svg", { width: 10, height: 10, viewBox: "0 0 320 512", fill: "currentColor", style: { verticalAlign: "middle" }, "aria-hidden": true, children: dir === "desc" ? /* @__PURE__ */ jsx("path", { d: "M41 288h238c21.4 0 32.1 25.9 17 41L177 448c-9.4 9.4-24.6 9.4-34 0L24 329c-15.1-15.1-4.4-41 17-41z" }) : /* @__PURE__ */ jsx("path", { d: "M279 224H41c-21.4 0-32.1-25.9-17-41L143 64c9.4-9.4 24.6-9.4 34 0l119 119c15.1 15.1 4.4 41-17 41z" }) });
}
function DeltaBar({ delta, maxDelta }) {
  if (maxDelta === 0) return /* @__PURE__ */ jsx("div", { style: { width: "50px" } });
  const pct = Math.min(Math.abs(delta) / maxDelta * 100, 100);
  const color = delta === 0 ? "transparent" : deltaTextColor(delta);
  return /* @__PURE__ */ jsx("div", { style: {
    width: "50px",
    height: "8px",
    backgroundColor: "rgba(255,255,255,0.1)",
    borderRadius: "4px",
    overflow: "hidden",
    flexShrink: 0
  }, children: /* @__PURE__ */ jsx("div", { style: {
    width: `${pct}%`,
    height: "100%",
    backgroundColor: color,
    borderRadius: "4px"
  } }) });
}
function SortHeader({
  label,
  column,
  sortColumn,
  sortDirection,
  onSort,
  style
}) {
  const isActive = sortColumn === column;
  return /* @__PURE__ */ jsx(
    "th",
    {
      onClick: () => onSort(column),
      style: {
        ...style,
        cursor: "pointer",
        userSelect: "none",
        fontWeight: "normal",
        color: isActive ? "#e6edf3" : "#8b949e"
      },
      children: /* @__PURE__ */ jsxs("span", { style: { display: "inline-flex", alignItems: "center", gap: "2px" }, children: [
        label,
        isActive && /* @__PURE__ */ jsx(SortIcon, { dir: sortDirection })
      ] })
    }
  );
}
var td = { padding: "8px 6px", textAlign: "right", fontFamily: "monospace", fontSize: "0.85em", whiteSpace: "nowrap" };
var dim = { color: "#8b949e" };
function metricCells(metric, vals, status, kind, maxDelta) {
  const blank = metric.dirOnly && kind !== "dir";
  const before = blank ? "-" : status === "added" ? "-" : metric.fmt(vals.old);
  const after = blank ? "-" : status === "removed" ? "-" : metric.fmt(vals.new);
  const beforeColor = status === "removed" ? SHRANK_RED : "#8b949e";
  const afterColor = status === "added" ? GREW_GREEN : void 0;
  return /* @__PURE__ */ jsxs(Fragment, { children: [
    /* @__PURE__ */ jsx("td", { style: { ...td, color: beforeColor, borderLeft: "1px solid rgba(255,255,255,0.1)", paddingLeft: "12px" }, children: before }),
    /* @__PURE__ */ jsx("td", { style: { ...td, color: afterColor }, children: after }),
    /* @__PURE__ */ jsx("td", { style: { ...td, color: deltaTextColor(vals.delta), fontWeight: vals.delta !== 0 ? "bold" : void 0 }, children: blank ? "-" : metric.fmtDelta(vals.delta) }),
    /* @__PURE__ */ jsx("td", { style: { padding: metric.dirOnly ? "8px 4px" : "8px 12px 8px 4px" }, children: blank ? null : /* @__PURE__ */ jsx(DeltaBar, { delta: vals.delta, maxDelta }) })
  ] });
}
function ParentSummaryRow({
  parent,
  metrics,
  maxDelta,
  renderIcon,
  rowAction
}) {
  const { uri } = parent;
  const dirName = uri === "/" ? "/" : uri.split("/").pop() || uri;
  return /* @__PURE__ */ jsxs("tr", { style: { backgroundColor: "rgba(88, 166, 255, 0.1)", borderBottom: "2px solid rgba(255,255,255,0.2)" }, children: [
    /* @__PURE__ */ jsx("td", { style: { padding: "8px", fontWeight: "bold" }, children: /* @__PURE__ */ jsxs("div", { style: { display: "flex", alignItems: "center", gap: "8px" }, children: [
      renderIcon?.({ kind: "dir" }),
      /* @__PURE__ */ jsxs("span", { style: { fontFamily: "monospace", fontSize: "0.9em" }, children: [
        ". (",
        dirName,
        ")"
      ] })
    ] }) }),
    metrics.map((m) => {
      const v = parent.values[m.id] ?? { old: null, new: null, delta: 0 };
      return /* @__PURE__ */ jsxs(MetricGroup, { children: [
        /* @__PURE__ */ jsx("td", { style: { ...td, ...dim, borderLeft: "1px solid rgba(255,255,255,0.1)", paddingLeft: "12px" }, children: m.fmt(v.old) }),
        /* @__PURE__ */ jsx("td", { style: td, children: m.fmt(v.new) }),
        /* @__PURE__ */ jsx("td", { style: { ...td, color: deltaTextColor(v.delta), fontWeight: v.delta !== 0 ? "bold" : void 0 }, children: m.fmtDelta(v.delta) }),
        /* @__PURE__ */ jsx("td", { style: { padding: m.dirOnly ? "8px 4px" : "8px 12px 8px 4px" }, children: /* @__PURE__ */ jsx(DeltaBar, { delta: v.delta, maxDelta: Math.max(maxDelta[m.id], Math.abs(v.delta)) }) })
      ] }, m.id);
    }),
    /* @__PURE__ */ jsx("td", { style: { padding: "4px 8px", textAlign: "center", borderLeft: "1px solid rgba(255,255,255,0.1)" }, children: rowAction?.({ uri, kind: "dir", path: uri }) })
  ] });
}
function MetricGroup({ children }) {
  return /* @__PURE__ */ jsx(Fragment, { children });
}
function DiffRow({
  row,
  metrics,
  maxDelta,
  renderIcon,
  renderPathLink,
  rowAction
}) {
  const { bg } = statusColors[row.status];
  const label = /* @__PURE__ */ jsx("span", { style: { fontFamily: "monospace", fontSize: "0.9em" }, children: row.path });
  const pathEl = row.kind === "dir" && renderPathLink ? renderPathLink(row, row.path) : label;
  return /* @__PURE__ */ jsxs("tr", { style: { backgroundColor: bg, borderBottom: "1px solid rgba(255,255,255,0.05)" }, children: [
    /* @__PURE__ */ jsx("td", { style: { padding: "8px" }, children: /* @__PURE__ */ jsxs("div", { style: { display: "flex", alignItems: "center", gap: "8px" }, children: [
      renderIcon?.({ kind: row.kind }),
      pathEl,
      row.status === "touched" && /* @__PURE__ */ jsx(
        "span",
        {
          title: "same bytes & count, mtime moved (rename / net-zero churn / touch)",
          style: {
            fontSize: "0.7em",
            color: "#8b949e",
            padding: "0 6px",
            borderRadius: 3,
            background: UNCHANGED_GREY,
            backgroundImage: TOUCHED_HATCH
          },
          children: "touched"
        }
      )
    ] }) }),
    metrics.map((m) => /* @__PURE__ */ jsx(MetricGroup, { children: metricCells(m, row.values[m.id] ?? { old: null, new: null, delta: 0 }, row.status, row.kind, maxDelta[m.id]) }, m.id)),
    /* @__PURE__ */ jsx("td", { style: { padding: "4px 8px", textAlign: "center", borderLeft: "1px solid rgba(255,255,255,0.1)" }, children: rowAction?.({ uri: row.uri, kind: row.kind, path: row.path }) })
  ] });
}
function DiffTable({
  rows,
  parent,
  metrics,
  unchangedCount,
  pageSize = 50,
  renderIcon,
  renderPathLink,
  rowAction
}) {
  const [sortColumn, setSortColumn] = useState(`${metrics[0].id}_delta`);
  const [sortDirection, setSortDirection] = useState("desc");
  const [page, setPage] = useState(0);
  useEffect(() => {
    setPage(0);
  }, [rows, sortColumn, sortDirection]);
  const handleSort = (col) => {
    if (sortColumn === col) {
      setSortDirection((d) => d === "asc" ? "desc" : "asc");
    } else {
      setSortColumn(col);
      setSortDirection("desc");
    }
  };
  const changedRows = rows.filter((r) => r.status !== "unchanged");
  const sortedRows = useMemo(() => {
    if (!sortColumn) return changedRows;
    const i = sortColumn.lastIndexOf("_");
    const mid = sortColumn.slice(0, i);
    const kind = sortColumn.slice(i + 1);
    const valOf = (r) => {
      const v = r.values[mid] ?? { old: null, new: null, delta: 0 };
      return kind === "delta" ? Math.abs(v.delta) : kind === "old" ? v.old ?? 0 : v.new ?? 0;
    };
    return [...changedRows].sort((a, b) => {
      const aVal = valOf(a), bVal = valOf(b);
      return sortDirection === "desc" ? bVal - aVal : aVal - bVal;
    });
  }, [changedRows, sortColumn, sortDirection]);
  const maxDelta = {};
  for (const m of metrics) {
    maxDelta[m.id] = Math.max(...changedRows.map((r) => Math.abs(r.values[m.id]?.delta ?? 0)), 1);
  }
  const subTh = { padding: "4px 6px", textAlign: "right", fontSize: "0.75em", whiteSpace: "nowrap" };
  const cols = 1 + metrics.length * 4 + 1;
  return /* @__PURE__ */ jsxs("table", { style: { width: "100%", borderCollapse: "collapse" }, children: [
    /* @__PURE__ */ jsxs("thead", { style: { position: "sticky", top: 0, zIndex: 2, background: "#0d1117" }, children: [
      /* @__PURE__ */ jsxs("tr", { style: { borderBottom: "1px solid rgba(255,255,255,0.1)" }, children: [
        /* @__PURE__ */ jsx("th", { style: { textAlign: "left", padding: "8px", width: "100%" }, children: "Path" }),
        metrics.map((m) => /* @__PURE__ */ jsx("th", { colSpan: 4, style: { textAlign: "center", padding: "8px 12px", borderLeft: "1px solid rgba(255,255,255,0.1)", whiteSpace: "nowrap" }, children: m.label }, m.id)),
        /* @__PURE__ */ jsx("th", { style: { padding: "8px", borderLeft: "1px solid rgba(255,255,255,0.1)", whiteSpace: "nowrap" } })
      ] }),
      /* @__PURE__ */ jsxs("tr", { style: { borderBottom: "1px solid rgba(255,255,255,0.1)" }, children: [
        /* @__PURE__ */ jsx("th", { style: { width: "100%" } }),
        metrics.map((m) => /* @__PURE__ */ jsxs(MetricGroup, { children: [
          /* @__PURE__ */ jsx(SortHeader, { label: "old", column: `${m.id}_old`, sortColumn, sortDirection, onSort: handleSort, style: { ...subTh, borderLeft: "1px solid rgba(255,255,255,0.1)", paddingLeft: "12px" } }),
          /* @__PURE__ */ jsx(SortHeader, { label: "new", column: `${m.id}_new`, sortColumn, sortDirection, onSort: handleSort, style: subTh }),
          /* @__PURE__ */ jsx(SortHeader, { label: "\u0394", column: `${m.id}_delta`, sortColumn, sortDirection, onSort: handleSort, style: subTh }),
          /* @__PURE__ */ jsx("th", { style: subTh })
        ] }, m.id)),
        /* @__PURE__ */ jsx("th", { style: { whiteSpace: "nowrap" } })
      ] })
    ] }),
    /* @__PURE__ */ jsxs("tbody", { children: [
      /* @__PURE__ */ jsx(
        ParentSummaryRow,
        {
          parent,
          metrics,
          maxDelta,
          renderIcon,
          rowAction
        }
      ),
      sortedRows.slice(page * pageSize, (page + 1) * pageSize).map((row) => /* @__PURE__ */ jsx(
        DiffRow,
        {
          row,
          metrics,
          maxDelta,
          renderIcon,
          renderPathLink,
          rowAction
        },
        row.key
      )),
      sortedRows.length === 0 && /* @__PURE__ */ jsx("tr", { children: /* @__PURE__ */ jsx("td", { colSpan: cols, style: { padding: "24px", textAlign: "center", color: "#8b949e" }, children: "No changes detected between scans" }) }),
      (sortedRows.length > pageSize || unchangedCount > 0) && /* @__PURE__ */ jsx("tr", { children: /* @__PURE__ */ jsxs("td", { colSpan: cols, style: { padding: "6px 8px", color: "#8b949e", fontSize: "0.8em" }, children: [
        sortedRows.length > pageSize && /* @__PURE__ */ jsxs("span", { style: { display: "inline-flex", alignItems: "center", gap: 8, marginRight: 16 }, children: [
          page * pageSize + 1,
          "\u2013",
          Math.min((page + 1) * pageSize, sortedRows.length),
          " of ",
          sortedRows.length.toLocaleString(),
          ["\xAB", "\u2039", "\u203A", "\xBB"].map((g, i) => {
            const last = Math.ceil(sortedRows.length / pageSize) - 1;
            const to = [0, page - 1, page + 1, last][i];
            const off = to < 0 || to > last || to === page;
            return /* @__PURE__ */ jsx(
              "button",
              {
                disabled: off,
                onClick: () => setPage(to),
                style: { background: "none", border: "none", color: off ? "#555" : "#54aeff", cursor: off ? "default" : "pointer", fontSize: "1em", padding: "0 2px" },
                children: g
              },
              g
            );
          })
        ] }),
        unchangedCount > 0 && /* @__PURE__ */ jsxs(Fragment, { children: [
          unchangedCount.toLocaleString(),
          " unchanged ",
          unchangedCount === 1 ? "entry" : "entries",
          " not listed"
        ] })
      ] }) })
    ] })
  ] });
}

export { DiffTable, DiffTreemap, DustHatch, GREW_GREEN, MIN_CELL_PX, NEUTRAL, OutlineOverlay, SHRANK_RED, TOUCHED_HATCH, TitleSpaceToggle, Treemap, TreemapCanvas, UNCHANGED_GREY, UNCHANGED_SWATCH, buildDiffTree, canvasToPngBlob, churn, colorResolver, composeExport, copyPng, cssColor, defaultExportFilename, deltaColor, deltaTextColor, dimUnmatched, downloadPng, drawDust, dustLineCount, dustOffsets, fetchable, filterNodes, groupOutlines, groupRects, mapMinFrac, parseQuery, statusColors, unionOutline };
//# sourceMappingURL=index.js.map
//# sourceMappingURL=index.js.map