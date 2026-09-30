import { json } from '../../cfn/http'
import { type Env, isOpen } from '../../cfn/env'

/** `GET /api/capabilities` — what this deployment can do. The static (cloud)
 *  deployment serves scans; it can't walk a filesystem, mutate one, or run
 *  the heavier analyses. The UI hides those affordances. (The Flask server
 *  answers the same shape with everything on.) */
export const CAPABILITIES = {
  static: true,
  scan: false,
  delete: false,
  reveal: false,
  histogram: false,
  filter: false,
  preview: false,
  // On-the-fly diff of two scan slices (`functions/api/compare.ts`); no
  // persisted diff index, so the response carries no `index` field and the
  // client treats it as final (spec `specs/public-diff-demo.md`).
  compare: true,
  progress: false,
  library: false,
  backend: false,
  s3: false,
  // Immediate delete is off (the edge can't reach arbitrary buckets), but a
  // gated deployment can *stage* deletes into D1 for an admin to dispatch
  // (spec `specs/staged-delete.md`). The handler turns this on when not open.
  stageDelete: false,
  // The edge has no inline executor for arbitrary buckets, so staging is the
  // only method it can offer (CP6). A same-account R2 CFN would relax this (CP7).
  deleteApproval: 'staged' as const,
  filesystem: false,
  // `/api/*` is gated (spec `specs/done/pages-auth.md`): the UI probes
  // `/api/auth/whoami` and shows the wall when nobody is signed in. An open
  // demo (`PUBLIC_OPEN`) reports `auth: false`, so the UI skips both.
  auth: true,
}

export const onRequestGet: PagesFunction<Env> = async ({ env }) =>
  // Staged delete needs an identity to attribute a deletion to, so it's off on
  // the open (unauthenticated) demo and on wherever the gate is active.
  json({ ...CAPABILITIES, auth: !isOpen(env), stageDelete: !isOpen(env) }, { maxAge: 300 })
