// `/og/<kind>.png?<view>&sig=…`: a page's signed card (specs/done/dogi.md). No
// sign-in: the signature is the authorization, and it covers the tier.
import { serveCard, type OgEnv } from '../_lib/og/serve.js'
import { onRequestGet as shortcut } from './index.js'

export const onRequestGet = async (ctx: { request: Request; env: OgEnv; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const { pathname } = new URL(ctx.request.url)
  return pathname === '/og' || pathname === '/og/' ? shortcut(ctx) : serveCard(ctx)
}
