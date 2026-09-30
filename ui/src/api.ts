// Default max rows for treemap performance (keep in sync with server DEFAULT_MAX_ROWS)
export const DEFAULT_MAX_ROWS = 2000

export type Scan = {
  id: number
  path: string
  time: string
  blob: string
  error_count?: number | null
  error_paths?: string | null  // JSON array
  size?: number | null
  n_children?: number | null
  n_desc?: number | null
}

export type Row = {
  path: string
  size: number | null
  mtime: number | null
  /** Size-weighted mean mtime (epoch s) — present when the scan was indexed with `--mean-mtime`. */
  mtime_mean?: number | null
  kind: 'file' | 'dir'
  parent: string | null
  uri: string
  n_desc: number | null
  n_children: number | null
  scanned?: boolean | 'partial'
  scan_time?: string
  expand_preview?: string  // Preview of auto-expand path (e.g., "0" for vms that expands to vms/0)
}

// Row with extra fields for collapsed (auto-expanded) parents
export type CollapsedRow = Row & {
  original_path: string  // Path relative to the requested URI
}

export type ScanDetails = {
  root: Row
  children: Row[]
  rows: Row[]
  time: string | null
  scan_path: string | null
  scan_status: 'full' | 'partial' | 'none'
  error_count?: number | null
  error_paths?: string | null  // JSON array
  collapsed_rows?: CollapsedRow[] | null  // Auto-expanded single-child directories
}

export async function fetchScans(): Promise<Scan[]> {
  const res = await fetch('/api/scans')
  if (!res.ok) throw new Error('Failed to fetch scans')
  return res.json()
}

export type HistogramChild = {
  path: string
  kind: 'file' | 'dir'
  /** Bytes per bin; length === edges.length - 1. */
  bytes: number[]
  total_bytes: number
  n_files: number
}

export type Histogram = {
  uri: string
  scan_path: string
  time: string
  /** Shared bin edges, epoch seconds ascending. */
  edges: number[]
  children: HistogramChild[]
  omitted: number
  omitted_bytes: number
}

export async function fetchHistogram(uri: string, bins: number = 24, limit: number = 20, scanId?: number): Promise<Histogram> {
  const params = new URLSearchParams({ uri, bins: String(bins), limit: String(limit) })
  if (scanId !== undefined) {
    params.set('scan_id', String(scanId))
  }
  const res = await fetch(`/api/histogram?${params}`)
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to fetch histogram')
  }
  return res.json()
}

export async function fetchScanDetails(uri: string, scanId?: number, depth: number = 2, maxRows: number = DEFAULT_MAX_ROWS): Promise<ScanDetails> {
  const params = new URLSearchParams({ uri, depth: String(depth), max_rows: String(maxRows) })
  if (scanId !== undefined) {
    params.set('scan_id', String(scanId))
  }
  const res = await fetch(`/api/scan?${params}`)
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to fetch scan details')
  }
  return res.json()
}

export type ScanJob = {
  job_id: string
  path: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  started: string
  finished?: string
  output?: string
  error?: string
}

export async function startScan(path: string): Promise<ScanJob> {
  const res = await fetch('/api/scan/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to start scan')
  }
  return res.json()
}

export async function fetchScanStatus(jobId: string): Promise<ScanJob> {
  const res = await fetch(`/api/scan/status/${jobId}`)
  if (!res.ok) throw new Error('Failed to fetch scan status')
  return res.json()
}

export async function fetchRunningScans(): Promise<ScanJob[]> {
  const res = await fetch('/api/scans/running')
  if (!res.ok) throw new Error('Failed to fetch running scans')
  return res.json()
}

export type DeleteResult = {
  success: boolean
  path: string
  deleted_size: number
  deleted_n_desc: number
}

export async function deletePath(path: string): Promise<DeleteResult> {
  const res = await fetch('/api/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to delete')
  }
  return res.json()
}

// ---- staged delete (spec `specs/staged-delete.md`) ------------------------

/** One staged URI. `bytes`/`objects` come from the freshest covering scan on
 *  the Flask peer (what a dispatch reports); the edge has no index to size
 *  against and sends `null`. */
/** A staged URI, sized from its freshest covering scan (`null`s on the edge,
 *  which has no scans to size from — and no `kind`). */
export type StagedItem = { uri: string; bytes: number | null; objects: number | null; kind: 'file' | 'dir' | null }

/** A staged set (open plan) with its URIs, as `GET /api/staged` returns it. */
export type StagedPlan = {
  id: number
  name: string
  state: 'open' | 'closed'
  created_by: string
  created_ts: number
  items: StagedItem[]
}

/** A recorded run. `finished_ts == null` = enqueued, awaiting the executor. */
export type StagedRun = {
  run_id: string
  plan_id: number
  mode: string
  actor: string
  started_ts: number
  finished_ts: number | null
  deleted_bytes: number
  deleted_objects: number
}

export type StagedState = { plans: StagedPlan[]; runs: StagedRun[] }

async function postJson<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.error || fallback)
  }
  return res.json()
}

export async function fetchStaged(): Promise<StagedState> {
  const res = await fetch('/api/staged')
  if (!res.ok) throw new Error('Failed to fetch staged set')
  return res.json()
}

/** Stage URIs into the shared open plan (spec `specs/staged-delete.md`). */
export const stageUris = (uris: string[], note?: string): Promise<{ plan_id: number; added: string[] }> =>
  postJson('/api/plans/stage', note ? { uris, note } : { uris }, 'Failed to stage')

export const unstageUris = (uris: string[]): Promise<{ removed: number }> =>
  postJson('/api/plans/unstage', { uris }, 'Failed to unstage')

export type DispatchResult = {
  run_id: string
  plan_id: number
  mode: string
  items: number
  /** Scope of the run (dry or real), from its bands — the Flask peer only. */
  bytes?: number
  objects?: number
  deleted_bytes?: number
  deleted_objects?: number
  state: 'done' | 'dry' | 'enqueued'
}

/** Dispatch a plan (default the open `Staged`): admin only. The edge enqueues +
 *  closes it; the Flask peer deletes inline. `uris` dispatches just those staged
 *  items (they leave the plan, which stays open while anything remains);
 *  `forReal: false` is a dry run (report only) — both Flask-only. */
export const dispatchPlan = (opts: { plan?: string; uris?: string[]; forReal?: boolean } = {}): Promise<DispatchResult> =>
  postJson('/api/dispatch', {
    ...(opts.plan ? { plan: opts.plan } : {}),
    ...(opts.uris ? { uris: opts.uris } : {}),
    ...(opts.forReal === false ? { for_real: false } : {}),
  }, 'Failed to dispatch')

export async function revealPath(path: string): Promise<void> {
  const res = await fetch('/api/reveal', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to reveal')
  }
}

export type ScanProgress = {
  id: number
  path: string
  pid: number
  started: string
  items_found: number
  items_per_sec: number | null
  error_count: number
  status: 'running' | 'completed' | 'failed'
}

export async function fetchScansProgress(): Promise<ScanProgress[]> {
  const res = await fetch('/api/scans/progress')
  if (!res.ok) throw new Error('Failed to fetch scans progress')
  return res.json()
}

export type S3Bucket = {
  name: string
  created: string
  last_scanned: string | null
  size?: number | null
  n_children?: number | null
  n_desc?: number | null
}

export async function fetchS3Buckets(): Promise<S3Bucket[]> {
  const res = await fetch('/api/s3/buckets')
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to fetch S3 buckets')
  }
  return res.json()
}

// Scan history for compare page
export type ScanHistoryItem = {
  id: number
  path: string
  time: string
  size?: number | null
  n_children?: number | null
  n_desc?: number | null
  scan_path?: string  // The actual scanned path (may be ancestor of requested path)
}

export async function fetchScanHistory(uri: string): Promise<ScanHistoryItem[]> {
  const res = await fetch(`/api/scans/history?uri=${encodeURIComponent(uri)}`)
  if (!res.ok) throw new Error('Failed to fetch scan history')
  return res.json()
}

// Compare scans
export type CompareRow = {
  path: string
  size: number | null
  mtime: number | null
  kind: 'file' | 'dir'
  parent: string | null
  uri: string
  n_desc: number | null
  n_children: number | null
  /** `touched`: same size & count, mtime differs (rename / net-zero churn / `touch`) */
  status: 'added' | 'removed' | 'changed' | 'touched' | 'unchanged'
  size_delta: number
  size_old?: number
  n_desc_delta?: number
  n_desc_old?: number
}

export type CompareResult = {
  uri: string
  scan1: { id: number; time: string; size: number | null; n_desc: number | null; scan_path?: string }
  scan2: { id: number; time: string; size: number | null; n_desc: number | null; scan_path?: string }
  rows: CompareRow[]
  summary: {
    added: number
    removed: number
    changed: number
    unchanged: number
    total_delta: number
  }
}

export async function compareScans(
  uri: string,
  scan1: number,
  scan2: number,
): Promise<CompareResult> {
  const params = new URLSearchParams({ uri, scan1: String(scan1), scan2: String(scan2) })
  const res = await fetch(`/api/compare?${params}`)
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to compare scans')
  }
  return res.json()
}

// File preview
export type FilePreview = {
  path: string
  size: number
  truncated: boolean
  hex_truncated: boolean
  preview_bytes: number
  is_text: boolean
  content: string | null
  hex: string | null
  extension: string
}

export async function fetchFilePreview(path: string, maxSize?: number): Promise<FilePreview> {
  const params = new URLSearchParams({ path })
  if (maxSize !== undefined) {
    params.set('max_size', String(maxSize))
  }
  const res = await fetch(`/api/file/preview?${params}`)
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to fetch file preview')
  }
  return res.json()
}

// Storage backend info
export type BackendInfo = {
  name: string
  supports_updates: boolean
  stats?: {
    num_scans?: number
    total_rows?: number
    total_size_tracked?: number
    db_file_size?: number
  }
}

export type AvailableBackend = {
  name: string
  description: string
  supports_updates: boolean
  current: boolean
}

export type AvailableBackendsResponse = {
  backends: AvailableBackend[]
  current: string
  switch_instructions: string
}

export type LibraryEntry = {
  path: string
  label: string
  opened_at?: number
}

export type CurrentLibrary = LibraryEntry & {
  exists: boolean
  scans: number
}

export type LibraryInfo = {
  current: CurrentLibrary
  recents: LibraryEntry[]
}

export async function fetchLibrary(): Promise<LibraryInfo> {
  const res = await fetch('/api/library')
  if (!res.ok) throw new Error('Failed to fetch library')
  return res.json()
}

export async function openLibrary(path: string): Promise<LibraryInfo> {
  const res = await fetch('/api/library/open', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data?.error || 'Failed to open library')
  return data
}

/** Native folder picker (pywebview app only). Returns the chosen absolute path,
 * null if cancelled, or undefined when no native picker is available (browser). */
export async function pickLibraryDir(): Promise<string | null | undefined> {
  const res = await fetch('/api/library/pick', { method: 'POST' })
  if (res.status === 501) return undefined
  if (!res.ok) throw new Error('Folder picker failed')
  const data = await res.json()
  return data.picked ?? null
}

export async function fetchBackendInfo(): Promise<BackendInfo> {
  const res = await fetch('/api/backend')
  if (!res.ok) throw new Error('Failed to fetch backend info')
  return res.json()
}

export async function fetchAvailableBackends(): Promise<AvailableBackendsResponse> {
  const res = await fetch('/api/backend/available')
  if (!res.ok) throw new Error('Failed to fetch available backends')
  return res.json()
}

export type FilterRow = {
  path: string
  uri: string
  depth: number
  kind: string
  /** Matched bytes at-or-under this node — true re-aggregation. */
  size: number
  /** Outermost matches at-or-under this node. */
  n_matches: number
  /** This node itself is an outermost match. */
  matched: boolean
}

export type FilterResult = {
  uri: string
  scan_path: string
  time: string
  query: string
  total_size: number
  n_matches: number
  max_depth_scanned: number
  /** True when the vocab sidecar answered the query (segment-local + fresh index). */
  indexed?: boolean
  rows: FilterRow[]
}

/** Recursive server-side filter (`/api/filter`): sizes count matched bytes
 * only, outermost matches — never double-counted. */
export async function fetchFilter(uri: string, q: string, scanId?: number, depth: number = 4): Promise<FilterResult> {
  const params = new URLSearchParams({ uri, q, depth: String(depth) })
  if (scanId !== undefined) {
    params.set('scan_id', String(scanId))
  }
  const res = await fetch(`/api/filter?${params}`)
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to fetch filter results')
  }
  return res.json()
}

export type CompareRecRow = {
  path: string
  uri: string
  depth: number
  kind: 'file' | 'dir'
  status: 'added' | 'removed' | 'changed' | 'touched' | 'unchanged'
  size_a: number
  size_b: number
  size_delta: number
  n_desc_a: number
  n_desc_b: number
  n_desc_delta: number
  /** This dir's children were loaded and merged into the frontier. */
  expanded: boolean
  /** Stats differ below but the walk stopped here (budget/depth). */
  pruned: boolean
}

export type CompareRecResult = {
  uri: string
  recursive: true
  scan1: CompareResult['scan1']
  scan2: CompareResult['scan2']
  rows: CompareRecRow[]
  /** Labeled grey context: per expanded dir, its biggest unchanged children
   * (`top`) and the aggregate of the rest (`rest`, keyed by parent path —
   * `''` = the compared uri). */
  unchanged?: {
    top: CompareRecRow[]
    rest: Record<string, { count: number; size: number; n_desc: number }>
  }
  summary: CompareResult['summary'] & { expansions: number; truncated: boolean }
  /** Persisted full-diff index for the pair: `done` = this response is a
   * slice of it (complete; any subpath is instant); `building` = the walk
   * answered, refetch when it lands. */
  index?: DiffIndexStatus
}

export type DiffIndexStatus = {
  status: 'none' | 'building' | 'done' | 'failed'
  time?: string
  n_rows?: number
  n_added?: number
  n_removed?: number
  n_changed?: number
  n_touched?: number
  seconds?: number
  error?: string
}

export async function fetchDiffIndexStatus(scan1: number, scan2: number): Promise<DiffIndexStatus> {
  const res = await fetch(`/api/diff/status?scan1=${scan1}&scan2=${scan2}`)
  if (!res.ok) throw new Error('Failed to fetch diff index status')
  return res.json()
}

/** Best-first pruned recursive diff (`/api/compare?recursive=1`): the delta
 * frontier across depths — added/removed dirs are not descended, stats-equal
 * dirs are pruned. */
export async function compareScansRecursive(
  uri: string,
  scan1: number,
  scan2: number,
  budget: number = 200,
  /** Drop rows whose bytes and |Δ| are both under this fraction of the
   * compared subtree — pass the canvas's drawable floor (`min_px / canvas_px`)
   * so the response carries what can actually be rendered. */
  minFrac?: number,
): Promise<CompareRecResult> {
  const params = new URLSearchParams({
    uri, scan1: String(scan1), scan2: String(scan2), recursive: '1', budget: String(budget),
  })
  if (minFrac !== undefined) params.set('min_frac', String(minFrac))
  const res = await fetch(`/api/compare?${params}`)
  if (!res.ok) {
    const err = await res.json()
    throw new Error(err.error || 'Failed to compare scans')
  }
  return res.json()
}

/** What this server can do — `GET /api/capabilities`. Keep in sync with
 *  `server.py` (everything on) and `functions/api/capabilities.ts` (static). */
export type Capabilities = {
  /** A static deployment: scans served from an object store, no live Python. */
  static: boolean
  scan: boolean
  delete: boolean
  reveal: boolean
  histogram: boolean
  filter: boolean
  preview: boolean
  compare: boolean
  /** Live scan progress (SSE). */
  progress: boolean
  library: boolean
  backend: boolean
  s3: boolean
  /** Stage paths for deletion into a plan an admin dispatches (spec
   *  `specs/staged-delete.md`), rather than deleting immediately (`delete`).
   *  On where the deployment is gated (needs an identity to attribute to). */
  stageDelete: boolean
  /** The deployment's delete-approval policy (spec `specs/staged-delete.md` CP6):
   *  `sync` deletes inline, `staged` always queues, `user-choice` lets each user
   *  pick (sticky) — the deployment default + lock. A credentialless deployment
   *  (edge) is `staged`; a credentialed one (laptop) defaults `sync`. */
  deleteApproval: 'sync' | 'staged' | 'user-choice'
  /** Browse the host's local filesystem (`/file/*`). Off on a static deployment,
   *  which has no live scanner — the Local nav item hides. */
  filesystem: boolean
  /** `/api/*` needs a session (`/api/auth/whoami`); the UI shows a sign-in wall otherwise. */
  auth: boolean
}

export const ALL_CAPABILITIES: Capabilities = {
  static: false, scan: true, delete: true, reveal: true, histogram: true, filter: true,
  preview: true, compare: true, progress: true, library: true, backend: true, s3: true,
  stageDelete: true, deleteApproval: 'sync', filesystem: true, auth: false,
}

/** A server without the endpoint (an older Flask) can do everything. */
export async function fetchCapabilities(): Promise<Capabilities> {
  const res = await fetch('/api/capabilities')
  if (res.status === 404) return ALL_CAPABILITIES
  if (!res.ok) throw new Error('Failed to fetch capabilities')
  return { ...ALL_CAPABILITIES, ...(await res.json()) }
}
