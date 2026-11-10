import { test } from 'node:test'
import { deepStrictEqual as equal, rejects, throws } from 'node:assert'
import { apiClient, eventRequest, verifyRevision, revisionProbe, waitForHost } from './run.mjs'
import { authorize, cfList, dnsClaim, ownedPagesDeployments, previewPlan, publishPages, publishWorker, retirePages, retireWorker, workerConfig } from './preview.mjs'

const sha = 'a'.repeat(40)
const account = '1'.repeat(32), zone = '2'.repeat(32)
const config = { repository: 'org/repo', targets: { main: { provider: 'pages', project: 'site', domain: 'preview.example.org', account_id: account, zone_id: zone } } }
const plan = previewPlan(config, 'main', 12, sha)
const record = { id: 'dns12', type: 'CNAME', name: plan.hostname, proxied: true, content: 'pr-12.site.pages.dev', comment: plan.owner }
const deployment = n => ({ id: `dep${n}`, environment: 'preview', latest_stage: { status: 'success' }, deployment_trigger: { metadata: { branch: 'pr-12', commit_hash: sha, commit_message: plan.owner } } })
const envelope = rows => ({ result: rows, result_info: { total_pages: 1 } })
const workerPreview = { id: 'preview12', name: 'pr12', slug: 'pr12', urls: [`https://${plan.hostname}`] }
const workerDeployment = { id: 'w-dep12', annotations: { 'workers/message': plan.owner, 'workers/tag': sha } }
const workerPath = `/accounts/${account}/workers/workers/site/previews/pr12`

test('provider-neutral plan has exact bounded names and no credentials', () => {
  equal(plan, { protocol: 'cf-preview-v1', repository: 'org/repo', target: 'main', pr: 12, sha,
    provider: 'pages', account, zone, project: 'site', branch: 'pr-12', preview: 'pr12',
    hostname: 'pr12.preview.example.org', owner: 'cf-preview-v1:org/repo#12:site' })
  for (const number of [0, -1, 1.5, NaN]) throws(() => previewPlan(config, 'main', number, sha), { message: 'Invalid PR number' })
  throws(() => previewPlan(config, 'main', 12, 'a'.repeat(10)), { message: 'Expected full head SHA' })
  throws(() => previewPlan(config, 'prod', 12, sha), { message: 'Unknown preview target/provider' })
})

test('live repository permissions authorize one head, independent of username/team labels', async () => {
  const calls = []
  const pr = { state: 'open', draft: false, base: { ref: 'main', repo: { full_name: 'org/repo' } }, head: { sha }, user: { login: 'author' } }
  const github = async (method, path) => { calls.push([method, path]); return path.endsWith('/permission') ? { permission: 'maintain' } : pr }
  equal(await authorize({ github, repository: 'org/repo', target: 'main', number: 12, sha, actor: 'operator', manual: true }), pr)
  equal(calls, [['GET', '/repos/org/repo/pulls/12'], ['GET', '/repos/org/repo/collaborators/operator/permission']])
  await rejects(authorize({ github, repository: 'org/repo', target: 'main', number: 12, sha: 'b'.repeat(40), actor: 'operator', manual: true }), { message: 'PR head changed; rebuild or approve the new head' })
  await rejects(authorize({ github: async (_, p) => p.endsWith('/permission') ? { permission: 'read' } : pr, repository: 'org/repo', target: 'main', number: 12, sha }), { message: 'A current write collaborator must approve this head' })
})

test('cleanup checks actual closed state; command approval never includes arbitrary text', async () => {
  const pr = { state: 'closed', base: { ref: 'main', repo: { full_name: 'org/repo' } } }
  equal(await authorize({ github: async () => pr, repository: 'org/repo', target: 'main', number: 12, closed: true }), pr)
  equal(eventRequest({ issue: { number: 12, pull_request: {} }, comment: { body: `/preview ${sha}` } }, 'issue_comment', 'operator'), { number: 12, requestedSha: sha, actor: 'operator', manual: true })
  throws(() => eventRequest({ issue: { number: 12, pull_request: {} }, comment: { body: '/preview do something else' } }, 'issue_comment', 'operator'), { message: 'Not a preview command on a PR' })
})

test('all deployment pages are collected, including a full last page', async () => {
  const calls = []
  const rows = await cfList(async (method, path) => {
    calls.push([method, path]); return path.endsWith('page=1') ? { result: Array.from({ length: 25 }, (_, i) => i) } : { result: [25] }
  }, '/deployments?env=preview')
  equal(rows, Array.from({ length: 26 }, (_, i) => i))
  equal(calls, [['GET', '/deployments?env=preview&per_page=25&page=1'], ['GET', '/deployments?env=preview&per_page=25&page=2']])
})

test('production and unmarked deployments cannot be swept under a preview branch name', () => {
  equal(ownedPagesDeployments(plan, { production_branch: 'main', canonical_deployment: { id: 'prod' } }, [deployment(1)]), [deployment(1)])
  throws(() => ownedPagesDeployments(plan, { production_branch: 'pr-12' }, []), { message: 'Refusing production branch cleanup' })
  throws(() => ownedPagesDeployments(plan, { production_branch: 'main' }, [{ ...deployment(1), environment: 'production' }]), { message: 'Unowned or production deployment in preview namespace' })
  throws(() => ownedPagesDeployments(plan, { production_branch: 'main' }, [{ ...deployment(1), deployment_trigger: { metadata: { ...deployment(1).deployment_trigger.metadata, commit_message: 'manual' } } }]), { message: 'Unowned or production deployment in preview namespace' })
  equal(dnsClaim(plan, record), true)
  equal(dnsClaim(plan, { ...record, content: 'site.pages.dev' }), false)
})

test('Pages publication deploys exact head, reserves owned DNS, then registers custom domain', async () => {
  const writes = [], uploads = []
  const cf = async (method, path, body) => {
    if (method !== 'GET') { writes.push([method, path, body]); return path.endsWith('dns_records') ? record : { id: 'domain12', name: plan.hostname } }
    if (path.endsWith('/projects/site')) return { production_branch: 'main' }
    if (path.includes('/dns_records?')) return envelope([])
    if (path.includes('/deployments?')) return envelope([deployment(1)])
    if (path.endsWith(`/domains/${plan.hostname}`)) return null
    throw new Error(path)
  }
  equal(await publishPages({ cf, plan, upload: async argv => uploads.push(argv) }), { ...plan, deployment_id: 'dep1', dns_id: 'dns12', domain_id: 'domain12', url: 'https://pr12.preview.example.org' })
  equal(uploads, [['pages', 'deploy', '--branch', 'pr-12', '--project-name', 'site', '--commit-hash', sha, '--commit-message', plan.owner, '--commit-dirty=false']])
  equal(writes, [
    ['POST', `/zones/${zone}/dns_records`, { type: 'CNAME', name: plan.hostname, content: 'pr-12.site.pages.dev', proxied: true, ttl: 1, comment: plan.owner }],
    ['POST', `/accounts/${account}/pages/projects/site/domains`, { name: plan.hostname }],
  ])
})

test('a custom hostname must not overwrite someone else’s DNS', async () => {
  const writes = []
  const cf = async (method, path) => { if (method !== 'GET') writes.push(method); return path.includes('/dns_records?') ? envelope([{ ...record, comment: 'somebody else' }]) : path.includes('/deployments?') ? envelope([]) : { production_branch: 'main' } }
  await rejects(publishPages({ cf, plan, upload: async () => writes.push('upload') }), { message: 'Preview hostname has an unowned DNS record' })
  equal(writes, [])
})

test('Pages close removes only owned host and payloads; repeat close is a no-op', async () => {
  let live = true
  const deleted = []
  const cf = async (method, path) => {
    if (method === 'DELETE') { deleted.push(path); return null }
    if (path.endsWith('/projects/site')) return { production_branch: 'main', canonical_deployment: { id: 'prod' } }
    if (path.includes('/dns_records?')) return envelope(live ? [record] : [])
    if (path.includes('/deployments?')) return envelope(live ? [deployment(1), deployment(2)] : [])
    if (path.endsWith(`/domains/${plan.hostname}`)) return live ? { id: 'domain12' } : null
    throw new Error(path)
  }
  equal(await retirePages({ cf, plan }), { ...plan, retired: true, removed_deployments: ['dep1', 'dep2'], removed_dns: ['dns12'], removed_domain: 'domain12' })
  equal(deleted, [`/accounts/${account}/pages/projects/site/domains/${plan.hostname}`, `/zones/${zone}/dns_records/dns12`, `/accounts/${account}/pages/projects/site/deployments/dep1?force=true`, `/accounts/${account}/pages/projects/site/deployments/dep2?force=true`])
  live = false
  equal(await retirePages({ cf, plan }), { ...plan, retired: true, removed_deployments: [], removed_dns: [], removed_domain: null })
})

test('Workers preview config excludes all production routing/hooks and pins the owner marker', () => {
  const template = { main: 'edge/worker.js', assets: { directory: 'www/dist' }, vars: { APP: 'demo' }, compatibility_date: '2026-11-10', routes: ['prod.example/*'], triggers: { crons: ['* * * * *'] }, build: { command: 'not-run' } }
  equal(workerConfig(template, plan, '/built'), { name: 'site', account_id: account, main: '/built/edge/worker.js', find_additional_modules: true, no_bundle: true,
    compatibility_date: '2026-11-10', assets: { directory: '/built/www/dist' }, previews: { vars: { APP: 'demo', CF_PREVIEW_OWNER: plan.owner } } })
  throws(() => workerConfig({ main: '../outside.js' }, plan, '/built'), { message: 'Worker entry must be relative to the artifact' })
  throws(() => workerConfig({ main: 'worker.js', assets: { directory: '../outside' } }, plan, '/built'), { message: 'Assets must be relative to the artifact' })
})

test('Worker close removes only its owned native Preview; a repeated close is a no-op', async () => {
  const deleted = []
  const cf = async (method, path) => {
    if (method === 'DELETE') { deleted.push(path); return null }
    if (path === workerPath) return workerPreview
    if (path === `${workerPath}/deployments/latest`) return workerDeployment
    throw new Error(path)
  }
  equal(await retireWorker({ cf, plan }), { ...plan, retired: true, removed_preview: 'preview12' })
  equal(deleted, [workerPath])
  equal(await retireWorker({ cf: async () => null, plan }), { ...plan, retired: true, removed_preview: null })
  await rejects(retireWorker({ cf: async (_, p) => p === workerPath ? workerPreview : { annotations: {} }, plan }), { message: 'Refusing an unowned native Worker Preview' })
})

test('provider errors expose status and route, never the response body or token', async () => {
  const client = apiClient('https://api.cloudflare.com/client/v4', () => 'fake-secret', async () => ({ ok: false, status: 403, json: () => ({ errors: ['fake-secret'] }) }))
  await rejects(client('GET', '/projects/site'), { message: 'GET /projects/site: HTTP 403' })
})

test('certificate propagation is bounded and no HTTPS success is fabricated', async () => {
  const statuses = [503, 401], waits = []
  equal(await waitForHost('https://preview.example', { fetcher: async () => ({ status: statuses.shift() }), wait: async ms => waits.push(ms) }), 401)
  equal(waits, [10000])
  await rejects(waitForHost('https://preview.example', { fetcher: async () => { throw new Error('pending certificate') }, wait: async () => {}, attempts: 2 }), { message: 'Preview hostname did not become reachable with valid TLS within the bounded wait' })
})

test('close can retire a prior target after the PR was retargeted', async () => {
  const pr = { state: 'closed', base: { ref: 'new-base', repo: { full_name: 'org/repo' } } }
  equal(await authorize({ github: async () => pr, repository: 'org/repo', target: 'main', number: 12, closed: true }), pr)
})

test('Worker publication uses native Previews without modifying parent domains or inheriting Base settings', async t => {
  const { mkdtempSync, readFileSync, rmSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  const workDir = mkdtempSync(resolve('tmp/cf-preview-worker-'))
  t.after(() => rmSync(workDir, { recursive: true }))
  let uploaded = false
  const writes = [], uploads = []
  const cf = async (method, path, body) => {
    if (method !== 'GET') { writes.push([method, path, body]); throw new Error('Unexpected write') }
    if (path === workerPath) return uploaded ? workerPreview : null
    if (path === `${workerPath}/deployments/latest`) return workerDeployment
    if (path.includes('/workers/domains?')) return envelope([{ hostname: 'preview.example.org', service: 'site', zone_id: zone, previews_enabled: true }])
    if (path === `/accounts/${account}/workers/workers/site`) return { id: 'parent' }
    throw new Error(path)
  }
  equal(await publishWorker({ cf, plan, template: { main: 'worker.js', compatibility_date: '2026-11-10' }, artifact: '/built', workDir,
    upload: async argv => { uploads.push(argv); uploaded = true } }), { ...plan, preview_id: 'preview12', deployment_id: 'w-dep12', url: 'https://pr12.preview.example.org' })
  equal(uploads, [['preview', '--config', resolve(workDir, 'worker.json'), '--worker-name', 'site', '--name', 'pr12', '--ignore-base-config', '--tag', sha, '--message', plan.owner]])
  equal(writes, [])
  equal(JSON.parse(readFileSync(resolve(workDir, 'worker.json'), 'utf8')), {
    name: 'site', account_id: account, main: '/built/worker.js', find_additional_modules: true, no_bundle: true,
    compatibility_date: '2026-11-10', previews: { vars: { CF_PREVIEW_OWNER: plan.owner } },
  })
})

test('unsupported stateful bindings fail explicitly instead of disappearing', () => {
  throws(() => workerConfig({ main: 'worker.js', durable_objects: {} }, plan, '/built'), { message: 'Unsupported preview template fields: durable_objects' })
})

test('a live custom hostname must serve the captured SHA, not a production revision or redirect', async () => {
  const calls = [], waits = []
  const responses = [
    { status: 200, text: async () => 'b'.repeat(40) },
    { status: 302, text: async () => sha },
    { status: 200, text: async () => `${sha}\n` },
  ]
  equal(await verifyRevision('https://pr12.example.org', { path: '/__revision' }, sha, {
    fetcher: async (url, options) => { calls.push([url, options.method, options.redirect]); return responses.shift() },
    wait: async ms => waits.push(ms),
  }), 200)
  equal(calls, Array(3).fill(['https://pr12.example.org/__revision', 'GET', 'manual']))
  equal(waits, [10000, 10000])
  equal(await verifyRevision('https://pr12.example.org', { path: '/api/health', header: 'x-disky-build' }, sha, {
    fetcher: async () => ({ status: 401, headers: new Headers({ 'x-disky-build': `${sha} 2026-11-10T00:00:00Z` }) }),
  }), 401)
  await rejects(verifyRevision('https://pr12.example.org', { path: '/__revision' }, sha, {
    fetcher: async () => ({ status: 200, text: async () => 'b'.repeat(40) }), attempts: 1,
  }), { message: 'Preview hostname did not serve the captured full source SHA within the bounded wait' })
  for (const path of ['//elsewhere.example/revision', '/\\elsewhere', 'https://elsewhere.example/']) {
    throws(() => revisionProbe({ path }), { message: 'An explicit same-origin revision verification path is required' })
  }
})

test('native Worker preview cleanup refuses a mismatched slug before any deletion', async () => {
  const writes = []
  const cf = async (method) => { if (method !== 'GET') writes.push(method); return { ...workerPreview, slug: 'production' } }
  await rejects(retireWorker({ cf, plan }), { message: 'Unexpected native Preview name/slug' })
  equal(writes, [])
})

test('a PR artifact cannot use symlinks to upload files outside its build', async t => {
  const { mkdtempSync, rmSync, symlinkSync, writeFileSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  const { validateArtifact } = await import('./preview.mjs')
  const root = mkdtempSync(resolve('tmp/cf-preview-artifact-'))
  t.after(() => rmSync(root, { recursive: true }))
  writeFileSync(resolve(root, 'index.html'), '<html></html>')
  equal(validateArtifact(root), undefined)
  symlinkSync('../outside', resolve(root, 'escaped'))
  throws(() => validateArtifact(root), { message: 'Artifact contains a symlink or special file' })
})

test('publication cannot take over an unmarked pre-existing Pages preview branch', async () => {
  const writes = []
  const unowned = deployment(1)
  unowned.deployment_trigger.metadata.commit_message = 'manually deployed'
  const cf = async (method, path) => {
    if (method !== 'GET') writes.push(method)
    return path.includes('/deployments?') ? envelope([unowned]) : { production_branch: 'main' }
  }
  await rejects(publishPages({ cf, plan, upload: async () => writes.push('upload') }), { message: 'Unowned or production deployment in preview namespace' })
  equal(writes, [])
})
