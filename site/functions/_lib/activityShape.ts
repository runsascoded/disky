/**
 * The activity feed's pure parts (specs/done/activity-feed.md): event kinds and their defaults, which requests and
 * session-log events are which kind, bursts, and the Slack / Discord renderings. No Workers or DOM types; the D1 and
 * sink I/O is `activity.ts`.
 */
import { benignError, isErrorEvent, isFailedApi, type SlogEvent, summarize, TRUNC } from './sessionLogShape.js'

export const NOTIFY = ['off', 'count', 'line'] as const
export type Notify = typeof NOTIFY[number]

export interface KindDef {
  kind: string
  source: 'server' | 'client'
  /** What the kind covers (the `/admin` table). */
  covers: string
  /** Counted noun: one, many. */
  noun: [string, string]
  log: boolean
  notify: Notify
}

const def = (kind: string, source: KindDef['source'], covers: string, noun: [string, string], notify: Notify, log = true): KindDef =>
  ({ kind, source, covers, noun, log, notify })

/** Every kind, in report order, with its defaults. */
export const KIND_DEFS: KindDef[] = [
  def('assign', 'server', 'Owner assigned or cleared (a folder or an object)', ['assignment', 'assignments'], 'line'),
  def('note', 'server', 'Chart note added, edited or removed', ['note', 'notes'], 'line'),
  def('stage', 'server', 'Staged-deletion edits (each plan has its own thread)', ['staging edit', 'staging edits'], 'count'),
  def('deletion', 'server', 'Deletion runs dispatched, stopped, undone or purged', ['deletion action', 'deletion actions'], 'line'),
  def('access', 'server', 'Grants, share links, access requests, the sign-in allowlist', ['access change', 'access changes'], 'line'),
  def('admin', 'server', 'Admin console edits and site settings', ['admin edit', 'admin edits'], 'line'),
  def('token', 'server', 'Agent / CLI token minted', ['token', 'tokens'], 'line'),
  def('profile', 'server', 'Profile name or avatar', ['profile edit', 'profile edits'], 'count'),
  def('write', 'server', 'Any other change', ['other change', 'other changes'], 'count'),
  def('view', 'client', 'Page loads', ['page load', 'page loads'], 'count'),
  def('nav', 'client', 'URL changes: routes, drills, query params, the section in view', ['URL change', 'URL changes'], 'count'),
  def('click', 'client', 'Clicks on controls (buttons, links, menus, checkboxes)', ['click', 'clicks'], 'count'),
  def('filter', 'client', 'Path-filter input', ['filter', 'filters'], 'count'),
  def('pick', 'client', 'Picker choices (selects, the assign and scan pickers)', ['pick', 'picks'], 'count'),
  def('resume', 'client', 'A tab picked up again after a gap: its earlier burst (and thread) continues', ['resumption', 'resumptions'], 'line'),
  def('error', 'client', 'Errors: uncaught, rejected, console, failed requests (each a thread reply)', ['error', 'errors'], 'line'),
  def('api', 'client', 'Other requests the page made', ['request', 'requests'], 'off'),
  def('prefetch', 'client', 'Hover prefetches', ['prefetch', 'prefetches'], 'off'),
  def('viewport', 'client', 'Window size', ['resize', 'resizes'], 'off'),
  def('vis', 'client', 'Tab shown / hidden', ['visibility change', 'visibility changes'], 'off'),
]
export const KIND_BY_NAME: Record<string, KindDef> = Object.fromEntries(KIND_DEFS.map(d => [d.kind, d]))

export interface KindConf { log: boolean; notify: Notify }
export type KindsConf = Record<string, KindConf>
export interface KindRow { kind: string; log: number; notify: string }

/** The defaults, overridden by the stored rows (rows for unknown kinds, or bad `notify` values, are ignored). */
export function mergeKinds(rows: readonly KindRow[]): KindsConf {
  const out: KindsConf = Object.fromEntries(KIND_DEFS.map(d => [d.kind, { log: d.log, notify: d.notify }]))
  for (const r of rows) {
    if (!out[r.kind] || !(NOTIFY as readonly string[]).includes(r.notify)) continue
    out[r.kind] = { log: !!r.log, notify: r.notify as Notify }
  }
  return out
}

/** Writes that are reads or plumbing, never events. */
const NOT_EVENTS = [
  /^\/api\/session-log(\/sessions.*)?$/, /^\/api\/footer-cache$/, /^\/api\/digest-image$/, /^\/api\/static-bench$/,
  /^\/api\/prefixes$/, /^\/auth\//, /^\/api\/auth\/(exchange|logout|track|request|email)(\/|$)/,
  /^\/slack\//,
]

/** A successful write's kind, or null when it isn't an event. */
export function serverKind(method: string, path: string): string | null {
  if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') return null
  if (NOT_EVENTS.some(re => re.test(path))) return null
  if (path === '/api/actions') return 'assign'
  if (path === '/api/annotations') return 'note'
  if (path.startsWith('/api/plans')) return 'stage'
  if (/^\/api\/(plan-)?sweep\//.test(path)) return 'deletion'
  if (path === '/api/token') return 'token'
  if (path === '/api/auth/profile' || path === '/api/auth/avatar') return 'profile'
  if (path.startsWith('/api/auth/') || path.startsWith('/api/og/')) return 'access'
  if (path.startsWith('/api/db') || path === '/api/session-log/switch' || path.startsWith('/api/activity/')) return 'admin'
  return 'write'
}

/** A session-log event's kind, or null (`dropped` markers). */
export function clientKind(e: Pick<SlogEvent, 'k'> & { s?: unknown, err?: unknown }): string | null {
  switch (e.k) {
    case 'start': return 'view'
    case 'error': case 'reject': case 'console': return 'error'
    case 'api': return isFailedApi(e) ? 'error' : 'api'
    case 'dropped': return null
    default: return e.k
  }
}

/** The auth package's admin writes, as what they did. */
export function authLine(method: string, path: string): string {
  const seg = path.replace(/^\/api\/auth\/?/, '').split('/').filter(Boolean)
  const [what, id, verb] = seg
  if (what === 'grants') {
    if (!id) return 'minted a grant'
    if (verb) return `${verb === 'disable' ? 'disabled' : verb === 'enable' ? 'enabled' : verb === 'revoke' ? 'revoked' : verb === 'rotate' ? 'rotated' : verb} grant ${id}`
    return method === 'PATCH' ? `edited grant ${id}` : `${method} grant ${id}`
  }
  if (what === 'requests' && id && (verb === 'approve' || verb === 'deny')) return `${verb === 'approve' ? 'approved' : 'denied'} access request ${id}`
  if (what === 'decide') return 'decided an access request (emailed link)'
  if (what === 'allowed') {
    if (id === 'sync') return 'synced the sign-in allowlist'
    if (method === 'DELETE' && id) return `removed ${decodeURIComponent(id)} from the sign-in allowlist`
    return 'added to the sign-in allowlist'
  }
  if (what === 'profile' || what === 'avatar') return what === 'avatar' ? 'changed their avatar' : 'edited their profile'
  return `${method} ${path}`
}

/** A route's line when it set none of its own. */
export function genericLine(method: string, path: string): string {
  return path.startsWith('/api/auth/') ? authLine(method, path) : `${method} ${path}`
}

/** One server event, as recorded. */
export interface ServerEvent {
  ts: number
  kind: string
  actor: string
  via: string
  method: string
  route: string
  line: string
  detail?: Record<string, unknown>
}

/** A route's account of one thing a request did: its line and any structured fields (`bytes`, `owner`, …). */
export interface Detail {
  line: string
  kind?: string
  [k: string]: unknown
}

/** One actor's activity: no gap over `gapS` between events. */
export interface Burst {
  id: number
  actor: string
  via: string
  origin: string
  first_ts: number
  last_ts: number
  counts: Record<string, number>
  /** Paths viewed (`view` / `nav`), by count. */
  pages: Record<string, number>
  /** Paths viewed, in order: the latest `TRAIL_KEEP` stops. */
  trail: Trail
  sids: string[]
}

/** A burst's path trail: `n` stops in all (a repeat of the previous path is the same stop, counted), the latest
 *  `TRAIL_KEEP` kept as `[path, views]`. */
export interface Trail { n: number; last: [string, number][] }
export const TRAIL_KEEP = 10
export const emptyTrail = (): Trail => ({ n: 0, last: [] })

/** `trail` with `paths` (in order) appended: a path equal to the previous stop adds a view to it. */
export function addTrail(trail: Trail, paths: readonly string[], keep = TRAIL_KEEP): Trail {
  const last = trail.last.map(([p, n]) => [p, n] as [string, number])
  let n = trail.n
  for (const p of paths) {
    const prev = last[last.length - 1]
    if (prev && prev[0] === p) prev[1]++
    else { last.push([p, 1]); n++ }
  }
  return { n, last: last.slice(-keep) }
}

/** Whether an event at `ts` joins the actor's latest burst (else it starts a new one). */
export const joins = (latest: Pick<Burst, 'last_ts'> | null, ts: number, gapS: number): boolean =>
  !!latest && ts - latest.last_ts <= gapS && ts >= latest.last_ts - gapS

/** `pages` with `more` added, kept to the `keep` most-viewed (ties by path). */
export function addPages(pages: Record<string, number>, more: Record<string, number>, keep = 20): Record<string, number> {
  const all = { ...pages }
  for (const [p, n] of Object.entries(more)) all[p] = (all[p] ?? 0) + n
  return Object.fromEntries(Object.entries(all).sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).slice(0, keep))
}

/** One client error, as the feed records it: when (epoch s), its line, and the page it happened on. */
export interface ClientError { ts: number; line: string; page: string | null }

/** A batch's client events, per kind (the recorded kinds only), the paths viewed (by count, and in order), and its
 *  errors (when `error` is recorded). An error's page is the latest `view` / `nav` before it in the batch. */
export function tallyClient(events: readonly SlogEvent[], conf: KindsConf): {
  counts: Record<string, number>; pages: Record<string, number>; trail: string[]; errors: ClientError[]
} {
  const counts: Record<string, number> = {}
  const pages: Record<string, number> = {}
  const trail: string[] = []
  const errors: ClientError[] = []
  let page: string | null = null
  for (const e of events) {
    const k = clientKind(e)
    if ((e.k === 'start' || e.k === 'nav') && typeof e.u === 'string') page = pathOf(e.u)
    if (!k || !conf[k]?.log || (k === 'error' && benignError(e.msg))) continue
    counts[k] = (counts[k] ?? 0) + 1
    if ((k === 'view' || k === 'nav') && page !== null) {
      pages[page] = (pages[page] ?? 0) + 1
      trail.push(page)
    }
    if (k === 'error' && isErrorEvent(e)) errors.push({ ts: Math.floor(e.t / 1000), line: cutLine(summarize(e)), page })
  }
  return { counts, pages, trail, errors }
}

const cutLine = (s: string, n: number = TRUNC.msg): string => s.length > n ? `${s.slice(0, n - 1)}…` : s

/** A logged URL's path (no query or hash). */
export const pathOf = (u: string): string => {
  const p = u.replace(/^[a-z]+:\/\/[^/]+/i, '').replace(/[?#].*$/, '')
  return p || '/'
}

/** The events a batch keeps: the recorded kinds (`dropped` markers always). */
export const keptEvents = (events: readonly SlogEvent[], conf: KindsConf): SlogEvent[] =>
  events.filter(e => { const k = clientKind(e); return k === null || !!conf[k]?.log })

/** Whether any of a burst's counted kinds is reported. */
export const reported = (counts: Record<string, number>, conf: KindsConf): boolean =>
  Object.entries(counts).some(([k, n]) => n > 0 && (conf[k]?.notify ?? 'off') !== 'off')

// ---- rendering ----

export interface Fmt {
  bold: (s: string) => string
  link: (url: string, text: string) => string
  code: (s: string) => string
  /** Literal text (a client's error message), escaped for the sink. */
  esc: (s: string) => string
}
export const SLACK: Fmt = {
  bold: s => `*${s}*`,
  link: (url, text) => `<${url}|${text.replace(/[<>|]/g, '')}>`,
  code: s => `\`${s.replace(/`/g, "'")}\``,
  esc: s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
}
export const DISCORD: Fmt = {
  bold: s => `**${s}**`,
  link: (url, text) => `[${text.replace(/[[\]]/g, '')}](<${url}>)`,
  code: s => `\`${s.replace(/`/g, "'")}\``,
  esc: s => s.replace(/([*_~`|\\])/g, '\\$1'),
}

const fmtN = (n: number): string => n.toLocaleString('en-US')
const hm = (ts: number): string => new Date(ts * 1000).toISOString().slice(11, 16)

/** A gap in seconds, at most two units: `45s`, `3m 07s`, `4h 17m`, `2d 03h`. */
export function fmtGap(s: number): string {
  const t = Math.max(0, Math.round(s))
  if (t < 60) return `${t}s`
  const m = Math.floor(t / 60)
  if (m < 60) return `${m}m ${String(t % 60).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`
  return `${Math.floor(h / 24)}d ${String(h % 24).padStart(2, '0')}h`
}
const day = (ts: number): string => new Date(ts * 1000).toISOString().slice(0, 10)

/** IEC, as the staged-deletion threads in the same channel print sizes. */
export const fmtBytes = (n: number): string => {
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${i ? v.toFixed(1) : v} ${u[i]}`
}

/** How a burst names its actor: the email's local part, else the grant's name as given. */
export const actorName = (actor: string): string => actor.includes('@') ? actor.replace(/@.*$/, '') : actor

/** `n noun(s)` for a kind. */
export const countText = (kind: string, n: number): string => {
  const d = KIND_BY_NAME[kind]
  return `${fmtN(n)} ${d ? (n === 1 ? d.noun[0] : d.noun[1]) : kind}`
}

/** What the summary shows besides counts, from the burst's server events. */
export interface Highlights {
  /** The assignment with the most bytes. */
  largest?: { pattern: string; bytes: number; owner: string | null }
  /** Owners assigned to (null = cleared), in first-seen order. */
  owners?: (string | null)[]
  /** The staged plans touched. */
  plans?: number[]
}

export function highlights(events: readonly Pick<ServerEvent, 'kind' | 'detail'>[]): Highlights {
  const out: Highlights = {}
  const owners: (string | null)[] = []
  const plans: number[] = []
  for (const e of events) {
    const d = e.detail ?? {}
    if (e.kind === 'assign') {
      const owner = typeof d.owner === 'string' ? d.owner : null
      if (!owners.includes(owner)) owners.push(owner)
      if (typeof d.bytes === 'number' && typeof d.pattern === 'string' && (!out.largest || d.bytes > out.largest.bytes)) {
        out.largest = { pattern: d.pattern, bytes: d.bytes, owner }
      }
    }
    if (e.kind === 'stage' && typeof d.plan === 'number' && !plans.includes(d.plan)) plans.push(d.plan)
  }
  if (owners.length) out.owners = owners
  if (plans.length) out.plans = plans
  return out
}

export interface SummaryOpts {
  fmt: Fmt
  conf: KindsConf
  highlights: Highlights
  /** `/admin/activity?burst=<id>` etc. are built from the burst's origin. */
  topPages?: number
}

const via = (v: string): string => v === 'grant' ? 'agent token / share link' : v === 'public' ? 'signed out' : 'browser'

const spanText = (b: Pick<Burst, 'first_ts' | 'last_ts'>): string =>
  `${day(b.first_ts)} ${b.first_ts === b.last_ts ? hm(b.first_ts) : `${hm(b.first_ts)}–${hm(b.last_ts)}`}Z`
const countsText = (b: Pick<Burst, 'counts'>, conf: KindsConf): string[] =>
  KIND_DEFS.filter(d => (b.counts[d.kind] ?? 0) > 0 && conf[d.kind]?.notify !== 'off').map(d => countText(d.kind, b.counts[d.kind]))
const activityLink = (b: Pick<Burst, 'origin' | 'id'>, fmt: Fmt): string => fmt.link(`${b.origin}/admin/activity?burst=${b.id}`, 'activity')

/** The burst's summary (Discord's one message): who, how, when, the reported counts, highlights, links. */
export function renderSummary(b: Burst, o: SummaryOpts): string {
  const { fmt, conf } = o
  const counts = countsText(b, conf)
  const lines = [`${fmt.bold(actorName(b.actor))} (${via(b.via)}) · ${spanText(b)}`, ...(counts.length ? [counts.join(' · ')] : [])]
  return [...lines, ...detailLines(b, o, [activityLink(b, fmt)])].join('\n')
}

/** A burst's Slack post, one line: the counts, when, the activity link; signed in (`via` session) the post is sent
 *  as the user (`sender`), so the line leaves out who; otherwise it leads with who and how. */
export function renderHeadline(b: Burst, o: Pick<SummaryOpts, 'fmt' | 'conf'>, asUser: boolean): string {
  const { fmt, conf } = o
  const who = asUser ? [] : [`${fmt.bold(actorName(b.actor))} (${via(b.via)})`]
  return [...who, ...countsText(b, conf), spanText(b), activityLink(b, fmt)].join(' · ')
}

/** The rest of a burst's Slack post, as its first reply: highlights, pages, session / plan links. */
export const renderDetails = (b: Burst, o: SummaryOpts): string => detailLines(b, o, []).join('\n')

function detailLines(b: Burst, o: SummaryOpts, lead: string[]): string[] {
  const { fmt, conf } = o
  const lines: string[] = []
  const h = o.highlights
  if (h.largest) lines.push(`Largest assignment: ${fmt.code(h.largest.pattern)} (${fmtBytes(h.largest.bytes)}) → ${h.largest.owner ?? 'nobody'}`)
  if (h.owners && (h.owners.length > 1 || !h.largest)) lines.push(`Owners: ${h.owners.map(x => x ?? 'cleared').join(', ')}`)
  if (conf.view?.notify !== 'off') lines.push(...pageLines(b, fmt, o.topPages ?? 5))
  const links = [...lead, ...sessionLinks(b, fmt)]
  for (const p of h.plans ?? []) links.push(fmt.link(`${b.origin}/staged?plan=${p}`, `plan ${p}`))
  if (links.length) lines.push(links.join(' · '))
  return lines
}

/** Who a signed-in burst's Slack post is sent as: the profile's name, else the email's local part. */
export const senderName = (actor: string, profileName: string | null | undefined): string => profileName?.trim() || actorName(actor)

const stop = (fmt: Fmt, [p, n]: [string, number]): string => `${fmt.code(p)}${n > 1 ? ` ×${n}` : ''}`

/** Where the burst went: up to `TRAIL_KEEP` stops, in order; past that, the most-viewed `top` paths, then the last
 *  `TRAIL_KEEP` stops. A burst from before trails were kept lists its most-viewed paths. */
export function pageLines(b: Pick<Burst, 'pages' | 'trail'>, fmt: Fmt, top: number): string[] {
  const hist = (n: number) => Object.entries(b.pages).slice(0, n).map(e => stop(fmt, e)).join(', ')
  if (!b.trail.n) return Object.keys(b.pages).length ? [`Pages: ${hist(top)}`] : []
  const path = b.trail.last.map(e => stop(fmt, e)).join(' → ')
  if (b.trail.n <= b.trail.last.length) return [`Pages: ${path}`]
  return [`Pages (${fmtN(b.trail.n)} stops): ${hist(top)}`, `Last ${b.trail.last.length}: ${path}`]
}

/** Sessions linked one by one (their sid's first 8 characters) up to this many; past it, the actor's session list. */
export const MAX_SESSION_LINKS = 3

/** `session 9109d590` links, each to its timeline. */
export function sessionLinks(b: Pick<Burst, 'origin' | 'actor' | 'sids'>, fmt: Fmt, max = MAX_SESSION_LINKS): string[] {
  if (!b.sids.length) return []
  if (b.sids.length > max) return [fmt.link(`${b.origin}/admin/sessions?u=${encodeURIComponent(b.actor)}`, `${b.sids.length} sessions`)]
  return b.sids.map(sid => fmt.link(sessionUrl(b.origin, sid), `session ${sid.slice(0, 8)}`))
}

/** A session's timeline, optionally filtered (`show=errors`). */
export const sessionUrl = (origin: string, sid: string, show?: string): string =>
  `${origin}/admin/sessions/${encodeURIComponent(sid)}${show ? `?show=${show}` : ''}`

/** Lines one reply (or Discord section) lists, before `+N more`. */
export const MAX_LINES = 30

/** A `line` kind's list: a header, then one line per distinct line (oldest first; repeats folded in as `×N`, from
 *  the first's time), cut at `max`. An event's `href` (an error's session) links its time. */
export function renderLines(kind: string, events: readonly (Pick<ServerEvent, 'ts' | 'line'> & { href?: string })[], fmt: Fmt, max = MAX_LINES): string {
  const groups = new Map<string, { ts: number; href?: string; n: number }>()
  for (const e of events) {
    const g = groups.get(e.line)
    if (g) g.n++
    else groups.set(e.line, { ts: e.ts, href: e.href, n: 1 })
  }
  const all = [...groups]
  const out = [
    fmt.bold(countText(kind, events.length)),
    ...all.slice(0, max).map(([line, g]) => `• ${g.href ? fmt.link(g.href, hm(g.ts)) : hm(g.ts)} ${line}${g.n > 1 ? ` ×${fmtN(g.n)}` : ''}`),
  ]
  if (all.length > max) out.push(`+${fmtN(all.length - max)} more`)
  return out.join('\n')
}

/** Discord's message limit. */
export const DISCORD_MAX = 2000

/** One Discord message: the summary, then each `line` kind's list, cut to fit (`+N more` and the activity link
 *  stay). */
export function renderDiscord(summary: string, sections: readonly string[], activityUrl: string, max = DISCORD_MAX): string {
  let out = summary
  const tail = `\n…more in [activity](<${activityUrl}>)`
  for (const s of sections) {
    const next = `${out}\n\n${s}`
    if (next.length > max) {
      const room = max - out.length - tail.length - 2
      const cut = room > 40 ? `\n\n${s.slice(0, s.lastIndexOf('\n', room) > 0 ? s.lastIndexOf('\n', room) : room)}` : ''
      return (out + cut + tail).slice(0, max)
    }
    out = next
  }
  return out
}
