// /api/plans[/:id[/items]] — CRUD for first-class deletion plans (specs/cw-sweep.md).
//
//   GET    /api/plans              list plans (+ item/run counts)          viewer
//   POST   /api/plans              { name, note? } -> { id }               admin
//   GET    /api/plans/:id          plan + items + runs                     viewer
//   PATCH  /api/plans/:id          { state: 'closed' }                     admin
//   POST   /api/plans/:id/items    { prefixes: [...], note? }              admin
//   DELETE /api/plans/:id/items    { prefixes: [...] }                     admin
//   POST   /api/plans/stage        { prefixes: [...], note? } -> { plan_id, batch_id, staged, covered, absorbed }
//                                                                          stager (`STAGING` deployments)
//   GET    /api/plans/staged       the shared open plan (+ items, batches, runs), or { plan: null }   viewer
//
// Reads are open to any authenticated viewer; curating a plan's items and
// closing plans require admin; staging (the opt-in trash proposal) needs the
// full base scope, and a stager may remove what they staged themselves. Items
// are editable only while the plan is `open`. Prefixes canonicalize in the
// deployment's shape (`STORE_SCHEME` / `STORE_BUCKETS`).
import type { D1Database } from "@cloudflare/workers-types"
import { type Ctx, type Env as AuthEnv, json, requireAdmin, requireStager, requireViewer } from "../../_lib/auth.js"
import { audit, canonicalPrefix, openPlanId, planDetail, type PlanRow, type PrefixShape, prefixShape, stageItems } from "../../_lib/plans.js"
import { notifyPlan, stageEvent, type NotifyEnv } from "../../_lib/stagedSlack.js"

type Env = AuthEnv & NotifyEnv & { DB?: D1Database }
type Bg = (p: Promise<unknown>) => void

const now = (): number => Math.floor(Date.now() / 1000)

async function readBody(req: Request): Promise<Record<string, unknown>> {
  return ((await req.json().catch(() => null)) as Record<string, unknown> | null) ?? {}
}

async function listPlans(db: D1Database): Promise<Response> {
  const { results } = await db.prepare(`
    SELECT p.*,
      (SELECT count(*) FROM plan_items i WHERE i.plan_id = p.id) AS items,
      (SELECT count(*) FROM deletion_runs r WHERE r.plan_id = p.id) AS runs
    FROM plans p ORDER BY p.created_ts DESC
  `).all()
  return json({ plans: results })
}

async function getPlan(db: D1Database, id: number, staging: boolean): Promise<Response> {
  // On a staging deployment the memo lives on the item's stage batch (one
  // gesture, one note) — joined back so a staged item carries the reason it
  // was trashed. Elsewhere `stage_batches` doesn't exist.
  const d = await planDetail(db, id, staging)
  return d ? json(d) : json({ error: "no such plan" }, 404)
}

async function getStaged(db: D1Database, staging: boolean): Promise<Response> {
  const id = await openPlanId(db)
  if (id == null) return json({ plan: null, items: [], batches: [], runs: [] })
  return json(await planDetail(db, id, staging))
}

async function createPlan(db: D1Database, who: string, body: Record<string, unknown>): Promise<Response> {
  const name = typeof body.name === "string" ? body.name.trim() : ""
  if (!name) return json({ error: "name required" }, 400)
  const note = typeof body.note === "string" ? body.note : null
  const row = await db.prepare(
    "INSERT INTO plans (name, note, state, created_by, created_ts) VALUES (?, ?, 'open', ?, ?) RETURNING id",
  ).bind(name, note, who, now()).first<{ id: number }>()
  const id = row!.id
  await audit(db, "plans", String(id), "insert", who, null, { name, note })
  return json({ id }, 201)
}

async function closePlan(db: D1Database, who: string, id: number, body: Record<string, unknown>): Promise<Response> {
  if (body.state !== "closed") return json({ error: "only { state: 'closed' } is supported" }, 400)
  const plan = await db.prepare("SELECT * FROM plans WHERE id = ?").bind(id).first<PlanRow>()
  if (!plan) return json({ error: "no such plan" }, 404)
  await db.prepare("UPDATE plans SET state = 'closed', closed_ts = ? WHERE id = ?").bind(now(), id).run()
  await audit(db, "plans", String(id), "update", who, { state: plan.state }, { state: "closed" })
  return json({ id, state: "closed" })
}

async function editItems(
  db: D1Database, who: string, id: number, add: boolean, body: Record<string, unknown>, shape: PrefixShape, own = false,
): Promise<Response> {
  const plan = await db.prepare("SELECT * FROM plans WHERE id = ?").bind(id).first<PlanRow>()
  if (!plan) return json({ error: "no such plan" }, 404)
  if (plan.state !== "open") return json({ error: "plan is closed; reopen or make a new plan" }, 409)
  const raw = Array.isArray(body.prefixes) ? body.prefixes : []
  if (!raw.length) return json({ error: "prefixes required" }, 400)
  const prefixes: string[] = []
  for (const r of raw) {
    const c = typeof r === "string" ? canonicalPrefix(r, shape) : null
    if (!c) return json({ error: `bad prefix ${JSON.stringify(r)}` }, 400)
    prefixes.push(c)
  }
  const note = typeof body.note === "string" ? body.note : null
  const ts = now()
  if (add) {
    for (const p of prefixes) {
      await db.prepare(
        "INSERT OR IGNORE INTO plan_items (plan_id, prefix, note, added_by, added_ts) VALUES (?, ?, ?, ?, ?)",
      ).bind(id, p, note, who, ts).run()
    }
    await audit(db, "plan_items", String(id), "insert", who, null, { prefixes })
  } else {
    // A stager (not admin) may only take back what they staged themselves.
    for (const p of prefixes) {
      const r = own
        ? await db.prepare("DELETE FROM plan_items WHERE plan_id = ? AND prefix = ? AND added_by = ?").bind(id, p, who).run()
        : await db.prepare("DELETE FROM plan_items WHERE plan_id = ? AND prefix = ?").bind(id, p).run()
      if (own && !r.meta.changes) return json({ error: `not yours to unstage: ${p}` }, 403)
    }
    await audit(db, "plan_items", String(id), "delete", who, { prefixes, own }, null)
  }
  return json({ id, [add ? "added" : "removed"]: prefixes })
}

const background = (ctx: { waitUntil?: Bg }, p: Promise<unknown>): void => { if (ctx.waitUntil) ctx.waitUntil(p); else void p }

export const onRequest = async (ctx: Ctx & { env: Env; waitUntil?: Bg }): Promise<Response> => {
  if (!ctx.env.DB) return json({ error: "plans store not configured (no D1 binding)" }, 503)
  const db = ctx.env.DB
  const shape = prefixShape(ctx.env)
  const staging = !!ctx.env.STAGING
  const segs = new URL(ctx.request.url).pathname.replace(/^\/api\/plans\/?/, "").split("/").filter(Boolean)
  const method = ctx.request.method

  // /api/plans
  if (segs.length === 0) {
    if (method === "GET") {
      const gated = await requireViewer(ctx)
      return gated instanceof Response ? gated : listPlans(db)
    }
    if (method === "POST") {
      const gated = await requireAdmin(ctx)
      return gated instanceof Response ? gated : createPlan(db, (gated.email ?? gated.name ?? 'guest'), await readBody(ctx.request))
    }
    return json({ error: "method not allowed" }, 405)
  }

  // /api/plans/staged — the shared open plan, for the /staged console.
  if (segs.length === 1 && segs[0] === "staged" && method === "GET") {
    const gated = await requireViewer(ctx)
    return gated instanceof Response ? gated : getStaged(db, staging)
  }

  // /api/plans/stage — the opt-in trash proposal, on deployments that stage.
  if (segs.length === 1 && segs[0] === "stage" && method === "POST") {
    if (!staging) return json({ error: "this deployment does not stage; admins curate plans directly" }, 404)
    // The full base scope: a read-only guest link can't propose.
    const gated = await requireStager(ctx)
    if (gated instanceof Response) return gated
    const body = await readBody(ctx.request)
    const prefixes = Array.isArray(body.prefixes)
      ? (body.prefixes as unknown[]).filter((x): x is string => typeof x === "string")
      : []
    const note = typeof body.note === "string" ? body.note : null
    const by = gated.email ?? gated.name ?? "guest"
    const res = await stageItems(db, prefixes, by, note, shape)
    if ("error" in res) return json(res, 400)
    // Announce the batch in the plan's Slack thread (specs/done/staged-slack.md),
    // after the response — a Slack hiccup never fails the gesture.
    if (res.staged.length) {
      const siteUrl = new URL(ctx.request.url).origin
      background(ctx, notifyPlan(ctx.env, db, res.plan_id, siteUrl,
        stageEvent({ planId: res.plan_id, batchId: res.batch_id, by, prefixes: res.staged, covered: res.covered.length, note, siteUrl })))
    }
    return json(res, 201)
  }

  const id = Number(segs[0])
  if (!Number.isInteger(id) || id <= 0) return json({ error: "bad plan id" }, 400)

  // /api/plans/:id
  if (segs.length === 1) {
    if (method === "GET") {
      const gated = await requireViewer(ctx)
      return gated instanceof Response ? gated : getPlan(db, id, staging)
    }
    if (method === "PATCH") {
      const gated = await requireAdmin(ctx)
      return gated instanceof Response ? gated : closePlan(db, (gated.email ?? gated.name ?? 'guest'), id, await readBody(ctx.request))
    }
    return json({ error: "method not allowed" }, 405)
  }

  // /api/plans/:id/items — admins curate; on a staging deployment a stager
  // may DELETE (unstage) their own items.
  if (segs.length === 2 && segs[1] === "items" && (method === "POST" || method === "DELETE")) {
    const gated = await (staging && method === "DELETE" ? requireStager(ctx) : requireAdmin(ctx))
    if (gated instanceof Response) return gated
    const who = gated.email ?? gated.name ?? 'guest'
    const res = await editItems(db, who, id, method === "POST", await readBody(ctx.request), shape, !gated.admin)
    if (res.ok) {
      const body = (await res.clone().json()) as { added?: string[]; removed?: string[] }
      const n = (body.added ?? body.removed ?? []).length
      const verb = body.added ? "added" : "unstaged"
      background(ctx, notifyPlan(ctx.env, db, id, new URL(ctx.request.url).origin,
        { text: `:leftwards_arrow_with_hook: ${who.replace(/@.*$/, "")} ${verb} ${n} ${n === 1 ? "prefix" : "prefixes"}` }))
    }
    return res
  }

  return json({ error: "not found" }, 404)
}
