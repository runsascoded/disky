import type { Env } from '../../../cfn/env'
import { postUnstage } from '../../../cfn/stagedRoutes'

/** `POST /api/plans/unstage` — remove URIs from every open plan (`view`). */
export const onRequestPost: PagesFunction<Env> = ({ env, request }) => postUnstage(env, request)
