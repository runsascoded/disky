/**
 * `POST /api/hosts/<id>/uploads` `{root, stamp, keys}` → `{urls, expires}`: presigned PUTs for one batch of a
 * capture's keys under `captures/<id>/<root>/<stamp>/`. Its device token only (specs/device-api.md).
 */
import type { Ctx } from '../../../_lib/auth.js'
import { uploads } from '../../../_lib/devices.js'

export const onRequestPost = (ctx: Ctx & { params: { id: string } }): Promise<Response> => uploads(ctx, ctx.params.id)
