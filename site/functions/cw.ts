// The CoreWeave view lives on its own hostname now (cw-s3.oa.dev — its own
// CF Access audience), so `/cw` is a redirect, preserving the query so old
// deep links (`/cw?d=260819-1008`) keep working. The CW unfurl meta that used
// to be rewritten here moved to `functions/index.ts`, keyed on the host.
export const onRequest = (ctx: { request: Request }): Response => {
  const url = new URL(ctx.request.url)
  return Response.redirect(`https://cw-s3.oa.dev/${url.search}`, 302)
}
