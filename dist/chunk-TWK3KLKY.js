// src/squarify.ts
function squarify(items, x, y, w, h, getSize) {
  const out = [];
  items = items.filter((it) => getSize(it) > 0);
  const total = items.reduce((s, it) => s + getSize(it), 0);
  if (!total || w <= 0 || h <= 0) return out;
  const scale = w * h / total;
  let row = [];
  let rowSum = 0;
  const rest = items.slice();
  const worst = (r, len) => {
    const s = r.reduce((a, it) => a + getSize(it) * scale, 0);
    let mn = Infinity;
    let mx = 0;
    for (const it of r) {
      const a = getSize(it) * scale;
      mn = Math.min(mn, a);
      mx = Math.max(mx, a);
    }
    const s2 = s * s;
    const l2 = len * len;
    return Math.max(l2 * mx / s2, s2 / (l2 * mn));
  };
  const layoutRow = () => {
    const len = Math.min(w, h);
    const thick = rowSum / len;
    let off = 0;
    for (const it of row) {
      const l = getSize(it) * scale / thick;
      if (w <= h) out.push({ it, x: x + off, y, w: l, h: thick });
      else out.push({ it, x, y: y + off, w: thick, h: l });
      off += l;
    }
    if (w <= h) {
      y += thick;
      h -= thick;
    } else {
      x += thick;
      w -= thick;
    }
  };
  while (rest.length) {
    const len = Math.min(w, h);
    const it = rest[0];
    if (!row.length || worst([...row, it], len) <= worst(row, len)) {
      row.push(rest.shift());
      rowSum += getSize(it) * scale;
    } else {
      layoutRow();
      row = [];
      rowSum = 0;
    }
  }
  if (row.length) layoutRow();
  return out;
}
function foldSmall(items, w, h, getSize, mergeSmall, minArea = 16) {
  const vis = items.filter((it) => getSize(it) > 0);
  const total = vis.reduce((s, it) => s + getSize(it), 0);
  if (!total || w <= 0 || h <= 0) return vis;
  const scale = w * h / total;
  const kept = vis.filter((it) => getSize(it) * scale >= minArea);
  const tiny = vis.filter((it) => getSize(it) * scale < minArea);
  if (tiny.length < 2) return vis;
  return [...kept, mergeSmall(tiny)];
}
function foldThin(rects, minSide, mergeSmall) {
  const keep = [];
  const thin = [];
  for (const r of rects) (Math.min(r.w, r.h) < minSide ? thin : keep).push(r.it);
  if (thin.length < 2) return null;
  return [...keep, mergeSmall(thin)];
}
function squarifyRemainder(items, x, y, w, h, getSize, minSide = 7, remainderFrac = 0.14) {
  const laid = squarify(items, x, y, w, h, getSize);
  if (w <= 0 || h <= 0) return laid;
  const big = [];
  const tail = [];
  for (const r of laid) (Math.min(r.w, r.h) < minSide ? tail : big).push(r.it);
  if (tail.length < 2 || big.length === 0) return laid;
  const sum = (arr) => arr.reduce((s, it) => s + Math.max(0, getSize(it)), 0);
  const tailSum = sum(tail);
  const total = tailSum + sum(big);
  if (!total) return laid;
  const frac = Math.min(0.5, Math.max(tailSum / total, remainderFrac));
  if (w >= h) {
    const tw = w * frac;
    return [
      ...squarify(big, x, y, w - tw, h, getSize),
      ...squarify(tail, x + (w - tw), y, tw, h, getSize)
    ];
  }
  const th = h * frac;
  return [
    ...squarify(big, x, y, w, h - th, getSize),
    ...squarify(tail, x, y + (h - th), w, th, getSize)
  ];
}

export { foldSmall, foldThin, squarify, squarifyRemainder };
//# sourceMappingURL=chunk-TWK3KLKY.js.map
//# sourceMappingURL=chunk-TWK3KLKY.js.map