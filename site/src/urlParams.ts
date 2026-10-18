// The site's URL params, described once (use-prms `describeParams`): the "URL params" dialog (`UrlParamsModal`)
// shows each beside the hooks a page mounts, and `urlParams.test.ts` fails on a hook key missing here.
import { describeParams } from 'use-prms'

export interface UrlParamDoc {
  label: string
  description: string
  examples?: string[]
  /** The routes that read it, when not the map. */
  pages?: string[]
  /** A legacy spelling rewritten on load, or a dev fixture's: listed only when the URL carries it. */
  hidden?: boolean
}

export const URL_PARAMS: Record<string, UrlParamDoc> = {
  ac: {
    label: 'Age chart color',
    description: 'Colors the age chart by its own axis, using the same one-letter tokens as `c` (t tree, w written, r read, u user). When absent, the age chart follows the map\'s color.',
    examples: ['t', 'w', 'r', 'u'],
  },
  af: {
    label: 'Artifact folder',
    description: 'In a run\'s details, the folder of decision files being browsed, relative to the run\'s log directory (paths containing `..` are ignored). When absent, the table lists the per-bucket log folders.',
    pages: ['/staged', '/runs'],
  },
  after: {
    hidden: true,
    label: 'Fixture compare to',
    description: 'Development fixture only: the UTC instant the federated-scans preview compares to. Defaults to the fixture\'s built-in instant, and an invalid value shows an error.',
    pages: ['/dev/federated-scans'],
  },
  area: {
    label: 'Deletion map area',
    description: 'On a run\'s bucket-completion treemap, sizes cells by `bytes` or by `objects`. Defaults to bytes.',
    examples: ['bytes', 'objects'],
    pages: ['/staged', '/runs'],
  },
  at: {
    label: 'As-of instant',
    description: 'On the map of a union view, sets the cutoff instant: the page shows each source\'s newest scan at or before it. It is written as compact UTC (`20261013T1935Z`; ISO is also accepted), and replaces an end scan in `d`. On the federated-scans dev fixture it sets the fixture\'s "as of" instant instead. When absent, the page uses `d`.',
    examples: ['20261013T1935Z'],
  },
  atf: {
    label: 'Diff start instant',
    description: 'On the map of a union view, sets the diff\'s start as a cutoff instant (compact UTC, like `at`) when no single scan id names it. Can\'t be combined with a start scan or span in `d`. When absent, the diff starts at the previous scan.',
    examples: ['20261013T1935Z'],
  },
  avg: {
    label: 'Rate average',
    description: 'On a run\'s progress chart, smooths the rate metrics with a trailing average of this many seconds (capped at a day). The menu offers 300, 900 and 3600. Defaults to 0, which shows raw interval rates.',
    examples: ['300', '900', '3600'],
    pages: ['/staged', '/runs'],
  },
  axis: {
    label: 'Right axis',
    description: 'On a run\'s progress chart, adds a second metric on the right axis: `rate`, `objects`, `bytes` or `byteRate`. Defaults to `none`, and a value equal to the left metric is ignored.',
    examples: ['none', 'objects', 'bytes', 'byteRate'],
    pages: ['/staged', '/runs'],
  },
  before: {
    hidden: true,
    label: 'Fixture compare from',
    description: 'Development fixture only: the UTC instant the federated-scans preview compares from. Defaults to the fixture\'s built-in instant.',
    pages: ['/dev/federated-scans'],
  },
  bucket: {
    label: 'Progress bucket',
    description: 'On a run\'s progress chart, limits the plotted samples (and the target line) to one bucket. When absent, all buckets are shown.',
    pages: ['/staged', '/runs'],
  },
  by: {
    label: 'Assigner',
    description: 'With a person picked in `o`, counts only the assignments this assigner made (the lens behind the assignments heatmap\'s cells). It is ignored without a person in `o`, and on deployments without ownership.',
  },
  c: {
    label: 'Map color',
    description: 'Colors the treemap by `t` (tree), `w` (when written), `r` (last read) or `u` (owner). Older spellings (`tree`, `written`, `age`, `date`, `read`, `user`) are rewritten to the letter on load. When absent, the deployment\'s default applies (owner on stores with ownership, else tree), and an axis the scan can\'t support falls back.',
    examples: ['t', 'w', 'r', 'u'],
  },
  cl: {
    label: 'Storage classes',
    description: 'Limits the view\'s bytes to the listed storage classes, one letter each, in order: `s` Standard, `n` Nearline, `c` Coldline, `a` Archive. When absent (or when all four are listed), every class is counted.',
    examples: ['s', 'nc', 'sna'],
  },
  d: {
    label: 'Scan',
    description: 'Picks the scan the page shows, and optionally the diff\'s start. A slug is a compact `YYMMDD[HH[MM]]` prefix that resolves to the latest matching scan. `END-START` pins both ends, a trailing `-7d`/`-12h` sets a look-back span, and a leading `-` keeps the end on the latest scan. When absent, the page follows the latest scan and diffs against the previous one. The map and `/names` share it, and a slug that matches no scan shows a "no scan matches" notice instead of falling back.',
    examples: ['261009', '26100912', '2610091200', '-7d', '2609040002-7d', '-2609010002', '2609040002-2609010002'],
  },
  date: {
    hidden: true,
    label: 'Scan (legacy)',
    description: 'Legacy spelling of the scan selection: an ISO or compact scan id for the shown scan. It is folded into `d` and rewritten on load.',
    examples: ['2026-10-06'],
  },
  denied: {
    label: 'Denied address',
    description: 'Set when a verified address isn\'t on the access list. The login wall then opens the request-access form, pre-filled with that address.',
  },
  dm: {
    label: 'Diff area mode',
    description: '`max` sizes the diff treemap\'s cells by the larger of the old and new sizes. When absent, cells are sized by the size change (|Δ|), falling back to `max` when nothing moved.',
    examples: ['max'],
  },
  err: {
    label: 'Errors only',
    description: '`1` lists only sessions that logged errors. When absent, all sessions are listed.',
    examples: ['1'],
    pages: ['/admin/sessions'],
  },
  error: {
    label: 'Sign-in error',
    description: 'A code the sign-in endpoints attach after a failed sign-in (for example `link`, `app-link:…`, `google:…`). The sign-in page shows a matching message.',
    examples: ['link', 'app-link:not-allowed'],
    pages: ['/signin'],
  },
  ex: {
    label: 'Peak callouts',
    description: '`0` turns off the size-over-time chart\'s labels for local peaks and dips. When absent, they are on.',
    examples: ['0'],
  },
  exp: {
    label: 'Callout floor',
    description: 'The smallest prominence a peak or dip needs to get a label on the size-over-time chart, as a percent of the trace\'s range. Defaults to 10.',
    examples: ['5', '20'],
  },
  exr: {
    label: 'Callout radius',
    description: 'How far apart, in days, labeled peaks and dips on the size-over-time chart must be (a weaker one within this distance is suppressed). Defaults to 3.',
    examples: ['1', '7'],
  },
  f: {
    label: 'Name filter',
    description: 'Filters the map, its tables, charts and diff to paths matching this query, applied on the server. It is parsed with the syntax in `qs`, and a query that doesn\'t parse shows an error and isn\'t applied. On a deployment that serves search only from its index, a filtered view without `d` opens on the newest indexed scan. When absent, nothing is filtered.',
  },
  fit: {
    label: 'Fit y-axis',
    description: 'When present (as a bare `?fit`), the size-over-time chart\'s y-axis starts at the data\'s minimum instead of zero.',
    examples: [''],
  },
  flat: {
    label: 'Flat list',
    description: 'When present (as a bare `?flat`), shows staged prefixes in one table instead of one per staging batch. A filter in `q` implies it.',
    examples: [''],
    pages: ['/staged'],
  },
  from: {
    hidden: true,
    label: 'Diff start (legacy)',
    description: 'Legacy spelling of a pinned diff start (a scan id, as once used on `/names`). It is folded into `d` and rewritten on load.',
  },
  iec: {
    label: 'Binary units',
    description: 'When present (as a bare `?iec`), shows sizes in binary units (KiB, MiB, GiB). Picking units also saves the choice in this browser. When absent, the saved choice applies, else decimal units.',
    examples: [''],
  },
  key: {
    label: 'Share key',
    description: 'A share-link credential. On load it is exchanged for a session and removed from the address bar, so it never carries over into later navigation or re-shares.',
  },
  l: {
    hidden: true,
    label: 'Lens (legacy)',
    description: 'Legacy review lens. It is rewritten on load to the owner axis: `user`/`mine` becomes `o=<person>` or `o=me` (with `lu`), `unclaimed`/`communal` becomes bare `o`, and `todo` becomes `k=u`.',
    examples: ['user', 'unclaimed', 'todo'],
  },
  li: {
    label: 'Legend columns',
    description: 'Which figures each treemap legend row shows, one letter each: `s` size, `p` percent of the view, `c` estimated monthly cost. Defaults to `s`.',
    examples: ['s', 'sp', 'spc'],
  },
  ln: {
    label: 'Line layout',
    description: 'When present (as a bare `?ln`), draws the size-over-time chart as separate lines instead of stacked areas.',
    examples: [''],
  },
  lu: {
    hidden: true,
    label: 'Lens user (legacy)',
    description: 'Legacy companion to `l=user`, naming the person. It is rewritten to `o=<key>` on load.',
  },
  mapColor: {
    label: 'Deletion map color',
    description: 'Colors a run\'s bucket-completion treemap by `prefix`, `bucket` or `completion`. Defaults to `prefix`.',
    examples: ['prefix', 'bucket', 'completion'],
    pages: ['/staged', '/runs'],
  },
  metric: {
    label: 'Left axis',
    description: 'The main metric on a run\'s progress chart: `rate` (objects/s), `objects`, `bytes` or `byteRate` (bytes/s). Defaults to `rate`.',
    examples: ['rate', 'objects', 'bytes', 'byteRate'],
    pages: ['/staged', '/runs'],
  },
  mt: {
    hidden: true,
    label: 'Lens (legacy)',
    description: 'An older spelling of `l`: `mine` becomes `o=me` (or `o=<key>` with `mu`) on load.',
    examples: ['mine'],
  },
  mu: {
    hidden: true,
    label: 'Lens user (legacy)',
    description: 'An older spelling of `lu`, rewritten to `o=<key>` on load.',
  },
  n: {
    label: 'Rows per page',
    description: 'Page size for both the children table and the diff table on the map: 20, 50, 100 or 200. Defaults to 20, and any other value reads as 20.',
    examples: ['50', '100', '200'],
  },
  name: {
    label: 'Name literal',
    description: 'The name to summarize: a case-insensitive literal substring of any one path component. Defaults to `datakit`.',
    examples: ['datakit', '.json'],
    pages: ['/names'],
  },
  next: {
    label: 'Return path',
    description: 'Where to return after signing in. Only same-origin paths are accepted; anything else returns to `/`.',
    pages: ['/signin'],
  },
  o: {
    label: 'Owner',
    description: 'Narrows the map to one owner pool or person: bare `o` is unowned bytes, `o=*` owned bytes, `o=me` your own, `o=<key>` one person\'s, and `o=!<key>,<key>` bytes owned by anyone other than those people. The older spellings `unowned`, `owned`, `unclaimed` and `claimed` are rewritten. It applies only on deployments with ownership, and when absent, everything is shown.',
    examples: ['', '*', 'me', '!rw'],
  },
  og: {
    label: 'Preview token',
    description: 'A per-view token that lets link unfurlers show a detailed preview card. It grants no access, and is removed from the address bar on load so a re-share doesn\'t carry it.',
  },
  open: {
    label: 'Open object',
    description: 'Opens the named object (a child of the current directory) in the viewer below the map. Opening adds a history entry, so Back closes it, and drilling to another directory clears it.',
  },
  order: {
    label: 'Runs sort order',
    description: 'Sort direction for the runs table, `asc` or `desc`. Defaults to `desc`.',
    examples: ['asc', 'desc'],
    pages: ['/staged'],
  },
  outcome: {
    hidden: true,
    label: 'Simulated outcome',
    description: 'Development fixture only: the simulated staging response for the children-table preview. Defaults to `success`.',
    examples: ['success'],
    pages: ['/dev/children-table'],
  },
  p: {
    label: 'Drilled path',
    description: 'On a person\'s page, the folder their map is drilled into, so a deep view can be shared and Back walks out. When absent, the map shows their whole estate.',
    pages: ['/user'],
  },
  path: {
    hidden: true,
    label: 'Path (legacy)',
    description: 'A legacy link\'s drill path, as a query parameter. It is converted to the URL-path form on load, keeping the other parameters.',
  },
  perf: {
    label: 'Perf panel',
    description: '`1` at load shows the time-to-render panel. The timing marks themselves are always recorded.',
    examples: ['1'],
  },
  ps: {
    label: 'Path store',
    description: 'Picks which path store the page\'s data comes from: it is read once at load and forwarded on every same-origin `/api/` request. `iv` reads the interval store on a deployment where that is opt-in, and `scan` reads the per-scan sorts on one that serves the interval store (the parity check). It has no effect on requests to a secondary store. When absent, the deployment\'s default store is used.',
    examples: ['iv', 'scan'],
  },
  q: {
    label: 'Staged filter',
    description: 'Filters staged prefixes by substring. Terms can target a field (`owner:<who>`, `staged-by:<who>`), and `|` separates alternatives. Setting it also shows one flat table. When absent, all staged prefixes are listed.',
    examples: ['hedy|grace', 'staged-by:barbara'],
    pages: ['/staged'],
  },
  qs: {
    label: 'Query syntax',
    description: 'The syntax `f` is parsed with: `simple` (terms, with `*` wildcards and exclusions) or `regex` (one case-insensitive regex over the full path). An unknown id shows an error. When absent, the store\'s default applies, else `simple`. It is ignored on a deployment that serves search only from its index, where queries are always `plain` literals.',
    examples: ['simple', 'regex'],
  },
  range: {
    label: 'Chart window',
    description: 'The time window of a run\'s progress chart: a width (`2h`, `30m`, `1d`, or `all`), optionally after an ISO end instant and `~`. It is set by dragging, zooming or panning. Defaults to the last hour, ending now.',
    examples: ['all', '6h', '2026-10-13T19:35:00.000Z~2h'],
    pages: ['/staged', '/runs'],
  },
  rb: {
    label: 'Byte suffix',
    description: 'When present (as a bare `?rb`), writes sizes with a trailing B (`GB` instead of `G`). The choice is also saved in this browser. When absent, the saved choice applies, else no suffix.',
    examples: [''],
  },
  revoked: {
    label: 'Show revoked',
    description: '`1` includes revoked grants in the admin console\'s lists. When absent, they are hidden.',
    examples: ['1'],
    pages: ['/admin'],
  },
  rm: {
    label: 'Run mode filter',
    description: 'Shows only `real` or only `dry` runs in the runs table. When absent, both are shown.',
    examples: ['real', 'dry'],
    pages: ['/staged'],
  },
  rq: {
    label: 'Runs search',
    description: 'A case-insensitive substring match against each run\'s id, Batch job id, actor, mode, state and buckets. When absent, every run is listed.',
    pages: ['/staged'],
  },
  rs: {
    label: 'Run state filter',
    description: 'Shows only runs in this state (one of the states listed in the table\'s state menu). When absent, runs in every state are shown.',
    pages: ['/staged'],
  },
  rt: {
    label: 'Read tint',
    description: 'How the last-read color lens tints cells: `hatch` or `touch`. It is shared by the map, its tables and the age chart. Defaults to `hatch`.',
    examples: ['touch'],
  },
  ru: {
    label: 'Run user filter',
    description: 'Shows only runs started by this user. When absent, runs from every user are shown.',
    pages: ['/staged'],
  },
  run: {
    label: 'Focused run',
    description: 'A run id (or its Batch job id). The runs table pages to that row, opens its details and scrolls to it once it appears.',
    pages: ['/staged'],
  },
  s: {
    label: 'Shade / staged sort',
    description: 'On the map, `c` shades each cell by storage class within its main color (the older spelling `class` is rewritten); when absent, there is no shading. On `/staged`, it sorts the prefix tables by a column key (`name`, `b` size, `o` objects, `d` the listing-time column, `a` last read, `idle`, …), with a `-` prefix for descending; the default is `-b`.',
    examples: ['c', 'name', '-o', 'idle'],
  },
  si: {
    label: 'Decimal units',
    description: 'When present (as a bare `?si`), shows sizes in decimal units (KB, MB, GB), overriding a saved binary-units choice. Decimal is also the default.',
    examples: [''],
  },
  since: {
    label: 'Active since',
    description: 'Lists only sessions active since this date (`YYYY-MM-DD`). When absent, sessions from any date are listed.',
    examples: ['2026-10-01'],
    pages: ['/admin/sessions'],
  },
  sort: {
    label: 'Runs sort',
    description: 'The column the runs table sorts by: `started`, `by`, `mode`, `state`, `planned`, `deleted`, `bytes`, `rate`, `elapsed`, `gone`, `overwritten`, `drift`, `undo` or `buckets`. Defaults to `started`.',
    examples: ['started', 'deleted', 'rate'],
    pages: ['/staged'],
  },
  spy: {
    label: 'Render spy',
    description: '`1` installs the render spy (`window.__renderSpy`) and exposes the query cache as `window.__qc`, for debugging re-renders and refetches. Both are always on in development builds.',
    examples: ['1'],
  },
  store: {
    label: 'Scans store',
    description: 'Which configured store\'s scan runs to show, by its key. When absent, or for an unknown key, the primary store\'s runs are shown.',
    pages: ['/scans'],
  },
  t: {
    hidden: true,
    label: 'Group pin (legacy)',
    description: 'Retired group pin. On load, `unattributed` and `communal` become bare `o` (the unowned pool), and any other value is dropped.',
    examples: ['unattributed', 'communal'],
  },
  tm: {
    label: 'Table mode',
    description: 'What the children table lists: `c` the directory\'s children, `f` the largest files below it, or `d` the largest folders below it. The flat modes apply only to an unscoped view of a scan that lists objects. Defaults to children.',
    examples: ['f', 'd'],
  },
  ts: {
    label: 'Date scale',
    description: 'How dates map onto the date color gradient (on the map and its tables): `log` (age on a log scale, so recent weeks spread out), `lin` (linear in calendar time) or `rank` (by bytes, so each stretch of the gradient covers about the same area). Defaults to `log`.',
    examples: ['lin', 'rank'],
  },
  u: {
    label: 'Viewer / legend pin',
    description: 'On `/admin/sessions`, filters sessions to viewers whose address contains this text. On the map it is a legacy legend pin, rewritten to `o=<key>` on load.',
  },
  w: {
    label: 'Run window',
    description: 'Lists only the scan runs that started in this UTC window, written as `START~END` stamps (`YYYY-MM-DDTHHMM`) and set by dragging on the duration chart. Esc clears it. When absent, every run is listed.',
    examples: ['2026-10-12T0600~2026-10-13T0600'],
    pages: ['/scans'],
  },
  wall: {
    hidden: true,
    label: 'Force login wall',
    description: 'Development builds only: when present, shows the login wall instead of signing in automatically as the dev identity.',
    examples: [''],
  },
  xr: {
    label: 'Time range',
    description: 'The size-over-time chart\'s x-range: `1w` or `1m`. When absent, the chart shows all scans.',
    examples: ['1w', '1m'],
  },
  ym: {
    label: 'Y values',
    description: 'What the size-over-time chart plots: `d` the change, `p` the percent change. When absent, it plots absolute sizes.',
    examples: ['d', 'p'],
  },
}

/** Register the catalogue (once, at load). */
export const describeUrlParams = (): (() => void) =>
  describeParams(Object.fromEntries(Object.entries(URL_PARAMS).map(([k, { label, description, examples }]) => [k, { label, description, examples }])))
