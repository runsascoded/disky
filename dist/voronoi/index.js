import { useHoverPin } from '../chunk-HMG5G7YN.js';
import { DEFAULT_PALETTE } from '../chunk-FUKGMHLM.js';
import '../chunk-JJOWQXW4.js';
import '../chunk-2DHCBMZJ.js';
import { useRef, useState, useLayoutEffect, useMemo } from 'react';
import { hierarchy } from 'd3-hierarchy';
import { voronoiTreemap } from 'd3-voronoi-treemap';
import { jsxs, jsx } from 'react/jsx-runtime';

// src/voronoi/geometry.ts
function circlePolygon(cx, cy, r, sides = 64) {
  return Array.from({ length: sides }, (_, i) => {
    const t = 2 * Math.PI * i / sides;
    return [cx + r * Math.cos(t), cy + r * Math.sin(t)];
  });
}
function rectPolygon(x, y, w, h) {
  return [
    [x, y],
    [x + w, y],
    [x + w, y + h],
    [x, y + h]
  ];
}
function polygonArea(poly) {
  let a = 0;
  for (let i = 0, n = poly.length; i < n; i++) {
    const [x0, y0] = poly[i];
    const [x1, y1] = poly[(i + 1) % n];
    a += x0 * y1 - x1 * y0;
  }
  return Math.abs(a) / 2;
}
function polygonCentroid(poly) {
  let a = 0;
  let cx = 0;
  let cy = 0;
  for (let i = 0, n = poly.length; i < n; i++) {
    const [x0, y0] = poly[i];
    const [x1, y1] = poly[(i + 1) % n];
    const cross = x0 * y1 - x1 * y0;
    a += cross;
    cx += (x0 + x1) * cross;
    cy += (y0 + y1) * cross;
  }
  if (a === 0) {
    const n = poly.length || 1;
    return [poly.reduce((s, p) => s + p[0], 0) / n, poly.reduce((s, p) => s + p[1], 0) / n];
  }
  return [cx / (3 * a), cy / (3 * a)];
}
function toPointsAttr(poly, digits = 2) {
  return poly.map(([x, y]) => `${x.toFixed(digits)},${y.toFixed(digits)}`).join(" ");
}
function maxAreaError(cells) {
  const totalValue = cells.reduce((s, c) => s + c.value, 0);
  const totalArea = cells.reduce((s, c) => s + polygonArea(c.polygon), 0);
  if (!(totalValue > 0) || !(totalArea > 0)) return 0;
  let worst = 0;
  for (const c of cells) {
    const target = c.value / totalValue;
    const actual = polygonArea(c.polygon) / totalArea;
    if (target > 0) worst = Math.max(worst, Math.abs(actual - target) / target);
  }
  return worst;
}

// src/voronoi/prng.ts
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = a + 1831565813 >>> 0;
    let t = a;
    t = Math.imul(t ^ t >>> 15, t | 1);
    t ^= t + Math.imul(t ^ t >>> 7, t | 61);
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
function hashSeed(s) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}

// src/voronoi/layout.ts
function voronoiLayout(items, getValue, clip, opts = {}) {
  const positive = items.filter((n) => getValue(n) > 0);
  const total = positive.reduce((s, n) => s + getValue(n), 0);
  const minShare = opts.minShare ?? 5e-3;
  const kept = positive.filter((n) => getValue(n) / total >= minShare);
  const excludedItems = positive.length - kept.length;
  const excludedValue = total - kept.reduce((s, n) => s + getValue(n), 0);
  if (kept.length === 0 || clip.length < 3) {
    return { cells: [], error: 0, converged: true, excluded: excludedItems, excludedValue };
  }
  const convergenceRatio = opts.convergenceRatio ?? 1e-3;
  const maxIterationCount = opts.maxIterationCount ?? 200;
  const tolerance = opts.tolerance ?? 0.02;
  const seed = typeof opts.seed === "string" ? hashSeed(opts.seed) : opts.seed ?? 1;
  const root = hierarchy({ children: kept.map((node) => ({ node })) }).sum((d) => {
    const leaf = d;
    return leaf.node ? getValue(leaf.node) : 0;
  });
  const vt = voronoiTreemap().clip(clip).minWeightRatio(opts.minWeightRatio ?? 1e-4).convergenceRatio(convergenceRatio).maxIterationCount(maxIterationCount).prng(mulberry32(seed));
  vt(root);
  const cells = [];
  for (const leaf of root.children ?? []) {
    const polygon = leaf.polygon;
    const node = leaf.data.node;
    if (!polygon || polygon.length < 3) continue;
    cells.push({
      node,
      value: getValue(node),
      polygon,
      centroid: polygonCentroid(polygon),
      area: polygonArea(polygon)
    });
  }
  const error = maxAreaError(cells);
  return { cells, error, converged: error <= tolerance, excluded: excludedItems, excludedValue };
}
var FALLBACK_DIMS = { w: 420, h: 420 };
var LABEL_MIN_AREA = 900;
function VoronoiTreemap({
  items,
  getValue,
  getLabel,
  getColor,
  shape = "circle",
  clip,
  showLabels = true,
  formatValue,
  renderTooltip,
  onCellClick,
  convergenceRatio,
  maxIterationCount,
  minShare,
  minWeightRatio,
  seed,
  tolerance,
  className,
  style,
  height
}) {
  const wrapRef = useRef(null);
  const [dims, setDims] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    setDims({ w: el.clientWidth, h: el.clientHeight });
    const ro = new ResizeObserver(() => setDims({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const pin = useHoverPin({ excludeRefs: [wrapRef] });
  const w = dims.w || FALLBACK_DIMS.w;
  const h = dims.h || FALLBACK_DIMS.h;
  const clipPolygon = useMemo(() => {
    if (clip) return clip;
    if (shape === "rect") return rectPolygon(1, 1, Math.max(0, w - 2), Math.max(0, h - 2));
    const r = Math.max(0, Math.min(w, h) / 2 - 1);
    return circlePolygon(w / 2, h / 2, r);
  }, [clip, shape, w, h]);
  const { cells, error, converged, excluded, excludedValue } = useMemo(
    () => voronoiLayout(items, getValue, clipPolygon, {
      convergenceRatio,
      maxIterationCount,
      minShare,
      minWeightRatio,
      seed,
      tolerance
    }),
    [items, getValue, clipPolygon, convergenceRatio, maxIterationCount, minShare, minWeightRatio, seed, tolerance]
  );
  const active = pin.active;
  const activeCell = active == null ? null : cells[active] ?? null;
  return /* @__PURE__ */ jsxs(
    "div",
    {
      ref: wrapRef,
      className: "dt-voronoi" + (className ? ` ${className}` : ""),
      style: { position: "relative", width: "100%", height: height ?? "100%", ...style },
      children: [
        cells.length > 0 && /* @__PURE__ */ jsx("svg", { width: w, height: h, style: { display: "block" }, role: "img", "aria-label": "voronoi treemap", children: cells.map((cell, i) => {
          const color = getColor?.(cell.node, i) ?? DEFAULT_PALETTE[i % DEFAULT_PALETTE.length];
          const label = getLabel?.(cell.node);
          const isActive = active === i;
          return /* @__PURE__ */ jsxs(
            "g",
            {
              className: "dt-voronoi-cell",
              onMouseEnter: () => pin.hover(i),
              onMouseLeave: () => pin.hover(null),
              onClick: (e) => {
                pin.togglePin(i);
                onCellClick?.(cell.node, e);
              },
              style: { cursor: onCellClick ? "pointer" : void 0 },
              children: [
                /* @__PURE__ */ jsx(
                  "polygon",
                  {
                    points: toPointsAttr(cell.polygon),
                    fill: color,
                    fillOpacity: active == null || isActive ? 0.82 : 0.3,
                    stroke: "var(--dt-voronoi-edge, rgba(0,0,0,0.45))",
                    strokeWidth: 1
                  }
                ),
                showLabels && label && cell.area >= LABEL_MIN_AREA && /* @__PURE__ */ jsx(
                  "text",
                  {
                    x: cell.centroid[0],
                    y: cell.centroid[1],
                    textAnchor: "middle",
                    dominantBaseline: "middle",
                    fontSize: 11,
                    fill: "var(--dt-voronoi-ink, #10101a)",
                    pointerEvents: "none",
                    children: label
                  }
                )
              ]
            },
            i
          );
        }) }),
        activeCell && /* @__PURE__ */ jsx(
          "div",
          {
            className: "dt-voronoi-tip",
            style: {
              position: "absolute",
              left: Math.min(activeCell.centroid[0] + 8, Math.max(0, w - 180)),
              top: Math.max(0, activeCell.centroid[1] - 10),
              background: "var(--dt-voronoi-tip-bg, rgba(20,20,24,0.94))",
              color: "var(--dt-voronoi-tip-ink, #e6e6ea)",
              border: "1px solid var(--dt-voronoi-tip-border, #333)",
              borderRadius: 4,
              padding: "4px 8px",
              fontSize: 11,
              pointerEvents: "none",
              whiteSpace: "nowrap",
              zIndex: 1
            },
            children: renderTooltip?.(activeCell.node) ?? getLabel?.(activeCell.node) ?? String(activeCell.value)
          }
        ),
        (excluded > 0 || !converged && cells.length > 0) && // Say what was left out and how far off the areas are — an
        // unconverged tessellation must never read as exact.
        /* @__PURE__ */ jsxs("div", { className: "dt-voronoi-note", style: { fontSize: 11, opacity: 0.6, marginTop: 2 }, children: [
          excluded > 0 && `${excluded} too small to tessellate${formatValue ? ` (${formatValue(excludedValue)})` : ""}`,
          excluded > 0 && !converged && " \xB7 ",
          !converged && cells.length > 0 && `areas approximate (max ${(error * 100).toFixed(1)}% off target)`
        ] })
      ]
    }
  );
}

export { VoronoiTreemap, circlePolygon, hashSeed, maxAreaError, mulberry32, polygonArea, polygonCentroid, rectPolygon, toPointsAttr, voronoiLayout };
//# sourceMappingURL=index.js.map
//# sourceMappingURL=index.js.map