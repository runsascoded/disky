// `/og/<kind>.png?<view>&sig=…`: a page's signed card (specs/done/dogi.md). No
// sign-in: the signature is the authorization, and it covers the tier. `/og/run.png` is a deletion run's count-only card,
// for a signed-in viewer only: no unfurl links to it for anyone else. `/og/deploy.png` is the deployment's status card
// (the run thread head's image, `_lib/og/deploy.ts`): its signature alone draws it, else the generic card.
import { requireViewer } from '../_lib/auth.js'
import { serveDeployCard } from '../_lib/og/deploy.js'
import { serveCard, serveRunCard, type OgEnv } from '../_lib/og/serve.js'
import { onRequestGet as shortcut } from './index.js'

export const onRequestGet = async (ctx: { request: Request; env: OgEnv; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const { pathname } = new URL(ctx.request.url)
  if (pathname === '/og/run.png') {
    const viewer = await requireViewer(ctx)
    return viewer instanceof Response ? viewer : serveRunCard(ctx)
  }
  if (pathname === '/og/deploy.png') return serveDeployCard(ctx)
  return pathname === '/og' || pathname === '/og/' ? shortcut(ctx) : serveCard(ctx)
}
