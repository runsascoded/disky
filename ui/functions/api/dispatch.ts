import type { Env } from '../../cfn/env'
import { postDispatch } from '../../cfn/stagedRoutes'

/** `POST /api/dispatch` — enqueue a plan for the server-side executor (`admin`).
 *  The edge records intent + closes the plan; it never deletes. */
export const onRequestPost: PagesFunction<Env> = ({ env, request }) => postDispatch(env, request)
