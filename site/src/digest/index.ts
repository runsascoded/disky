/** The digest OP renderer (specs/slack-preview-page.md): a month's rows + the digest config + params → the OP's text
 * and its plot's `/api/digest-image` request, for either template: the only OP renderer (the job posts the rows to
 * `/api/digest-op`; goldens: `./parity.test.ts`). Pure; DOM-free. */
import { DEFAULT_PARAMS, type DigestCfg, type DigestParams, monthOf, type OpLinks } from './common'
import * as cw from './cw'
import type { ReplyCfg } from './cwReply'
import * as gcs from './gcs'

export * from './common'
export type { CwRows, CwScan } from './cw'
export { replies } from './cwReply'
export type { RenderedReply, ReplyCfg, ReplyMarks } from './cwReply'
export type { GcsRow, GcsRows } from './gcs'

export type DigestRows = cw.CwRows | gcs.GcsRows

/** The month's OP text. */
export function opText(rows: DigestRows, month: string, cfg: DigestCfg, links: OpLinks, p: DigestParams = DEFAULT_PARAMS): string {
  return rows.template === 'cw' ? cw.opText(rows, monthOf(month), cfg, p, links) : gcs.opText(rows, monthOf(month), cfg, p, links)
}

/** The month's plot request; `today_at` only when it differs from the default (the card's own). */
export function imageRequest(rows: DigestRows, month: string, cfg: DigestCfg, p: DigestParams = DEFAULT_PARAMS): Record<string, unknown> {
  const body = rows.template === 'cw' ? cw.imageRequest(rows, monthOf(month), cfg, p) : gcs.imageRequest(rows, monthOf(month), cfg, p)
  const [p0, p1] = p.todayAt
  if (p0 !== DEFAULT_PARAMS.todayAt[0] || p1 !== DEFAULT_PARAMS.todayAt[1]) body.today_at = p.todayAt
  return body
}

/** The preview bundle a converge writes (`digest.preview_bundle`). */
export interface PreviewBundle {
  version: 1
  month: string
  channel: string
  variant: string
  posted_at: string
  op_ts: string | null
  permalink: string | null
  plot_url: string | null
  prev_url: string | null
  cfg: DigestCfg
  rows: DigestRows
  op: string
  image: Record<string, unknown>
  /** The params the OP was rendered under; absent or `''`: the defaults. */
  params?: string
  /** The month's replies as posted, newest first (absent in bundles before 2026-11-02). */
  replies?: PreviewReply[]
  /** cw: what the replies were rendered under; absent: a bundle the page can't re-render replies from. */
  reply?: ReplyCfg | null
}

export interface PreviewReply { key: string; scan: string; username: string; body: string; icon_url: string | null; icon_emoji: string | null }
