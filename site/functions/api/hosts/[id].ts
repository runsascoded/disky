/**
 * `GET /api/hosts/<id>` — a host's status; `DELETE` — disconnect it. Its device token, or an admin session
 * (specs/device-api.md).
 */
import type { Ctx } from '../../_lib/auth.js'
import { disconnect, hostStatus } from '../../_lib/devices.js'

type IdCtx = Ctx & { params: { id: string } }

export const onRequestGet = (ctx: IdCtx): Promise<Response> => hostStatus(ctx, ctx.params.id)
export const onRequestDelete = (ctx: IdCtx): Promise<Response> => disconnect(ctx, ctx.params.id)
