// The leaf viewer: an opened object (`?open=`) rendered inside the map page by
// `@rdub/file-tree`'s renderer for its type — parquet (schema + row-group
// paging), CSV/TSV, JSON, markdown, text, images / video / audio, PDF.
// Disky owns navigation (map, table, crumbs); this panel only reads bytes,
// from wherever `objectSource` says this store's objects can be read.
// Loaded lazily (App.tsx): the viewers pull hyparquet and friends.
import { cloneElement, isValidElement, useEffect, useMemo, useRef, type ReactElement } from 'react'
import { useQuery } from '@tanstack/react-query'
import { MediaViewer, PdfViewer, TextViewer, parsePath } from '@rdub/file-tree/react'
import { HttpStore } from '@rdub/file-tree/stores/http'
import { makeParquetViewer, type ParquetCellRenderer } from '@rdub/file-tree/renderers/parquet'
import { makeCsvViewer } from '@rdub/file-tree/renderers/csv'
import { makeJsonTreeRenderer } from '@rdub/file-tree/renderers/json'
import { renderMarkdown } from '@rdub/file-tree/renderers/markdown'
import type { ElideCtx } from '@rdub/file-tree/renderers/table'
import { NotFoundError, type GetResult, type ListResult, type Range, type Store as FtStore } from '@rdub/file-tree'
import { CopyName } from './CopyName'
import { epochDaysToDate } from './colors'
import { objectSource, publicUrl, type ObjectSource, type ProxyInfo } from './objects'
import { useStore, useStoreFetch } from './store'
import { storeQuery } from './stores'
import { Tooltip } from './Tooltip'
import type { TreeNode } from './types'
import { CLASS_NAMES, fmtN } from './types'
import { useUnits } from './units'
import { pathCopy, pathText } from './pathCrumbs'

/** A file-tree `Store` over a public bucket's base URL: range GETs straight
 *  to `<base>/<key>` (the bucket's CORS must admit this site). Objects are
 *  opened by key, never listed, so `list` is unsupported. */
function publicStore(base: string): FtStore {
  return {
    async list(): Promise<ListResult> { throw new Error('a public object base is not listable') },
    async get(path: string, range?: Range): Promise<GetResult> {
      const res = await fetch(publicUrl(base, path), range ? { headers: { Range: `bytes=${range.offset}-${range.offset + range.length - 1}` } } : undefined)
      if (res.status === 404) throw new NotFoundError(path)
      if (!res.ok) throw new Error(`get ${path}: ${res.status}`)
      const cr = res.headers.get('Content-Range')
      const total = cr ? parseInt(cr.split('/')[1], 10) : NaN
      const contentType = res.headers.get('Content-Type') ?? undefined
      return {
        bytes: new Uint8Array(await res.arrayBuffer()),
        ...(Number.isFinite(total) ? { totalSize: total } : {}),
        ...(contentType ? { contentType } : {}),
      }
    },
    capabilities: { range: true },
    getUrl: (path: string) => publicUrl(base, path),
    describe: () => base,
  }
}

// Wide cells clip at 30em; the clipped tail comes back on hover in the site's
// floating tooltip (file-tree's `elide` seam), only for values long enough
// to plausibly clip.
const ELIDE_MIN = 40
const elideTooltip = (ctx: ElideCtx) =>
  ctx.text == null || ctx.text.length < ELIDE_MIN ? ctx.node : <Tooltip content={<code className="elide-full">{ctx.text}</code>}>{ctx.node}</Tooltip>

const SIZE_COLS = /^(size|size_bytes|bytes|b)$/
const HEX_ID_COLS = /(^|_)(id|version_id|etag|md5|sha\w*|hash)$/
const HEX_SHOW = 8
// Object keys / paths: every row shares a long prefix, so an end-clipped cell
// shows the same 40 characters on every line and hides the part that differs.
const PATH_COLS = /^(name|path|key|dir|prefix|parent)$/
const CLASS_COLS = /^(storage_class|storage_class_id|class|cl)$/
// A byte cell as `<number> <unit>` with the unit in its own slot, so a
// right-aligned column's numbers line up whatever the unit.
const byteCell = (n: number, fmtBytes: (b: number) => string) => {
  const [num, unit] = n < 1024 ? [fmtN(n), ''] : fmtBytes(n).split(' ')
  return (
    <span style={{ fontVariantNumeric: 'tabular-nums' }}>
      {num}<span style={{ display: 'inline-block', width: '2.6ch', marginLeft: '0.35ch', textAlign: 'left', opacity: 0.75 }}>{unit ?? ''}</span>
    </span>
  )
}
// Per-cell formatting on top of file-tree's defaults: byte columns in the
// site's units (exact count on hover), storage classes by name, long hex ids
// as a prefix (click to copy the whole), path-ish columns keeping their tail
// visible, and no native `title` on temporal cells (the formatted value is
// what anyone wants; the browser tooltip is slow).
const makeRenderCell = (fmtBytes: (b: number) => string): ParquetCellRenderer => ({ value, column, defaultNode }) => {
  const name = column.name
  if (SIZE_COLS.test(name) && (typeof value === 'number' || typeof value === 'bigint')) {
    const n = Number(value)
    return <Tooltip content={<code>{fmtN(n)} B</code>}>{byteCell(n, fmtBytes)}</Tooltip>
  }
  if (CLASS_COLS.test(name) && (typeof value === 'number' || typeof value === 'bigint' || typeof value === 'string')) {
    const label = CLASS_NAMES[String(value)]
    if (label) return <Tooltip content={<code>storage class {String(value)}</code>}><span>{label}</span></Tooltip>
  }
  if (HEX_ID_COLS.test(name) && typeof value === 'string' && value.length > HEX_SHOW + 4) {
    return <CopyName text={value}><code>{value.slice(0, HEX_SHOW)}…</code></CopyName>
  }
  if (PATH_COLS.test(name) && typeof value === 'string') {
    return <span className="cell-tail" dir="rtl"><bdi>{value}</bdi></span>
  }
  return isValidElement(defaultNode) && (defaultNode.props as { title?: string }).title != null
    ? cloneElement(defaultNode as ReactElement<{ title?: string }>, { title: undefined })
    : defaultNode
}
// file-tree's `<th>` is weight 500, which on the dark theme reads as one more row.
const headerProps = () => ({ style: { fontWeight: 650, borderBottom: '1px solid var(--ink-3)' } })
const tableOpts = { elide: { tooltip: elideTooltip }, headerProps, resizableColumns: { scope: 'schema' as const } }
const CsvViewer = makeCsvViewer(tableOpts)
const jsonRenderer = makeJsonTreeRenderer()

/** The object's bytes, by type (file-tree's `parsePath` kinds). */
function Body({ store, path }: { store: FtStore; path: string }) {
  const { fmtBytes } = useUnits()
  const ParquetViewer = useMemo(() => makeParquetViewer({ ...tableOpts, renderCell: makeRenderCell(fmtBytes) }), [fmtBytes])
  const parsed = parsePath(path)
  switch (parsed.kind) {
    case 'parquet':
      return <ParquetViewer store={store} path={path} />
    case 'text': {
      const ext = path.split('.').pop()?.toLowerCase() ?? ''
      if (ext === 'csv' || ext === 'tsv') return <CsvViewer store={store} path={path} delimiter={ext === 'tsv' ? '\t' : ','} />
      return <TextViewer store={store} path={path}
        jsonRenderer={ext === 'json' ? jsonRenderer : undefined}
        markdownRenderer={ext === 'md' || ext === 'markdown' ? renderMarkdown : undefined} />
    }
    case 'notebook':
      return <TextViewer store={store} path={path} jsonRenderer={jsonRenderer} />
    case 'image':
    case 'video':
    case 'audio':
      return <MediaViewer store={store} path={path} kind={parsed.kind} />
    case 'pdf':
      return <PdfViewer store={store} path={path} />
    default:
      return <p className="hint">No preview for this file type — download it from the link above.</p>
  }
}

function useProxyInfo(enabled: boolean) {
  const store = useStore()
  const sfetch = useStoreFetch()
  return useQuery<ProxyInfo>({
    queryKey: ['store-proxy', store.key],
    queryFn: async () => {
      const r = await sfetch('/api/store')
      if (!r.ok) throw new Error(`store: ${r.status}`)
      return r.json()
    },
    enabled,
    staleTime: Infinity,
  })
}

export default function ObjectPanel({ segs, node, onClose }: {
  /** The object's path from the store root (bucket first). */
  segs: string[]
  /** Its treemap node, when the view holds it (size, created). */
  node?: TreeNode
  onClose: () => void
}) {
  const store = useStore()
  const sfetch = useStoreFetch()
  const { fmtBytes } = useUnits()
  const ref = useRef<HTMLElement>(null)
  const isPublic = !!store.objectBases?.[segs[0]]
  const proxyQ = useProxyInfo(!isPublic)
  const src: ObjectSource | null = isPublic || proxyQ.data || proxyQ.isError ? objectSource(store, segs, proxyQ.data) : null
  const ft = useMemo((): FtStore | null =>
    !src ? null
    : src.kind === 'public' ? publicStore(src.base)
    : src.kind === 'proxy' ? HttpStore(src.api, storeQuery(store) ? { fetch: sfetch } : {})
    : null,
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [src?.kind, src && 'base' in src ? src.base : null, src && 'api' in src ? src.api : null, store, sfetch])
  const key = src?.key ?? segs.slice(1).join('/')
  const uri = store.scheme + segs.join('/')
  const shown = pathText(store.scheme, segs, store.home)
  const dl = ft?.getUrl?.(key)
  // Opening scrolls the panel into view; Escape closes it.
  useEffect(() => { ref.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [uri])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !(e.target as HTMLElement)?.closest('input, textarea, select')) onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <section className="object-panel" id="object" ref={ref} aria-label={`object ${segs[segs.length - 1]}`}>
      <header>
        <CopyName text={pathCopy(store.scheme, segs)}><b className="obj-name">{segs[segs.length - 1]}</b></CopyName>
        <span className="obj-meta">
          {node && <>{fmtBytes(node.b)}</>}
          {node?.d != null && <> · created {epochDaysToDate(node.d)}</>}
        </span>
        {dl && <a className="obj-dl" href={dl} download={segs[segs.length - 1]} target="_blank" rel="noreferrer">download</a>}
        <button type="button" className="obj-close" aria-label="Close the object (Esc)" title="Close (Esc)" onClick={onClose}>×</button>
      </header>
      <div className="obj-uri"><code>{shown}</code></div>
      {!src ? <p className="loading">resolving where this object is read from…</p>
        : src.kind === 'none' ? <p className="hint">No preview: <code>{src.bucket}</code> isn’t readable from this site — it has no public URL the browser may read here, and the site’s file proxy reads {proxyQ.data ? <code>{proxyQ.data.uri}</code> : 'another bucket'}. Its size and date are above.</p>
        : ft && <div className="obj-body"><Body store={ft} path={key} /></div>}
    </section>
  )
}
