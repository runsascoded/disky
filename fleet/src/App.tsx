import { useQuery } from '@tanstack/react-query'
import { pages, ref, repo } from 'virtual:fleet-docs'
import { pageHref, pageTitle, renderPage } from './docs'
import { Fleet } from './Fleet'

export interface Whoami { email: string | null; signIn: boolean }

export const useWhoami = () => useQuery({
  queryKey: ['whoami'],
  queryFn: async (): Promise<Whoami> => {
    const r = await fetch('/api/whoami')
    if (!r.ok) throw new Error(`whoami: HTTP ${r.status}`)
    return r.json()
  },
})

async function signOut() {
  await fetch('/api/logout', { method: 'POST' })
  location.reload()
}

function Header({ path }: { path: string }) {
  const who = useWhoami().data
  const link = (href: string, label: string) => <a href={href} className={path === href ? 'here' : undefined}>{label}</a>
  return (
    <header>
      <nav>
        {link('/', 'disky')}
        {pages.slice(1).map(p => <span key={p.slug}>{link(pageHref(p), pageTitle(p))}</span>)}
        {link('/fleet', 'Fleet')}
      </nav>
      {who?.email && <span className="who">{who.email} · <button className="link" onClick={signOut}>sign out</button></span>}
    </header>
  )
}

function Doc({ slug }: { slug: string }) {
  const page = pages.find(p => p.slug === slug)
  if (!page) return <NotFound />
  return <article className="doc" dangerouslySetInnerHTML={{ __html: renderPage(page, pages, repo, ref) }} />
}

function NotFound() {
  return <main><h1>Not found</h1><p><a href="/">The docs</a></p></main>
}

/** Where a failed sign-in lands (`/signin?error=oa:<reason>`). */
function SignIn() {
  const q = new URLSearchParams(location.search)
  const error = q.get('error')
  return (
    <main>
      <h1>Sign in</h1>
      {error && <p className="error">Sign-in didn’t complete: {error}</p>}
      <p><a className="button" href="/auth/oa?next=%2Ffleet">Sign in with OA</a></p>
    </main>
  )
}

export function App() {
  const path = location.pathname.replace(/\/+$/, '') || '/'
  const doc = /^\/docs\/(.+)$/.exec(path)
  return (
    <>
      <Header path={path} />
      {path === '/' ? <Doc slug="" />
        : doc ? <Doc slug={doc[1].toLowerCase()} />
        : path === '/fleet' ? <Fleet />
        : path === '/signin' ? <SignIn />
        : <NotFound />}
    </>
  )
}
