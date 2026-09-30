import type { Env } from '../../cfn/env'
import { getStaged } from '../../cfn/stagedRoutes'

/** `GET /api/staged` — open plans + their staged URIs, and the runs feed. */
export const onRequestGet: PagesFunction<Env> = ({ env }) => getStaged(env)
