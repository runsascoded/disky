/** VM mode's Worker-side adapter (specs/vm-mode.md §7): a parsed view as `vmd`'s `/v1/view` request, its answer as
 * `/api/subtree`'s or `/api/diff`'s body, and the signatures both ways (§4.3). Pure (WebCrypto only); no route calls
 * it yet: the lane decision and the wiring are the next phase's (specs/serverless-tiers.md §5). */
import type { QueryAst } from './queryAst.js'
import type { ClassScope, OwnerScope } from './scope.js'
import { ATTEN_DEFAULT, type DiffRow, type ViewNode } from './view.js'

const { ceil, log2, max, round } = Math

/** A generation's pins, as `vmd` echoes them (the pack's header). */
export interface VmPins {
  gen: string
  manifest: string
}

/** The cell rule (specs/vm-mode.md §2.2). */
export interface VmCell {
  share_log2?: number
  atten_log2: number
  depth?: number
  floor_bytes?: number
}

export type VmOwners = { in: string[] } | { not_in: string[] }

export interface VmViewRequest {
  pins: VmPins
  scan?: string
  from?: string
  to?: string
  path: string
  q?: QueryAst
  owners?: VmOwners
  classes?: number[]
  staged?: string[]
  cell: VmCell
  deadline_ms?: number
}

/** `/v1/view`'s tree: `ViewNode`'s shape, a diff's with the `to` side. */
export interface VmNode {
  name: string
  kind: 'file' | 'dir'
  bytes: number
  objects: number
  bytes_to?: number
  objects_to?: number
  matched?: true
  children?: VmNode[]
  folded?: number
}

export interface VmAnswer {
  v: 1
  tenant: string
  pins: VmPins
  scan?: string
  from?: string
  to?: string
  path: string
  cell: VmCell
  total: { bytes: number; objects: number; bytes_to?: number; objects_to?: number }
  nodes: number
  tree: VmNode
  timing: { pass_ms: number; merge_ms: number; total_ms: number; rows: number; dirs: number; shards: number }
}

/** `⌈log2(viewport area / cell area)⌉`: the share `2^-share_log2` is the cell's share of the viewport rounded down to a
 *  power of 2 (specs/serverless-tiers.md §7 item 1). */
export const shareLog2 = (w: number, h: number, minArea: number): number => max(0, ceil(log2((w * h) / minArea)))

/** The per-level attenuation as a power of 2 (`atten` 1 → 0, 2 → 1, 4 → 2); anything else has no exact form. */
export function attenLog2(atten: number): number {
  const k = round(log2(atten))
  if (!(atten >= 1) || 2 ** k !== atten) throw new Error(`atten ${atten}: VM mode takes a power of 2`)
  return k
}

/** An owner pool (`o=`) or a user lens as the VM's owner selection: owned = every slice but the unnamed one. */
export function vmOwners(o: { owner?: OwnerScope; lens?: string }): VmOwners | undefined {
  if (o.lens !== undefined) return { in: [o.lens] }
  if (o.owner === undefined) return undefined
  if (o.owner === 'owned') return { not_in: [''] }
  if (o.owner === 'unowned') return { in: [''] }
  return { not_in: ['', ...o.owner.not] }
}

export interface VmViewOpts {
  pins: VmPins
  date?: string
  from?: string
  to?: string
  path: string
  query?: QueryAst
  owner?: OwnerScope
  lens?: string
  classes?: ClassScope
  staged?: string[]
  w: number
  h: number
  minArea: number
  /** The per-level attenuation (a power of 2); default the Worker's own, `ATTEN_DEFAULT` (halving: `atten_log2 = 1`,
   *  Ryan's decision, specs/vm-mode.md §10 item 1). Always sent, so the request names its policy. */
  atten?: number
  maxDepth?: number
  deadlineMs?: number
}

export function vmRequest(o: VmViewOpts): VmViewRequest {
  const cell: VmCell = { share_log2: shareLog2(o.w, o.h, o.minArea), atten_log2: attenLog2(o.atten ?? ATTEN_DEFAULT),...(o.maxDepth ? { depth: o.maxDepth } : {}) }
  const owners = vmOwners(o)
  return {
    pins: o.pins,
    ...(o.date ? { scan: o.date } : { from: o.from, to: o.to }),
    path: o.path.replace(/\/+$/, ''),
    ...(o.query ? { q: o.query } : {}),
    ...(owners ? { owners } : {}),
    ...(o.classes ? { classes: [...o.classes].map(Number).sort((x, y) => x - y) } : {}),
    ...(o.staged ? { staged: o.staged } : {}),
    cell,
    ...(o.deadlineMs ? { deadline_ms: o.deadlineMs } : {}),
  }
}

const viewNode = (n: VmNode): ViewNode => ({
  name: n.name,
  kind: n.kind,
  bytes: n.bytes,
  objects: n.objects,
  ...(n.children?.length ? { children: n.children.map(viewNode) } : {}),
  ...(n.folded !== undefined ? { folded: n.folded } : {}),
  ...(n.matched ? { matched: true as const } : {}),
})

/** The byte threshold a drawn child of the folder clears (what `/api/subtree`'s `threshold` reports). */
export const vmThreshold = (a: VmAnswer): number => {
  const t = max(a.total.bytes, a.total.bytes_to ?? 0)
  return max(a.cell.floor_bytes ?? 0, a.cell.share_log2 === undefined ? 0 : t / 2 ** a.cell.share_log2)
}

/** `/api/subtree`'s body from a view's answer (`rootLabel`: the deployment's `ROOT_LABEL`, the root's name). */
export function subtreeBody(a: VmAnswer, o: { date: string; w: number; h: number; minArea: number; atten?: number; rootLabel: string; q?: string }) {
  const tree = viewNode(a.tree)
  if (a.path === '') tree.name = o.rootLabel
  return {
    date: o.date,
    path: a.path,
    w: o.w,
    h: o.h,
    minArea: o.minArea,
    atten: o.atten ?? ATTEN_DEFAULT,
    tier: 'vm',
    index: `vm:${a.pins.gen}/${a.pins.manifest}`,
    threshold: round(vmThreshold(a)),
    nodes: a.nodes,
    truncated: false,
    ...(o.q !== undefined ? { q: o.q } : {}),
    tree,
  }
}

const status = (r: Pick<DiffRow, 'bytes_before' | 'bytes_after' | 'objects_before' | 'objects_after'>): DiffRow['status'] =>
  r.objects_before === 0 && r.bytes_before === 0 ? 'added'
  : r.objects_after === 0 && r.bytes_after === 0 ? 'removed'
  : r.bytes_before !== r.bytes_after || r.objects_before !== r.objects_after ? 'changed'
  : 'unchanged'

/** `/api/diff`'s rows and totals from a diff's answer: every node (pre-order), its path relative to the folder, an
 *  `(other)` under its parent's path; `expanded` where the node's children are drawn. */
export function diffBody(a: VmAnswer): { rows: DiffRow[]; bytes_before: number; bytes_after: number; objects_before: number; objects_after: number } {
  const rows: DiffRow[] = []
  const walk = (n: VmNode, rel: string, depth: number) => {
    for (const c of n.children ?? []) {
      const p = rel ? `${rel}/${c.name}` : c.name
      const sides = { bytes_before: c.bytes, bytes_after: c.bytes_to ?? 0, objects_before: c.objects, objects_after: c.objects_to ?? 0 }
      rows.push({ path: p, depth: depth + 1, kind: c.kind, status: status(sides), ...sides, ...(c.children?.length ? { expanded: true as const } : {}) })
      walk(c, p, depth + 1)
    }
  }
  walk(a.tree, '', 0)
  return { rows, bytes_before: a.total.bytes, bytes_after: a.total.bytes_to ?? 0, objects_before: a.total.objects, objects_after: a.total.objects_to ?? 0 }
}

const enc = new TextEncoder()
const hex = (b: ArrayBuffer): string => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('')
export const sha256Hex = async (body: string): Promise<string> => hex(await crypto.subtle.digest('SHA-256', enc.encode(body)))
const hmac = async (key: string, s: string): Promise<string> => {
  const k = await crypto.subtle.importKey('raw', enc.encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  return hex(await crypto.subtle.sign('HMAC', k, enc.encode(s)))
}

/** The request's headers (specs/vm-mode.md §4.3): the tenant, the time, the HMAC over method, path, tenant, time and
 *  the body's SHA-256. */
export async function signRequest(key: string, tenant: string, method: string, path: string, body: string, ts = Math.floor(Date.now() / 1000)): Promise<Record<string, string>> {
  const t = String(ts)
  return { 'x-disky-tenant': tenant, 'x-disky-ts': t, 'x-disky-sig': await hmac(key, `v1\n${method}\n${path}\n${tenant}\n${t}\n${await sha256Hex(body)}`) }
}

/** Whether a reply's signature is the server's: over the tenant, the request's time and the reply's body. */
export async function checkResponse(key: string, tenant: string, ts: string, body: string, sig: string | null): Promise<boolean> {
  if (!sig) return false
  const want = await hmac(key, `v1-resp\n${tenant}\n${ts}\n${await sha256Hex(body)}`)
  if (want.length !== sig.length) return false
  let d = 0
  for (let i = 0; i < want.length; i++) d |= want.charCodeAt(i) ^ sig.charCodeAt(i)
  return d === 0
}

/** The answer's pins equal the request's (the Worker caches only then, specs/serverless-tiers.md §5.1). */
export const pinsMatch = (a: VmPins, b: VmPins): boolean => a.gen === b.gen && a.manifest === b.manifest
