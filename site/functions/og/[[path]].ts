// `/og/<kind>.png?<view>&sig=…`: a page's signed card (specs/done/dogi.md). No
// sign-in: the signature is the authorization, and it covers the tier.
import { serveCard, type OgEnv } from '../_lib/og/serve.js'

export const onRequestGet = (ctx: { request: Request; env: OgEnv; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => serveCard(ctx)
