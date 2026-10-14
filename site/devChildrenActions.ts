import type { Plugin } from 'vite'

/** Only requests referred by this fictional DEV page are intercepted. Never
 * installed in production, and never forwards fixture writes to the API. */
export const devChildrenActions: Plugin = {
  name: 'dev-children-actions',
  configureServer(server) {
    server.middlewares.use('/api/plans', (req, res, next) => {
      const ref = req.headers.referer ? new URL(req.headers.referer) : null
      if (ref?.pathname !== '/dev/children-table') return next()
      const chunks: Buffer[] = []
      req.on('data', chunk => chunks.push(Buffer.from(chunk)))
      req.on('end', () => {
        const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) as { prefixes?: string[]; objects?: string[] } : {}
        const outcome = ref.searchParams.get('outcome')
        const prefixes = body.prefixes ?? []
        const objects = body.objects ?? []
        setTimeout(() => {
          res.setHeader('content-type', 'application/json')
          if (req.url !== '/stage') { res.statusCode = 400; res.end(JSON.stringify({ error: 'Only synthetic staging is supported in this fixture' })); return }
          res.statusCode = outcome === 'error' ? 503 : 200
          res.end(JSON.stringify(outcome === 'error' ? { error: 'Synthetic staging failure' } : {
            plan_id: 1, batch_id: 1, staged: outcome === 'already' ? [] : [...prefixes, ...objects],
            staged_objects: outcome === 'already' ? [] : objects,
            covered: [], absorbed: [], as_of: null,
          }))
        }, 2500)
      })
    })
  },
}
