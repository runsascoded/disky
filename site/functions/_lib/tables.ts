/**
 * Registry of D1 tables exposed through the generic /api/db surface (and
 * rendered by the FE's generic `DbTable`). Adding an editable/viewable table
 * to the admin dashboard = adding an entry here — no new endpoints or UI.
 *
 * `readScope` / `writeScope` gate access per table: 'gcs' read = visible to
 * every signed-in viewer (the shareable-RO case, e.g. the email whitelist);
 * 'admin' = staff console only; writeScope null = read-only (logs).
 */
export interface ColumnSpec {
  name: string
  /** Rendered/edited as this type. */
  type: 'text' | 'int'
  /** Inline-editable in the UI (PATCH). PKs never are — delete + re-add. */
  editable?: boolean
  /** Set server-side on insert; not accepted from the client. */
  server?: 'who' | 'now'
  /** Required on insert. */
  required?: boolean
}

export interface TableSpec {
  name: string
  /** Single-column TEXT primary key. */
  pk: string
  columns: ColumnSpec[]
  readScope: 'gcs' | 'admin'
  writeScope: 'admin' | null
  orderBy: string
  /** One-line description shown in the dashboard. */
  desc: string
  /** Normalize a value for the named column on write. */
  normalize?: (col: string, v: string) => string
  /** Per-column CHECK-style validation → error string | null. */
  validate?: (col: string, v: string) => string | null
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export const TABLES: TableSpec[] = [
  {
    name: 'allowed_emails',
    pk: 'email',
    desc: 'Sign-in allowlist: non-OA emails that may view the dashboard (Google or email-PIN). Removal takes effect on the next request.',
    columns: [
      { name: 'email', type: 'text', required: true },
      { name: 'note', type: 'text', editable: true },
      { name: 'who', type: 'text', server: 'who' },
      { name: 'ts', type: 'int', server: 'now' },
    ],
    readScope: 'gcs',
    writeScope: 'admin',
    orderBy: 'email',
    normalize: (col, v) => (col === 'email' ? v.trim().toLowerCase() : v),
    validate: (col, v) => (col === 'email' && !EMAIL_RE.test(v) ? 'not an email address' : null),
  },
  {
    name: 'user_emails',
    pk: 'email',
    desc: 'Sign-in email → canonical attribution user id (powers the "me" owner filter).',
    columns: [
      { name: 'email', type: 'text', required: true },
      { name: 'user', type: 'text', editable: true, required: true },
      { name: 'who', type: 'text', server: 'who' },
      { name: 'ts', type: 'int', server: 'now' },
    ],
    readScope: 'gcs',
    writeScope: 'admin',
    orderBy: 'user, email',
    normalize: (col, v) => (col === 'email' ? v.trim().toLowerCase() : col === 'user' ? v.trim().toLowerCase() : v),
    validate: (col, v) =>
      col === 'email' && !EMAIL_RE.test(v) ? 'not an email address'
      : col === 'user' && !/^[a-z0-9_-]+$/.test(v) ? 'user must be a canonical id (lowercase, [a-z0-9_-])'
      : null,
  },
  {
    name: 'actions',
    pk: 'id',
    desc: 'Actions ledger (append-only WAL): owner assignments. Writes go through /api/actions.',
    columns: [
      { name: 'id', type: 'int' },
      { name: 'actor', type: 'text' },
      { name: 'ts', type: 'int' },
      { name: 'scan', type: 'text' },
      { name: 'pattern', type: 'text' },
      { name: 'set_owner', type: 'int' },
      { name: 'owner', type: 'text' },
      { name: 'memo', type: 'text' },
    ],
    readScope: 'gcs',
    writeScope: null,
    orderBy: 'id DESC',
  },
  {
    name: 'claims',
    pk: 'prefix',
    desc: 'Legacy (pre-ledger) claims table — migrated into `actions` (owner axis); read-only history.',
    columns: [
      { name: 'prefix', type: 'text' },
      { name: 'who', type: 'text' },
      { name: 'ts', type: 'int' },
    ],
    readScope: 'admin',
    writeScope: null,
    orderBy: 'prefix',
  },
  {
    name: 'deletion_runs',
    pk: 'run_id',
    desc: 'Executed deletion runs (dry + real): plan, actor, totals, expected-vs-actual counters, undo window, log dir. Written by `dt-cloud sweep execute`.',
    columns: [
      { name: 'run_id', type: 'text' },
      { name: 'plan', type: 'text' },
      { name: 'scan', type: 'text' },
      { name: 'head', type: 'int' },
      { name: 'exec_head', type: 'int' },
      { name: 'actor', type: 'text' },
      { name: 'mode', type: 'text' },
      { name: 'started_ts', type: 'int' },
      { name: 'finished_ts', type: 'int' },
      { name: 'deleted_bytes', type: 'int' },
      { name: 'deleted_objects', type: 'int' },
      { name: 'skipped_gone', type: 'int' },
      { name: 'skipped_overwritten', type: 'int' },
      { name: 'drift_dirs', type: 'int' },
      { name: 'ledger_drift_dirs', type: 'int' },
      { name: 'undo_deadline', type: 'int' },
      { name: 'undo_state', type: 'text' },
      { name: 'log_dir', type: 'text' },
      { name: 'buckets', type: 'text' },
    ],
    readScope: 'gcs',
    writeScope: null,
    orderBy: 'started_ts DESC',
  },
  {
    name: 'deletion_bands',
    pk: 'run_id',
    desc: 'Per-band breakdown of each deletion run — the per-path deletion-history unit.',
    columns: [
      { name: 'run_id', type: 'text' },
      { name: 'prefix', type: 'text' },
      { name: 'bytes', type: 'int' },
      { name: 'objects', type: 'int' },
      { name: 'gone', type: 'int' },
      { name: 'overwritten', type: 'int' },
      { name: 'drift_new_objects', type: 'int' },
      { name: 'undone_objects', type: 'int' },
    ],
    readScope: 'gcs',
    writeScope: null,
    orderBy: 'run_id DESC, bytes DESC',
  },
  {
    name: 'admin_emails',
    pk: 'email',
    desc: 'Admin allowlist: non-staff emails that may assign owners and dispatch deletes (`admin` scope on top of viewing). Takes effect on the next request.',
    columns: [
      { name: 'email', type: 'text', required: true },
      { name: 'note', type: 'text', editable: true },
      { name: 'who', type: 'text', server: 'who' },
      { name: 'ts', type: 'int', server: 'now' },
    ],
    readScope: 'admin',
    writeScope: 'admin',
    orderBy: 'email',
    normalize: (col, v) => (col === 'email' ? v.trim().toLowerCase() : v),
    validate: (col, v) => (col === 'email' && !EMAIL_RE.test(v) ? 'not an email address' : null),
  },
  {
    name: 'admin_edits',
    pk: 'id',
    desc: 'Append-only history of every admin table edit.',
    columns: [
      { name: 'id', type: 'int' },
      { name: 'tbl', type: 'text' },
      { name: 'pk', type: 'text' },
      { name: 'action', type: 'text' },
      { name: 'who', type: 'text' },
      { name: 'ts', type: 'int' },
      { name: 'old_json', type: 'text' },
      { name: 'new_json', type: 'text' },
    ],
    readScope: 'admin',
    writeScope: null,
    orderBy: 'id DESC',
  },
]

export const tableSpec = (name: string): TableSpec | undefined => TABLES.find(t => t.name === name)
