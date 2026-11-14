import { CONTAINER_BG, hierColor, contrastEdge } from './chunk-FUKGMHLM.js';
import { parseColor } from './chunk-JJOWQXW4.js';

// src/cellText.ts
var NAME_MIN_PX = 48;
var candidates = (t) => typeof t === "string" ? [t] : t;
function fitCandidate(cs, room, measure) {
  for (const c of cs) if (measure(c) <= room) return c;
  return cs[cs.length - 1] ?? "";
}
var inlineRoom = (boxW, pad, gap, nameW) => boxW - 2 * pad - gap - Math.min(nameW, NAME_MIN_PX);

// src/dust.ts
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
function dustLineCount(len, count, base) {
  const byCount = Math.round(base * Math.log2(count + 1));
  return Math.max(2, Math.min(byCount, Math.floor(len / 4)));
}
function dustLines(w, h, count, baseLines = 3, ratio = 1.32) {
  if (w <= 0 || h <= 0) return { xs: [], ys: [] };
  return {
    xs: dustOffsets(w, dustLineCount(w, count, baseLines), ratio).map((d) => Math.round(w - d) + 0.5),
    ys: dustOffsets(h, dustLineCount(h, count, baseLines), ratio).map((d) => Math.round(h - d) + 0.5)
  };
}
function dustRules(w, h, count, notch, baseLines = 3, ratio = 1.32) {
  const { xs, ys } = dustLines(w, h, count, baseLines, ratio);
  const nw = notch ? Math.max(0, notch.w) : 0;
  const nh = notch ? Math.max(0, notch.h) : 0;
  return {
    v: xs.map((x) => [x, x < nw ? Math.ceil(nh) : 0]).filter(([, y0]) => y0 < h),
    h: ys.map((y) => [y, y < nh ? Math.ceil(nw) : 0]).filter(([, x0]) => x0 < w)
  };
}
var HATCH_DARK = "rgba(0, 0, 0, 0.6)";
var HATCH_LIGHT = "rgba(255, 255, 255, 0.6)";
var DARK_A = 0.6;
var LIGHT_A = 0.6;
var lum = (c) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
function hatchInk(fill, alpha, base = CONTAINER_BG) {
  const a = fill ? Math.max(0, Math.min(1, fill[3] * alpha)) : 0;
  const ground = (fill ? lum(fill) * a : 0) + lum(base) * (1 - a);
  return ground * DARK_A >= (255 - ground) * LIGHT_A ? HATCH_DARK : HATCH_LIGHT;
}
function dustGround(style) {
  const largest = (xs) => xs?.reduce((a, b) => !a || b.frac > a.frac ? b : a, void 0)?.color;
  return largest(style.bands) ?? (style.segments && style.segments.length > 1 ? largest(style.segments) : void 0) ?? style.bg;
}
function dustInk(style, alpha = 1, base = CONTAINER_BG, parse = parseColor) {
  const ground = dustGround(style);
  return hatchInk(ground ? parse(ground) : null, alpha, base);
}

// src/layout.ts
function isFolded(n) {
  return typeof n === "object" && n !== null && n.__folded === true;
}
function edgeEmphFactor(depth, emphasis) {
  return 1 + emphasis * Math.max(0, 2 - depth);
}
function cellStroke(cfg, depth, w, h, dust, parentBw) {
  const bw = Math.min(cfg.borderWidth(depth, { w, h }) * edgeEmphFactor(depth, cfg.edgeEmphasis), dust ? 1 : Infinity);
  return cfg.borderStep ? Math.max(1, Math.min(Math.round(bw), parentBw - 1)) : bw;
}
function reclaimTitleColumns(rects, titleRight, bodyHeight, lift) {
  if (bodyHeight <= 0 || lift <= 0 || ![titleRight, bodyHeight, lift].every(Number.isFinite)) return rects;
  const epsilon = 1e-6;
  let boundary;
  let rightmost = -Infinity;
  for (const r of [...rects].sort((a, b) => a.x - b.x)) {
    if (r.x >= titleRight && rightmost <= r.x + epsilon) {
      boundary = r.x;
      break;
    }
    rightmost = Math.max(rightmost, r.x + r.w);
  }
  if (boundary === void 0) return rects;
  const width = rects.reduce((max, r) => Math.max(max, r.x + r.w), 0);
  const raisedHeight = bodyHeight + lift;
  const areaScale = width * raisedHeight / (width * bodyHeight + boundary * lift);
  const rightScale = areaScale * bodyHeight / raisedHeight;
  const newBoundary = boundary * areaScale;
  const heightScale = raisedHeight / bodyHeight;
  return rects.map((r) => r.x >= boundary - epsilon ? { ...r, x: newBoundary + (r.x - boundary) * rightScale, w: r.w * rightScale, y: r.y * heightScale - lift, h: r.h * heightScale } : { ...r, x: r.x * areaScale, w: r.w * areaScale });
}
function layoutChildren(node, path, chainLabels, raw, dims, cfg) {
  const { w, h, boxW, boxH, edge, showLbl, depth } = dims;
  let kw = boxW - 4;
  let kh = boxH - (showLbl ? 21 : 4);
  let mode = "gaps";
  let rects = [];
  let titleWidth = null;
  if (raw?.length && w > 90 && h > 44) {
    const lay = (width, height) => cfg.layTiles(cfg.fold(raw.slice(), width, height), 0, 0, width, height);
    rects = lay(kw, kh);
    mode = cfg.tilingFor(node, path, depth + 1, w, h, rects);
    if (mode === "shared") {
      kw = boxW - 2 * edge;
      kh = boxH - (showLbl ? 20 + edge : 2 * edge);
      rects = lay(kw, kh);
    }
    if (showLbl && cfg.getTitleWidth) {
      const width = cfg.getTitleWidth(node, chainLabels, w, boxW);
      const left = mode === "shared" ? edge : 3;
      const top = mode === "shared" ? edge : 3;
      const raised = reclaimTitleColumns(rects, width - left, kh, 20 - top);
      if (raised !== rects) titleWidth = width;
      rects = raised;
    }
  }
  return { rects, mode, titleWidth };
}
function place(node0, path0, r, depth, mode, cfg, parentBw) {
  const { getLabel, childrenOf, showLabels, collapseChains } = cfg;
  const folded = isFolded(node0);
  let node = node0;
  let path = path0;
  let chainLabels = null;
  if (collapseChains && !folded) {
    let cur = node0;
    let p = path0;
    const labels = [getLabel(cur)];
    for (; ; ) {
      const only = childrenOf(cur, p);
      if (!only || only.length !== 1) break;
      cur = only[0];
      p = [...p, cur];
      labels.push(getLabel(cur));
    }
    if (labels.length > 1) {
      node = cur;
      path = p;
      chainLabels = labels;
    }
  }
  const dust = Math.min(r.w, r.h) < 14;
  const shared = mode === "shared";
  const showLbl = showLabels && r.w > 36 && r.h > 13;
  const bw = shared ? cellStroke(cfg, depth, r.w, r.h, dust, parentBw) : 0;
  const edge = bw / 2;
  const boxW = shared ? r.w : r.w - (dust ? 1 : 2);
  const boxH = shared ? r.h : r.h - (dust ? 1 : 2);
  const kidChildren = folded ? void 0 : childrenOf(node, path);
  const { rects: kids, mode: kidsMode, titleWidth } = layoutChildren(
    node,
    path,
    chainLabels,
    kidChildren,
    { w: r.w, h: r.h, boxW, boxH, edge, showLbl, depth },
    cfg
  );
  const innerLeft = kidsMode === "shared" ? edge : 3;
  const innerTop = kidsMode === "shared" ? showLbl ? 20 : edge : showLbl ? 20 : 3;
  const children = kids.filter((s) => s.w >= 3 && s.h >= 3).map(
    (s) => place(
      s.it,
      isFolded(s.it) ? path : [...path, s.it],
      { x: r.x + innerLeft + s.x, y: r.y + innerTop + s.y, w: s.w, h: s.h },
      depth + 1,
      kidsMode,
      cfg,
      shared ? bw : Infinity
    )
  );
  return {
    node,
    path,
    folded,
    chainLabels,
    x: r.x,
    y: r.y,
    w: r.w,
    h: r.h,
    depth,
    mode,
    edge,
    dust,
    boxW,
    boxH,
    showLbl,
    hasKids: children.length > 0,
    titleWidth,
    children
  };
}
function layoutCells(topRects, basePath, rootMode, cfg) {
  return topRects.filter((r) => r.w >= 3 && r.h >= 3).map(
    (r) => place(
      r.it,
      isFolded(r.it) ? basePath : [...basePath, r.it],
      r,
      0,
      rootMode,
      cfg,
      Infinity
    )
  );
}
function flattenPlaced(cells, out = []) {
  for (const c of cells) {
    out.push(c);
    if (c.children.length) flattenPlaced(c.children, out);
  }
  return out;
}
function hitTest(cells, px, py) {
  let found = null;
  for (const c of cells) {
    if (px >= c.x && px < c.x + c.w && py >= c.y && py < c.y + c.h) {
      found = c;
      const deeper = hitTest(c.children, px, py);
      if (deeper) found = deeper;
    }
  }
  return found;
}

// src/cellStyle.ts
function categoricalStyle(path, hasKids, getLabel, topLevelSlot, defaultSlots, nested) {
  const top = path[1];
  const base = (top ? topLevelSlot.get(getLabel(top)) : void 0) ?? defaultSlots[0];
  if (!nested) {
    return hasKids ? { bg: "var(--dt-treemap-container-bg, #202024)", ink: "var(--dt-treemap-ink, #d0d0d8)" } : { bg: base, ink: "#fff" };
  }
  const l2Key = path.length >= 3 ? getLabel(path[2]) : null;
  const bg = hierColor(base, l2Key, hasKids);
  return hasKids ? { bg, ink: "var(--dt-treemap-ink, #d0d0d8)" } : { bg, ink: "#fff" };
}
var DEFAULT_RING_WIDTH = 2;
function resolveRing(ring) {
  if (!ring) return null;
  if (typeof ring === "string") return { color: ring, width: DEFAULT_RING_WIDTH, inset: true };
  if (!ring.color) return null;
  return { color: ring.color, width: ring.width ?? DEFAULT_RING_WIDTH, inset: ring.inset ?? true };
}
function resolveCellStyle(cell, o) {
  const { folded, node, path, depth, w, h, hasKids, mode } = cell;
  const fade = o.fadeAt(depth);
  const ctx = { w, h, hasKids, fade };
  const explicit = folded ? null : o.colorForCell?.(node, path, depth, ctx);
  let style;
  if (explicit) {
    style = explicit;
  } else if (folded) {
    style = o.dustTexture ? { bg: "var(--dt-treemap-folded-ground, rgba(120, 120, 135, 0.12))", ink: "var(--dt-treemap-folded-ink, #d0d0d8)" } : { bg: "var(--dt-treemap-folded, #4a4a52)", ink: "var(--dt-treemap-folded-ink, #d0d0d8)" };
  } else {
    style = categoricalStyle(path, hasKids, o.getLabel, o.topLevelSlot, o.defaultSlots, o.nestedHues ?? false);
  }
  if (o.lens && !folded) {
    style = o.lens(node, path, depth, ctx, style) ?? style;
  }
  let builtinEdge = null;
  if (mode === "shared" && o.edgeContrast && !style.edge) {
    builtinEdge = contrastEdge(style.bg, fade);
  }
  return { style, builtinEdge };
}
function foldedOf(cell) {
  return cell.folded ? cell.node : null;
}

export { HATCH_DARK, HATCH_LIGHT, NAME_MIN_PX, candidates, categoricalStyle, cellStroke, dustGround, dustInk, dustLineCount, dustLines, dustOffsets, dustRules, fitCandidate, flattenPlaced, foldedOf, hatchInk, hitTest, inlineRoom, isFolded, layoutCells, layoutChildren, resolveCellStyle, resolveRing };
//# sourceMappingURL=chunk-OHLUHXRA.js.map
//# sourceMappingURL=chunk-OHLUHXRA.js.map