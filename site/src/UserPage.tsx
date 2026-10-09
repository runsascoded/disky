import { Treemap, type CellStyle } from '@disk-tree/react'
import { useQuery } from '@tanstack/react-query'
import { stringParam, useUrlState } from 'use-prms'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Avatar } from './Avatar'
import { buildUserIndex, userColor } from './colors'
import { fmtDate } from './OwnerFactChip'
import { DEFAULT_STORE } from './stores'
import { Treemap as UserTreemap } from './Treemap'
import { ScanPicker } from './ScanPicker'
import { SiteNav } from './SiteNav'
import { useScan, useScans } from './scan'
import { Skeleton } from './Busy'
import { SiteKbd } from './SiteKbd'
import { useDocTitle, SITE } from './title'
import { Tooltip } from './Tooltip'
import { UserChip, canonId, ghHandle, shortName, shortUserKey } from './UserChip'
import {
  CLASS_NAMES, CLASS_PRICE_US,
  ratePerByte, fmtBytesIec, fmtN, fmtUsd,
  type Meta, type TreeNode, type UserInfo,
} from './types'

// Per-user estate pages: `/users` ranks everyone by owned bytes; `/user/:id`
// answers "what do I own, and what was assigned to me?".
//
// Everything here is folded server-side from the index tiers and the live
// ownership ledger (`/api/owners`, `/api/estate`, the user lens of
// `/api/subtree`): the same numbers the map's rollup shows, with no scan tree
// on the client (specs/view-serving.md §2).

type ClassMix = Record<string, number>
/** One person's owned bytes (assignments applied) + the class mix behind them. */
interface Owned { b: number; mix: ClassMix }

// `gs://marin-<bucket>/<path>/` → the treemap's URL path.
const prefixToPath = (prefix: string): string => {
  const m = /^[a-z0-9]+:\/\/(.*?)\/?$/.exec(prefix)
  return m ? m[1] : prefix
}

const store = DEFAULT_STORE

function useLatestScan() {
  return useScans(store).data?.[0] ?? null
}

function useScanFile<T>(name: string, asof: string | null) {
  return useQuery<T>({
    queryKey: [name, store.key, asof],
    queryFn: () => fetch(`${store.base}/${asof}/${name}.json`).then(r => r.json() as Promise<T>),
    enabled: !!asof,
    staleTime: Infinity,
  })
}

/** Per-user owned bytes (assignments applied) from `/api/owners`, keyed by
 * canonical id. */
function useOwned(asof: string | null): { data: Map<string, Owned> | null; error: Error | null } {
  const q = useQuery<{ users: Record<string, Owned> }, Error>({
    queryKey: ['owners', asof],
    enabled: !!asof,
    staleTime: 30_000,
    refetchInterval: 30_000,
    // A fresh ledger head recomputes server-side (~10s cold) — don't give up
    // on the first slow answer.
    retry: 2,
    queryFn: async () => {
      const r = await fetch(`/api/owners?date=${asof}`, { credentials: 'include' })
      if (!r.ok) throw new Error(`owners: ${r.status}`)
      return r.json()
    },
  })
  const data = useMemo(() => {
    if (!q.data) return null
    const m = new Map<string, Owned>()
    for (const [u, o] of Object.entries(q.data.users)) {
      const id = canonId(u)
      const cur = m.get(id)
      if (!cur) m.set(id, { b: o.b, mix: { ...o.mix } })
      else { cur.b += o.b; for (const [c, b] of Object.entries(o.mix)) cur.mix[c] = (cur.mix[c] ?? 0) + b }
    }
    return m
  }, [q.data])
  return { data, error: q.error ?? null }
}

// Est. $/mo with the storage-class mix behind it on hover.
function DollarCell({ b, mix }: { b: number; mix?: Record<string, number> }) {
  if (!mix || !b) return <>—</>
  const rows = Object.entries(mix)
    .sort(([a], [c]) => Number(a) - Number(c))
    .map(([cls, cb]) => ({
      name: CLASS_NAMES[cls] ?? cls,
      b: cb,
      usd: (cb / 1024 ** 3) * (CLASS_PRICE_US[cls] ?? 0.02),
    }))
  return (
    <Tooltip content={
      <table className="class-tt">
        <tbody>
          {rows.map(r => (
            <tr key={r.name}>
              <td>{r.name}</td>
              <td className="num">{fmtBytesIec(r.b)}</td>
              <td className="num">{fmtUsd(r.usd)}/mo</td>
            </tr>
          ))}
        </tbody>
      </table>
    }>
      <span className="has-tt">{fmtUsd(ratePerByte(mix) * b)}</span>
    </Tooltip>
  )
}

// One-level treemap of the whole estate by owner: every user, plus one
// "unowned" pool for everything no person owns.
interface OwnerCell {
  n: string
  b: number
  pool?: boolean   // the unowned pool (no user)
  id?: string      // canonical user id → /user/:id
  c?: OwnerCell[]
}

// The pool tile: the unowned gray pulled toward dark, so the user tiles'
// palette colours read as people, not more of the pool.
const POOL_TILE_BG = 'color-mix(in oklab, var(--t-unattr) 48%, #131311)'

/** The owner tiles (users + the unowned pool) — ONE derivation shared by
 * the map and its legend. Live owned bytes (assignments applied) win over scan
 * meta when loaded. */
function ownerCells(meta: Meta, owned: Map<string, Owned> | null): OwnerCell[] {
  const metaUsers: UserInfo[] = meta.users ?? []
  const users: { u: string; b: number }[] = owned
    ? [...owned.entries()].map(([u, o]) => ({ u, b: o.b })).filter(x => x.b > 0)
    : metaUsers
  const cells: OwnerCell[] = users.map(u => ({ n: shortName(u.u), id: u.u, b: u.b }))
  const userSum = users.reduce((s, u) => s + u.b, 0)
  const unattr = Math.max(0, meta.total_bytes - userSum)
  if (unattr > 0) cells.push({ n: 'unowned', b: unattr, pool: true })
  return cells.sort((a, b) => b.b - a.b)
}

function MapLegend({ cells }: { cells: OwnerCell[] }) {
  // User tiles carry their names; only the pool needs a key.
  if (!cells.some(c => c.pool)) return null
  return (
    <div className="map-legend">
      <span><i style={{ background: POOL_TILE_BG }} />unowned — bytes no person owns</span>
    </div>
  )
}

function UsersMap({ meta, owned, redact = false }: {
  meta: Meta
  owned: Map<string, Owned> | null
  /** og:image mode — names only: no sizes, no tooltips, no drill. */
  redact?: boolean
}) {
  const navigate = useNavigate()
  const root = useMemo(
    (): OwnerCell => ({ n: '', b: meta.total_bytes, c: ownerCells(meta, owned) }),
    [meta, owned],
  )
  const userIdx = useMemo(() => buildUserIndex(meta.users ?? []), [meta])
  return (
    <div className="users-map">
      <Treemap<OwnerCell>
        root={root}
        getSize={n => n.b}
        getChildren={n => n.c}
        getLabel={n => n.n}
        formatSize={redact ? () => '' : n => fmtBytesIec(n)}
        chrome={false}
        fullscreen={false}
        colorForCell={(n): CellStyle | null => {
          // The site's user palette (the color-by-owner map), the pool its own gray.
          if (!n.id) return n.pool ? { bg: POOL_TILE_BG } : null
          return { bg: userColor(n.id, userIdx) }
        }}
        renderTooltip={(n) => {
          if (redact) return null
          return (
            <div>
              <b>{n.n}</b>
              <div>{fmtBytesIec(n.b, true)} · {meta.total_bytes ? ((100 * n.b) / meta.total_bytes).toFixed(1) : 0}%</div>
              {n.id && <div className="tt-hint">click for breakdown</div>}
            </div>
          )
        }}
        // Real anchors (this map is one flat level, so every tile qualifies):
        // Vimium hints, cmd-click, native pointer all work; plain clicks
        // still route through the SPA below.
        cellHref={n =>
          n.id ? `/user/${n.id}`
          : n.pool ? '/?o'
          : undefined}
        onCellClick={(n) => {
          if (redact) return true
          // Every tile goes somewhere sane: users to their page, the
          // unattributed pool to the matching home lens.
          if (n.id) navigate(`/user/${n.id}`)
          else if (n.pool) navigate('/?o')
          return true
        }}
      />
    </div>
  )
}

interface Estate {
  user: string
  date: string
  head: number
  bytes: number
  objects: number
  mix: ClassMix
  assignments: { prefix: string; ts: number; bytes: number; objects: number; repainted_by?: string }[]
}

/** `/users/og` — fixed 1200×630 unfurl render of the owner map: names only
 * (no sizes, no $, no tooltips). Screenshot via `pnpm shots`. */
export function UsersOgPage() {
  const asof = useLatestScan()
  const metaQ = useScanFile<Meta>('meta', asof)
  const { data: owned } = useOwned(asof)
  useEffect(() => {
    const prev = document.documentElement.dataset.theme
    document.documentElement.dataset.theme = 'dark'
    return () => {
      if (prev) document.documentElement.dataset.theme = prev
      else delete document.documentElement.dataset.theme
    }
  }, [])
  return (
    <div className="og og-users">
      <div className="og-head">
        <h1>{SITE} — users</h1>
        <p>Who owns what: every user’s bytes, largest first.</p>
      </div>
      <div className="og-map">
        {metaQ.data && owned && <UsersMap meta={metaQ.data} owned={owned} redact />}
      </div>
      {metaQ.data && <MapLegend cells={ownerCells(metaQ.data, owned)} />}
    </div>
  )
}

export function UsersPage() {
  useDocTitle('Users')
  const scan = useScan(store)
  const asof = scan.asof
  const metaQ = useScanFile<Meta>('meta', asof)
  const mixes = metaQ.data?.user_class_bytes
  // Per-user owned bytes from /api/owners — the ledger folded server-side
  // against the floor-free index (assignments applied), so the table, the tiles
  // and the map's root rollup agree, and no tree.json. Scan meta is only the
  // pre-load fallback.
  const { data: owned, error: ownedErr } = useOwned(asof)
  const users = useMemo(() => {
    const metaUsers = metaQ.data?.users ?? []
    if (!owned) return [...metaUsers].sort((a, b) => b.b - a.b)
    return [...owned.entries()]
      .map(([u, o]) => ({ u, b: o.b }))
      .filter(x => x.b > 1e9)
      .sort((a, b) => b.b - a.b)
  }, [metaQ.data, owned])
  const mixOf = (u: string): ClassMix | undefined => owned?.get(u)?.mix ?? mixes?.[u]
  // Footer totals over exactly the rows shown (same assignments-applied basis);
  // $ only sums users whose class mix is known, so it's a floor, flagged as such.
  const totals = useMemo(() => {
    const t = { b: 0, usd: 0, priced: 0 }
    for (const u of users) {
      t.b += u.b
      const mix = owned?.get(u.u)?.mix ?? mixes?.[u.u]
      if (mix) { t.usd += ratePerByte(mix) * u.b; t.priced++ }
    }
    return t
  }, [users, mixes, owned])
  return (
    <main className="user-page">
      <SiteNav><ScanPicker scan={scan} /></SiteNav>
      <header>
        <div className="hrow">
          <h1>Users</h1>
        </div>
        <p className="sub">Everyone who owns storage{asof && <> in the {asof} scan</>}, largest first — the scan’s attribution with live assignments applied. Click a user (row or tile) for their breakdown.</p>
      </header>
      {ownedErr && <p className="tab-note" style={{ color: 'var(--s3)' }}>Couldn’t load the owner totals: {ownedErr.message}</p>}
      {metaQ.isLoading && <Skeleton height={300} label="loading users…" />}
      {metaQ.data && (
        <>
          <UsersMap meta={metaQ.data} owned={owned} />
          <MapLegend cells={ownerCells(metaQ.data, owned)} />
        </>
      )}
      {users.length > 0 && (
        <table className="worklist">
          <thead>
            <tr>
              <th>User</th>
              <th className="num">owned</th>
              <th className="num usd">est. $/mo</th>
            </tr>
          </thead>
          <tbody>
            {users.map(u => (
              <tr key={u.u}>
                <td className="user-td">
                  <Link className="user-link" to={`/user/${u.u}`}>
                    <UserChip who={u.u} />
                  </Link>
                </td>
                <td className="num">{fmtBytesIec(u.b)}</td>
                <td className="num usd">{owned || mixes ? <DollarCell b={u.b} mix={mixOf(u.u)} /> : '…'}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="total-row">
              <td>
                <Tooltip content="Sum of the rows above — bytes owned by some user (inferred from the scan, or assigned in the ledger). Unowned bytes (no owner: shared datasets, communal pools) are in no row.">
                  <span className="dotted">Total</span>
                </Tooltip>
                {' '}<span style={{ fontWeight: 400, opacity: 0.7 }}>· {users.length} users</span>
              </td>
              <td className="num">{fmtBytesIec(totals.b)}</td>
              <td className="num usd">
                {totals.priced
                  ? <Tooltip content={totals.priced < users.length ? `${totals.priced} of ${users.length} users have a storage-class mix; the rest are unpriced, so this is a floor` : 'sum of the per-user estimates'}>
                      <span className="has-tt">{totals.priced < users.length ? '≥ ' : ''}{fmtUsd(totals.usd)}</span>
                    </Tooltip>
                  : '—'}
              </td>
            </tr>
          </tfoot>
        </table>
      )}
      <SiteKbd />
    </main>
  )
}

const PAGE = 25

function AssignmentsTable({ rows }: { rows: Estate['assignments'] }) {
  const [page, setPage] = useState(0)
  const pages = Math.ceil(rows.length / PAGE)
  const p = Math.min(page, pages - 1)
  const slice = rows.slice(p * PAGE, p * PAGE + PAGE)
  return (
    <>
      <table className="worklist assignments">
        <thead>
          <tr><th>Prefix</th><th className="num">bytes</th><th className="num">objects</th><th>Assigned</th></tr>
        </thead>
        <tbody>
          {slice.map(r => (
            <tr key={r.prefix} className={r.repainted_by ? 'repainted' : undefined}>
              <td className="prefix">
                <Link to={`/${prefixToPath(r.prefix)}`}>{r.prefix}</Link>
                {r.repainted_by && <span className="memo" title={`a newer assignment on ${r.repainted_by} covers this one`}> — superseded</span>}
              </td>
              <td className="num">{fmtBytesIec(r.bytes)}</td>
              <td className="num">{fmtN(r.objects)}</td>
              <td>{fmtDate(r.ts)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {pages > 1 && (
        <div className="pager">
          <button type="button" disabled={p === 0} onClick={() => setPage(p - 1)}>← prev</button>
          <span className="pager-count">{p * PAGE + 1}–{p * PAGE + slice.length} of {rows.length}</span>
          <button type="button" disabled={p >= pages - 1} onClick={() => setPage(p + 1)}>next →</button>
        </div>
      )}
    </>
  )
}

/** `/user/:id/og` — fixed 1200×630 per-user unfurl card: avatar, name, and
 * their share of the estate (a percentage only — no bytes, no $). Screenshot
 * by `scripts/shoot-user-ogs.mjs`. */
export function UserOgPage() {
  const { id = '' } = useParams()
  const asof = useLatestScan()
  const metaQ = useScanFile<Meta>('meta', asof)
  const { data: owned } = useOwned(asof)
  useEffect(() => {
    const prev = document.documentElement.dataset.theme
    document.documentElement.dataset.theme = 'dark'
    return () => {
      if (prev) document.documentElement.dataset.theme = prev
      else delete document.documentElement.dataset.theme
    }
  }, [])
  const b = owned?.get(id)?.b ?? 0
  const total = metaQ.data?.total_bytes ?? 0
  const pct = total ? (100 * b) / total : 0
  return (
    <div className="og og-user">
      <div className="og-head ogu-head">
        <Avatar github={ghHandle(id)} name={shortName(id)} size={110} />
        <div>
          <h1>{shortName(id)}</h1>
          <p>{SITE} — their share of the estate.</p>
        </div>
      </div>
      {b > 0 && (
        <>
          <div className="ogu-bar">
            <div style={{ width: `${Math.max(0.5, pct)}%`, background: 'var(--s1)' }} />
          </div>
          <div className="ogu-legend">
            <span><i style={{ background: 'var(--s1)' }} />owned <b>{pct < 1 ? pct.toFixed(1) : Math.round(pct)}%</b> of {store.rootLabel}</span>
          </div>
        </>
      )}
    </div>
  )
}

export function UserPage() {
  const { id = '' } = useParams()
  useDocTitle(shortName(id))
  const scan = useScan(store)
  const asof = scan.asof
  const metaQ = useScanFile<Meta>('meta', asof)
  // The estate, folded server-side: owned bytes (assignments applied) and the
  // assignments themselves.
  const estateQ = useQuery<Estate>({
    queryKey: ['estate', asof, id],
    enabled: !!asof && !!id,
    staleTime: 30_000,
    refetchInterval: 30_000,
    queryFn: async () => {
      const r = await fetch(`/api/estate?date=${asof}&user=${encodeURIComponent(id)}`, { credentials: 'include' })
      if (!r.ok) throw new Error(`estate: ${r.status}`)
      return r.json()
    },
  })
  const estate = estateQ.data ?? null
  // Drill state lives in `?p=` so deep views are shareable and the back
  // button walks back out (same contract as the homepage map).
  const [pP, setPP] = useUrlState('p', stringParam())
  // The user's bytes as a drillable map (the homepage's user lens).
  const mapQ = useQuery<{ tree: TreeNode }>({
    queryKey: ['user-map', asof, id],
    enabled: !!asof && !!id,
    staleTime: Infinity,
    retry: (n: number, e: Error) => !/^4\d\d/.test(e.message) && n < 3,
    queryFn: async () => {
      const r = await fetch(`/api/subtree?date=${asof}&path=&w=1200&h=720&lens=user:${encodeURIComponent(id)}`, { credentials: 'include' })
      if (!r.ok) throw new Error(`${r.status}`)
      return r.json()
    },
  })
  const scopedTree = mapQ.data?.tree && mapQ.data.tree.b > 0 ? mapQ.data.tree : null
  const owned = estate?.bytes ?? 0
  const mix = estate?.mix && Object.keys(estate.mix).length ? estate.mix : metaQ.data?.user_class_bytes?.[id]
  const assignments = useMemo(() => [...(estate?.assignments ?? [])].sort((a, b) => b.bytes - a.bytes), [estate])
  // Resolve `?p=` against the scoped tree each render; a vanished segment
  // truncates to its deepest surviving ancestor.
  const mapPath = useMemo((): TreeNode[] | undefined => {
    if (!scopedTree) return undefined
    const path = [scopedTree]
    let cur: TreeNode = scopedTree
    for (const s of (pP ?? '').split('/').filter(Boolean)) {
      const next = cur.c?.find(c => c.n === s)
      if (!next) break
      path.push(next)
      cur = next
    }
    return path
  }, [scopedTree, pP])
  const loading = !asof || estateQ.isLoading

  return (
    <main className="user-page">
      <SiteNav><ScanPicker scan={scan} /></SiteNav>
      <header>
        <div className="hrow">
          <h1 className="user-head">
            <Avatar github={ghHandle(id)} name={shortName(id)} size={36} />
            {shortName(id)}
          </h1>
          <span style={{ display: 'inline-flex', gap: '1.2em' }}>
            <Link className="nav-files" to={`/?o=${shortUserKey(id)}`} style={{ fontSize: '0.9em' }}>Home,&nbsp;filtered&nbsp;to&nbsp;{shortName(id)}&nbsp;→</Link>
            <Link className="nav-files" to="/users" style={{ fontSize: '0.9em' }}>All&nbsp;users</Link>
          </span>
        </div>
        <p className="sub">
          {fmtBytesIec(owned, true)} owned ({asof ?? '…'} scan + live assignments)
          {mix != null && owned > 0 && <> · est. {fmtUsd(ratePerByte(mix) * owned)}/mo</>}
          {assignments.length > 0 && <> · {fmtN(assignments.length)} prefix{assignments.length === 1 ? '' : 'es'} assigned to {shortName(id)}</>}.
        </p>
      </header>

      {loading && <Skeleton height={200} label="loading estate…" />}
      {estateQ.error && <p className="tab-note" style={{ color: 'var(--s3)' }}>Couldn’t load the estate: {estateQ.error.message}</p>}
      {!loading && !estateQ.error && owned === 0 && (
        <p className="tab-note">No data owned by or assigned to “{id}”.</p>
      )}

      {owned > 0 && (
        <>
          {mapQ.isPending && !!asof && <Skeleton height={320} className="user-mini-map" label="loading map…" />}
          {scopedTree && scopedTree.b > 0 && (
            <div className="user-mini-map">
              <UserTreemap
                root={scopedTree}
                mode="tree"
                userIdx={new Map()}
                dateRange={null}
                scheme={store.scheme}
                path={mapPath}
                onPathChange={pth => setPP(pth.slice(1).map(n => n.n).join('/') || undefined)}
              />
            </div>
          )}
        </>
      )}

      {assignments.length > 0 && (
        <>
          <h2>Assigned</h2>
          <p className="tab-note">Prefixes assigned to {shortName(id)} in the ledger — counted in the total above immediately; the scan pipeline formalizes the ownership on its next run.</p>
          <AssignmentsTable rows={assignments} />
        </>
      )}
      <SiteKbd />
    </main>
  )
}
