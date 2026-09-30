// Per-path unfurl meta: crawlers never run the React router, so any route
// that wants its own title/description/og:image gets a Pages Function that
// serves the SPA shell with its meta rewritten at the edge (same pattern as
// the hostname-keyed CW rewrite in functions/index.ts).

export interface Unfurl {
  title: string
  desc: string
  image: string
  page: string
}

interface AssetsEnv {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
}

export async function unfurlShell(
  ctx: { request: Request; env: AssetsEnv },
  meta: Unfurl,
): Promise<Response> {
  const shell = await ctx.env.ASSETS.fetch(
    // `/`, not `/index.html`: the asset server canonicalises the latter with a
    // 308 whose empty body would sail through the rewriter untouched.
    new Request(new URL('/', ctx.request.url).toString(), ctx.request),
  )
  if (!shell.ok) return shell
  return new HTMLRewriter()
    .on('meta', {
      element(el) {
        const key = el.getAttribute('property') ?? el.getAttribute('name')
        if (!key) return
        const value =
          key === 'og:title' || key === 'twitter:title' ? meta.title
          : key === 'og:description' || key === 'twitter:description' || key === 'description' ? meta.desc
          : key === 'og:image' || key === 'twitter:image' ? meta.image
          : key === 'og:url' ? meta.page
          : null
        if (value) el.setAttribute('content', value)
      },
    })
    .on('title', {
      element(el) {
        el.setInnerContent(meta.title)
      },
    })
    .transform(shell)
}

/** `path`'s absolute URL when the deployment ships that image, else null.
 * People-specific cards (`/og-users.jpg`, `/og-user/<id>.jpg`) are generated
 * per deployment (`scripts/shoot-user-ogs.mjs`), never tracked in the base.
 * A missing asset comes back as the SPA shell (200, text/html) — only a real
 * image counts. */
export async function assetImage(
  ctx: { request: Request; env: { ASSETS: { fetch: (req: Request) => Promise<Response> } } },
  path: string,
): Promise<string | null> {
  const url = `${new URL(ctx.request.url).origin}${path}`
  const probe = await ctx.env.ASSETS.fetch(new Request(url, { method: 'HEAD' })).catch(() => null)
  return probe?.ok && (probe.headers.get('content-type') ?? '').startsWith('image/') ? url : null
}
