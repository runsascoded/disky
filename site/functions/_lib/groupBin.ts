/** A decoded row group's columns as one binary doc (the colo's copy, `index.ts` `putColoGroup`): parsing it is a
 *  header, typed-array views over the numbers and one UTF-8 decode per string column, where JSON parsed every value.
 *
 *  Layout (little-endian): `u32` header length, the header as UTF-8 JSON (`BinHeader`), padding to 8, then each
 *  column's section at its `off`:
 *  - `f`: `n` float64s (every value a finite number);
 *  - `s`: `n` int32 UTF-16 lengths (−1: null), then the values' UTF-8, concatenated (`bytes` long);
 *  - `j`: the column as a JSON array, UTF-8 (`bytes` long): booleans, numbers mixed with nulls or strings, or a string
 *    UTF-8 can't hold exactly (a lone surrogate, which JSON escapes and `TextEncoder` replaces).
 *  Every value a `GroupDoc` (`index.ts` `groupDoc`) holds decodes back exactly. */

export interface BinDoc { cols: string[]; v: ArrayLike<unknown>[] }

type Kind = 'f' | 's' | 'j'
interface BinHeader { cols: string[]; n: number; k: Kind[]; off: number[]; bytes: number[] }

const enc = new TextEncoder()
const dec = new TextDecoder()
const pad8 = (n: number): number => (n + 7) & ~7

/** A lone surrogate: UTF-8 can't encode it. */
const LONE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/

const kindOf = (col: ArrayLike<unknown>): Kind => {
  let num = true, str = true
  for (let i = 0; i < col.length; i++) {
    const x = col[i]
    if (typeof x !== 'number') num = false
    if (x !== null && (typeof x !== 'string' || LONE.test(x))) str = false
    if (!num && !str) return 'j'
  }
  return num ? 'f' : 's'
}

export function encodeBin(d: BinDoc): Uint8Array {
  const n = d.v[0]?.length ?? 0
  const k = d.v.map(kindOf)
  const parts: { kind: Kind; head?: Uint8Array; body: Uint8Array }[] = d.v.map((col, j) => {
    if (k[j] === 'f') return { kind: 'f', body: new Uint8Array(Float64Array.from(col as ArrayLike<number>).buffer) }
    if (k[j] === 'j') return { kind: 'j', body: enc.encode(JSON.stringify(Array.from(col))) }
    const lens = new Int32Array(n)
    let s = ''
    for (let i = 0; i < n; i++) {
      const x = col[i] as string | null
      if (x === null) lens[i] = -1
      else { lens[i] = x.length; s += x }
    }
    return { kind: 's', head: new Uint8Array(lens.buffer), body: enc.encode(s) }
  })
  // Sections from offset 0 of the body, each 8-aligned, so the typed-array views need no copy.
  const off: number[] = []
  const bytes: number[] = []
  let at = 0
  for (const p of parts) {
    off.push(at)
    bytes.push(p.body.length)
    at = pad8(at + (p.head?.length ?? 0) + p.body.length)
  }
  const header = enc.encode(JSON.stringify({ cols: d.cols, n, k, off, bytes } satisfies BinHeader))
  const base = pad8(4 + header.length)
  const out = new Uint8Array(base + at)
  new DataView(out.buffer).setUint32(0, header.length, true)
  out.set(header, 4)
  parts.forEach((p, j) => {
    let o = base + off[j]
    if (p.head) { out.set(p.head, o); o += p.head.length }
    out.set(p.body, o)
  })
  return out
}

export function decodeBin(buf: ArrayBuffer): BinDoc {
  const u8 = new Uint8Array(buf)
  const hl = new DataView(buf).getUint32(0, true)
  const h = JSON.parse(dec.decode(u8.subarray(4, 4 + hl))) as BinHeader
  const base = pad8(4 + hl)
  const v = h.cols.map((_, j): ArrayLike<unknown> => {
    const o = base + h.off[j]
    if (h.k[j] === 'f') return new Float64Array(buf, o, h.n)
    if (h.k[j] === 'j') return JSON.parse(dec.decode(u8.subarray(o, o + h.bytes[j]))) as unknown[]
    const lens = new Int32Array(buf, o, h.n)
    const s0 = o + 4 * h.n
    const all = dec.decode(u8.subarray(s0, s0 + h.bytes[j]))
    const col: (string | null)[] = new Array(h.n)
    let at = 0
    for (let i = 0; i < h.n; i++) {
      const L = lens[i]
      if (L < 0) col[i] = null
      else { col[i] = all.slice(at, at + L); at += L }
    }
    return col
  })
  return { cols: h.cols, v }
}
