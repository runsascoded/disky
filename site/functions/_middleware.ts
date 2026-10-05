// Every HTML page response gets its card stamped into `<head>`
// (specs/done/dogi.md): crawlers never run the React router, so the edge says what
// each URL shows. Everything else passes through untouched.
import { stampPage, type OgEnv } from './_lib/og/serve.js'

export const onRequest = async (ctx: { request: Request; env: OgEnv; next: () => Promise<Response> }): Promise<Response> => {
  const res = await ctx.next()
  if (ctx.request.method !== 'GET' || !(res.headers.get('content-type') ?? '').startsWith('text/html')) return res
  try {
    return await stampPage(ctx.env, new URL(ctx.request.url), res)
  } catch (e) {
    // An unfurl detail must never break the page.
    console.log(`og stamp failed: ${(e as Error).message}`)
    return res
  }
}
