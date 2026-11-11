/** The docs pages as the app serves them: `/` the index, `/docs/<slug>` the rest, each Markdown rendered with its
 *  relative links pointed where they resolve (another page here, else the file on the repo's host). */
import { marked, type Token } from 'marked'

export interface Page { slug: string; path: string; md: string }

export const pageHref = (p: Pick<Page, 'slug'>): string => (p.slug ? `/docs/${p.slug}` : '/')

/** A page's title: its first `# ` heading, else its file's name. */
export const pageTitle = (p: Page): string => /^# +(.+)$/m.exec(p.md)?.[1].trim() ?? p.path.split('/').pop()!.replace(/\.md$/, '')

/** `rel` resolved against the repo-relative directory `dir`; null when it climbs out of the repo. */
export function resolvePath(dir: string, rel: string): string | null {
  const out = dir ? dir.split('/') : []
  for (const seg of rel.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') { if (!out.length) return null; out.pop() } else out.push(seg)
  }
  return out.join('/')
}

/** Where a link (or, `image`, an image source) in the page at repo path `from` points. */
export function rewriteHref(href: string, from: string, pages: readonly Page[], repo: string, ref: string, image = false): string {
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('#') || href.startsWith('/')) return href
  const [path, hash] = href.split(/(?=#)/)
  const dir = from.includes('/') ? from.slice(0, from.lastIndexOf('/')) : ''
  const target = resolvePath(dir, path)
  if (target === null) return href
  const page = pages.find(p => p.path === target)
  if (page && !image) return pageHref(page) + (hash ?? '')
  return `${repo}/${image ? 'raw' : 'blob'}/${ref}/${target}${hash ?? ''}`
}

/** The page's HTML. Its Markdown is the repo's own (trusted), so raw HTML in it passes through. */
export function renderPage(p: Page, pages: readonly Page[], repo: string, ref: string): string {
  return marked.parse(p.md, {
    async: false,
    gfm: true,
    walkTokens: (t: Token) => {
      if (t.type === 'link') t.href = rewriteHref(t.href, p.path, pages, repo, ref)
      else if (t.type === 'image') t.href = rewriteHref(t.href, p.path, pages, repo, ref, true)
    },
  }) as string
}
