import type { Env } from '../../../cfn/env'
import { postStage } from '../../../cfn/stagedRoutes'

/** `POST /api/plans/stage` — stage URIs into the shared open plan (`view`). */
export const onRequestPost: PagesFunction<Env> = ({ env, request }) => postStage(env, request)
