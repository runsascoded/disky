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
  equal(eventRequest({ issue: { number: 12, pull_request: {} }, comment: { body: `/preview ${sha}` } }, 'issue_comment', 'operator'), { number: 12, mode: undefined, requestedSha: sha, actor: 'operator', manual: true })
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
  const { mkdirSync, mkdtempSync, readFileSync, rmSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  mkdirSync(resolve('tmp'), { recursive: true })
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
  const { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  const { validateArtifact } = await import('./preview.mjs')
  mkdirSync(resolve('tmp'), { recursive: true })
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

// `ui` mode (specs/cf-pr-previews.md § Modes): selection, the read-only layer, its viewer gate, and the Pages lifecycle around it.
const { classify, handle, unexpectedBindings, verifyAccess } = await import('./layer.mjs')
const { modePlan, publishUi, requireImplemented, selectMode, stageUi, uiCwdCheck, uiProjectCheck, uiSettings } = await import('./preview.mjs')

const team = 'oa.cloudflareaccess.com', aud = 'f'.repeat(64)
const uiTarget = (ui = {}) => ({ ...config.targets.main, verification: { path: '/api/health', header: 'x-disky-build' },
  modes: { ui: { project: 'site-ui', domain: 'ui.example.org', upstream: 'https://site.example.org', working_directory: '.',
    viewers: { access: { team_domain: team, aud }, domains: ['example.org'], emails: ['guest@else.example'] }, ...ui } } })
const uiConfig = { repository: 'org/repo', targets: { main: uiTarget() } }
const { layer } = uiSettings(uiTarget())
const uiPlan = modePlan(uiConfig, 'main', 12, sha, 'ui')
const host = 'https://pr12.ui.example.org'
const layerConfig = { ...layer, sha, pr: 12, hostname: 'pr12.ui.example.org' }
const publicConfig = { ...layerConfig, viewers: 'public' }

const { subtle } = globalThis.crypto
const rsa = () => subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify'])
const pair = await rsa(), otherPair = await rsa()
const accessKeys = [{ ...await subtle.exportKey('jwk', pair.publicKey), kid: 'k1' }]
const b64u = value => Buffer.from(value).toString('base64url')
const nowMs = 1_800_000_000_000
const claims = { iss: `https://${team}`, aud: [aud], email: 'Reviewer@Example.org', exp: nowMs / 1000 + 60, nbf: nowMs / 1000 - 60 }
async function jwt(payload = claims, { key = pair.privateKey, kid = 'k1', alg = 'RS256' } = {}) {
  const head = b64u(JSON.stringify({ alg, kid })), body = b64u(JSON.stringify(payload))
  return `${head}.${body}.${b64u(new Uint8Array(await subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(`${head}.${body}`))))}`
}

/** A layer run against fake production + assets: every upstream and asset request is recorded (the Access key fetch is not). */
function harness({ env = { CF_PREVIEW_READ_TOKEN: 'read-grant' }, upstream = () => new Response('{"ok":true}', { headers: { 'content-type': 'application/json' } }) } = {}) {
  const forwarded = [], assets = []
  const fetcher = async (url, init) => {
    if (url === `https://${team}/cdn-cgi/access/certs`) return Response.json({ keys: accessKeys })
    forwarded.push([url, init.method, Object.fromEntries(init.headers), init.redirect])
    return upstream(url)
  }
  const ASSETS = { fetch: async request => { assets.push([request.method, new URL(request.url).pathname]); return new Response('<html>pr build</html>', { headers: { 'content-type': 'text/html' } }) } }
  const run = async (config, path, { method = 'GET', headers = {} } = {}) => {
    const response = await handle(new Request(`${host}${path}`, { method, headers }), { ...env, ASSETS }, config, { fetcher, now: () => nowMs })
    return { status: response.status, body: method === 'HEAD' ? '' : await response.text(), headers: Object.fromEntries(response.headers) }
  }
  return { forwarded, assets, run }
}

test('mode selection: an explicit /preview <mode> wins, else one preview:<mode> label, else ui', () => {
  equal(selectMode({ labels: [] }), { mode: 'ui', source: 'default' })
  equal(selectMode({ labels: ['bug', 'preview:stack'] }), { mode: 'stack', source: 'label' })
  equal(selectMode({ labels: ['preview:stack', 'preview:stack'] }), { mode: 'stack', source: 'label' })
  equal(selectMode({ requested: 'ui', labels: ['preview:pipeline'] }), { mode: 'ui', source: 'comment' })
  throws(() => selectMode({ labels: ['preview:ui', 'preview:stack'] }), { message: 'Conflicting preview mode labels: preview:stack, preview:ui' })
  throws(() => selectMode({ labels: ['preview:full'] }), { message: 'Unknown preview mode label: preview:full' })
  throws(() => selectMode({ requested: 'full' }), { message: 'Unknown preview mode: full' })
  equal(requireImplemented('ui'), undefined)
  for (const mode of ['stack', 'pipeline']) throws(() => requireImplemented(mode), { message: `Preview mode ${mode} is specified but not implemented (specs/cf-pr-previews.md)` })
})

test('a /preview comment may name a mode and a head; anything else is not a command', () => {
  const comment = body => eventRequest({ issue: { number: 12, pull_request: {} }, comment: { body } }, 'issue_comment', 'operator')
  equal(comment('/preview'), { number: 12, mode: undefined, requestedSha: undefined, actor: 'operator', manual: true })
  equal(comment('/preview UI'), { number: 12, mode: 'ui', requestedSha: undefined, actor: 'operator', manual: true })
  equal(comment(`/preview stack ${sha}`), { number: 12, mode: 'stack', requestedSha: sha, actor: 'operator', manual: true })
  for (const body of ['/preview full', `/preview ${sha} ui`, '/preview ui please']) throws(() => comment(body), { message: 'Not a preview command on a PR' })
  equal(eventRequest({ inputs: { pr: '12', mode: 'ui' } }, 'workflow_dispatch', 'operator'), { number: 12, mode: 'ui', actor: 'operator', manual: true })
  equal(eventRequest({ pull_request: { number: 12 } }, 'pull_request_target', 'author'), { number: 12, mode: undefined, actor: 'author', manual: false })
})

test('a ui plan uses its own project and host, and verifies the layer’s revision, not production’s', () => {
  equal(uiPlan, { protocol: 'cf-preview-v1', repository: 'org/repo', target: 'main', pr: 12, sha, provider: 'pages', account, zone,
    project: 'site-ui', branch: 'pr-12', preview: 'pr12', hostname: 'pr12.ui.example.org', owner: 'cf-preview-v1:org/repo#12:site-ui', mode: 'ui' })
  equal(uiSettings(uiTarget()).target.verification, { path: '/__preview/revision' })
  equal(layer, { upstream: 'https://site.example.org', viewers: { access: { team_domain: team, aud }, emails: ['guest@else.example'], domains: ['example.org'] },
    forward: ['/api/', '/data/', '/v1/files/'], deny: ['/api/auth/', '/api/token'], allow: ['/api/auth/whoami'] })
  equal(uiSettings(uiTarget({ viewers: 'public' })).layer.viewers, 'public')
  throws(() => uiSettings(config.targets.main), { message: 'Target has no ui mode configured' })
  throws(() => uiSettings({ ...uiTarget(), provider: 'workers' }), { message: 'ui mode is implemented for Pages targets only' })
  throws(() => uiSettings(uiTarget({ project: 'site' })), { message: 'ui mode needs its own dedicated Pages project' })
  for (const upstream of ['http://site.example.org', 'https://site.example.org/api', 'https://user@site.example.org']) {
    throws(() => uiSettings(uiTarget({ upstream })), { message: 'ui upstream must be a bare https origin' })
  }
  throws(() => uiSettings(uiTarget({ viewers: undefined })), { message: 'ui viewers need a Cloudflare Access team domain and AUD tag, or "public"' })
  throws(() => uiSettings(uiTarget({ viewers: { access: { team_domain: team, aud } } })), { message: 'ui viewers need a lower-case email or domain allow list' })
  throws(() => modePlan(uiConfig, 'main', 12, sha, 'stack'), { message: 'Preview mode stack is specified but not implemented (specs/cf-pr-previews.md)' })
})

test('the forwarder classifies by method and path alone: reads of allowlisted paths go to production, nothing else does', () => {
  const at = (method, path) => classify(layerConfig, method, new URL(`${host}${path}`))
  equal([
    at('GET', '/api/subtree?date=261007'), at('HEAD', '/data/meta.json'), at('GET', '/v1/files/list?prefix=snapshots/'), at('GET', '/api/auth/whoami'),
    at('GET', '/'), at('GET', '/assets/index-abc.js'), at('GET', '/auth/google'), at('GET', '/v1/objects/bucket/key'), at('GET', '/apiary'),
    at('GET', '/api/token'), at('GET', '/api/token/x'), at('GET', '/api/auth/exchange'), at('GET', '/api/auth/whoami/x'),
    at('GET', '/data/..%2Fapi/token'), at('GET', '/api/subtree?key=share'),
    at('POST', '/api/plans/stage'), at('PUT', '/api/activity/kinds/x'), at('DELETE', '/api/token'), at('PATCH', '/api/db/t'), at('POST', '/'), at('OPTIONS', '/api/subtree'),
  ], [
    { kind: 'forward' }, { kind: 'forward' }, { kind: 'forward' }, { kind: 'forward' },
    { kind: 'asset' }, { kind: 'asset' }, { kind: 'asset' }, { kind: 'asset' }, { kind: 'asset' },
    ...Array(4).fill({ kind: 'refuse', status: 403, reason: 'Not forwarded by the read-only preview' }),
    { kind: 'refuse', status: 400, reason: 'Encoded path separators are refused' },
    { kind: 'refuse', status: 400, reason: 'Credentials in the URL are refused' },
    ...Array(6).fill({ kind: 'refuse', status: 405, reason: 'Read-only preview: only GET and HEAD' }),
  ])
})

test('a forwarded read carries only the preview’s read grant and safe headers; production’s cookies and origin stay behind', async () => {
  const moved = url => url.endsWith('/moved')
  const h = harness({ upstream: url => new Response(moved(url) ? null : 'rows', { status: moved(url) ? 302 : 200, headers: {
    'content-type': 'application/json', 'set-cookie': 'session=prod; Secure; HttpOnly', 'cache-control': 'public, max-age=3600',
    ...(moved(url) ? { location: 'https://site.example.org/api/subtree?date=261008' } : {}),
  } }) })
  const viewer = { 'cf-access-jwt-assertion': await jwt(), cookie: 'CF_Authorization=x; other=y', authorization: 'Bearer from-pr-code', accept: 'application/json', range: 'bytes=0-9', 'x-custom': '1' }
  equal(await h.run(layerConfig, '/api/subtree?date=261007', { headers: viewer }), { status: 200, body: 'rows', headers: {
    'content-type': 'application/json', 'cache-control': 'private, no-store', 'x-robots-tag': 'noindex, nofollow', 'x-cf-preview-revision': sha,
  } })
  equal(await h.run(layerConfig, '/api/moved', { headers: viewer }), { status: 302, body: '', headers: {
    'content-type': 'application/json', 'cache-control': 'private, no-store', location: '/api/subtree?date=261008', 'x-robots-tag': 'noindex, nofollow', 'x-cf-preview-revision': sha,
  } })
  const sent = { accept: 'application/json', authorization: 'Bearer read-grant', range: 'bytes=0-9' }
  equal(h.forwarded, [
    ['https://site.example.org/api/subtree?date=261007', 'GET', sent, 'manual'],
    ['https://site.example.org/api/moved', 'GET', sent, 'manual'],
  ])
  equal(h.assets, [])
})

test('the forwarder refuses writes and non-allowlisted paths without reaching production', async () => {
  const h = harness()
  const headers = { 'cf-access-jwt-assertion': await jwt() }
  const refusal = (status, text, extra = {}) => ({ status, body: `${text}\n`, headers: {
    'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store', ...extra, 'x-robots-tag': 'noindex, nofollow', 'x-cf-preview-revision': sha,
  } })
  const readOnly = refusal(405, 'Read-only preview: only GET and HEAD', { allow: 'GET, HEAD' })
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    equal(await h.run(layerConfig, '/api/plans/stage', { method, headers }), readOnly)
    equal(await h.run(layerConfig, '/auth/email/start', { method, headers }), readOnly)
  }
  equal(await h.run(layerConfig, '/__preview/revision', { method: 'POST' }), readOnly)
  equal(await h.run(layerConfig, '/api/token', { headers }), refusal(403, 'Not forwarded by the read-only preview'))
  equal(await h.run(layerConfig, '/api/auth/logout', { headers }), refusal(403, 'Not forwarded by the read-only preview'))
  equal(await h.run(layerConfig, '/auth/google', { headers }), { status: 200, body: '<html>pr build</html>', headers: {
    'content-type': 'text/html', 'x-robots-tag': 'noindex, nofollow', 'x-cf-preview-revision': sha,
  } })
  equal(h.forwarded, [])
  equal(h.assets, [['GET', '/auth/google']])
})

test('an auth-gated preview admits only a verified Access viewer; the revision probe is the one open path', async () => {
  const h = harness()
  const status = async (assertion, path = '/api/subtree') => {
    const r = await h.run(layerConfig, path, { headers: assertion === undefined ? {} : { 'cf-access-jwt-assertion': assertion } })
    return [r.status, r.body.trim()]
  }
  equal(await status(await jwt()), [200, '{"ok":true}'])
  equal(await status(await jwt(), '/'), [200, '<html>pr build</html>'])
  equal(await status(await jwt({ ...claims, email: 'guest@else.example' })), [200, '{"ok":true}'])
  equal(await status(undefined), [403, 'Cloudflare Access sign-in required'])
  equal(await status(undefined, '/'), [403, 'Cloudflare Access sign-in required'])
  equal(await status('not-a-jwt'), [403, 'Cloudflare Access sign-in required'])
  equal(await status('x.y.z'), [403, 'Malformed Access assertion'])
  equal(await status(await jwt(claims, { alg: 'HS256' })), [403, 'Unexpected Access assertion algorithm'])
  equal(await status(await jwt(claims, { kid: 'k2' })), [403, 'Unknown Access signing key'])
  equal(await status(await jwt(claims, { key: otherPair.privateKey })), [403, 'Bad Access assertion signature'])
  equal(await status(await jwt({ ...claims, iss: 'https://other.cloudflareaccess.com' })), [403, 'Access assertion from another team'])
  equal(await status(await jwt({ ...claims, aud: ['0'.repeat(64)] })), [403, 'Access assertion for another application'])
  equal(await status(await jwt({ ...claims, exp: nowMs / 1000 })), [403, 'Expired Access assertion'])
  equal(await status(await jwt({ ...claims, nbf: nowMs / 1000 + 60 })), [403, 'Expired Access assertion'])
  equal(await status(await jwt({ ...claims, email: 'someone@example.org.evil' })), [403, 'Not a preview viewer'])
  equal(await status(await jwt({ ...claims, email: undefined, common_name: 'service-token' })), [403, 'Not a preview viewer'])
  equal(await status(undefined, '/__preview/revision'), [200, sha])
  equal(h.forwarded.map(([url]) => url), ['https://site.example.org/api/subtree', 'https://site.example.org/api/subtree'])
  equal(h.assets, [['GET', '/']])
  equal(await verifyAccess(await jwt(), layer.viewers, accessKeys, nowMs), { ok: true, email: 'reviewer@example.org' })
})

test('the layer serves only its own host, refuses any binding, and needs its grant unless public', async () => {
  const headers = { 'cf-access-jwt-assertion': await jwt() }
  const brief = r => [r.status, r.body.trim()]
  equal(brief(await harness().run({ ...layerConfig, hostname: 'pr12.other.example' }, '/api/subtree', { headers })), [421, 'Preview serves only its own hostname'])
  equal(brief(await harness({ env: { CF_PREVIEW_READ_TOKEN: 'read-grant', DB: {}, STORE: { get() {} } } }).run(layerConfig, '/', { headers })), [503, 'Preview refuses bindings: DB, STORE'])
  equal(brief(await harness({ env: {} }).run(layerConfig, '/', { headers })), [503, 'Preview read grant CF_PREVIEW_READ_TOKEN is not set'])
  equal(unexpectedBindings({ ASSETS: {}, CF_PREVIEW_READ_TOKEN: 'x', NOTE: 'plain' }), [])
  const open = harness({ env: {} })
  equal(brief(await open.run(publicConfig, '/data/meta.json', { headers: { cookie: 'a=b', authorization: 'Bearer pr' } })), [200, '{"ok":true}'])
  equal(open.forwarded, [['https://site.example.org/data/meta.json', 'GET', {}, 'manual']])
})

test('a ui project must hold no binding and exactly the read grant; Wrangler must not pick up Functions or a config', async t => {
  const preview = (extra = {}) => ({ deployment_configs: { preview: { compatibility_date: '2026-11-01', compatibility_flags: ['nodejs_compat'], d1_databases: {}, r2_buckets: {},
    env_vars: { CF_PREVIEW_READ_TOKEN: { type: 'secret_text', value: null } }, ...extra } } })
  equal(uiProjectCheck(preview(), layer), undefined)
  throws(() => uiProjectCheck(preview({ d1_databases: { DB: { id: 'prod' } }, r2_buckets: { STORE: { name: 'prod' } } }), layer), { message: 'ui project has preview bindings: d1_databases, r2_buckets' })
  throws(() => uiProjectCheck(preview({ hyperdrive_bindings: { H: {} } }), layer), { message: 'ui project has preview bindings: hyperdrive_bindings' })
  throws(() => uiProjectCheck(preview({ env_vars: { CF_PREVIEW_READ_TOKEN: { type: 'secret_text' }, SESSION_SECRET: { type: 'secret_text' } } }), layer),
    { message: 'ui project preview variables must be exactly [CF_PREVIEW_READ_TOKEN (secret_text)], found [CF_PREVIEW_READ_TOKEN (secret_text), SESSION_SECRET (secret_text)]' })
  throws(() => uiProjectCheck(preview({ env_vars: { CF_PREVIEW_READ_TOKEN: { type: 'plain_text', value: 'leaked' } } }), layer),
    { message: 'ui project preview variables must be exactly [CF_PREVIEW_READ_TOKEN (secret_text)], found [CF_PREVIEW_READ_TOKEN (plain_text)]' })
  equal(uiProjectCheck(preview({ env_vars: {} }), { ...layer, viewers: 'public' }), undefined)
  const { mkdirSync, mkdtempSync, rmSync, writeFileSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  mkdirSync(resolve('tmp'), { recursive: true })
  const dir = mkdtempSync(resolve('tmp/cf-preview-cwd-'))
  t.after(() => rmSync(dir, { recursive: true }))
  equal(uiCwdCheck(dir), undefined)
  mkdirSync(resolve(dir, 'functions'))
  writeFileSync(resolve(dir, 'wrangler.toml'), 'name = "prod"\n')
  throws(() => uiCwdCheck(dir), { message: 'ui deploy directory must not hold functions, wrangler.toml' })
})

test('ui publish stages the PR’s assets under the trusted layer, then runs the owned Pages lifecycle', async t => {
  const { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } = await import('node:fs')
  const { resolve } = await import('node:path')
  const { pathToFileURL } = await import('node:url')
  mkdirSync(resolve('tmp'), { recursive: true })
  const root = mkdtempSync(resolve('tmp/cf-preview-ui-'))
  t.after(() => rmSync(root, { recursive: true }))
  const artifact = resolve(root, 'dist'), workDir = resolve(root, 'work'), cwd = resolve(root, 'cwd')
  for (const dir of [artifact, resolve(artifact, 'assets'), cwd]) mkdirSync(dir, { recursive: true })
  writeFileSync(resolve(artifact, 'index.html'), '<html></html>')
  writeFileSync(resolve(artifact, 'assets', 'index.js'), 'console.log(1)')
  writeFileSync(resolve(artifact, '_routes.json'), '{"version":1,"include":["/api/*"],"exclude":["/*"]}')
  writeFileSync(resolve(artifact, '_worker.js'), 'export default { fetch: () => fetch("https://evil.example") }')
  const project = { production_branch: 'main', deployment_configs: { preview: { env_vars: { CF_PREVIEW_READ_TOKEN: { type: 'secret_text' } } } } }
  const uiRecord = { ...record, name: uiPlan.hostname, content: 'pr-12.site-ui.pages.dev', comment: uiPlan.owner }
  const uiDeployment = { ...deployment(1), deployment_trigger: { metadata: { branch: 'pr-12', commit_hash: sha, commit_message: uiPlan.owner } } }
  const writes = [], uploads = []
  const cf = async (method, path) => {
    if (method !== 'GET') { writes.push([method, path]); return path.endsWith('dns_records') ? uiRecord : { id: 'domain12' } }
    if (path.endsWith('/projects/site-ui')) return project
    if (path.includes('/dns_records?')) return envelope([])
    if (path.includes('/deployments?')) return envelope(uploads.length ? [uiDeployment] : [])
    if (path.endsWith(`/domains/${uiPlan.hostname}`)) return null
    throw new Error(path)
  }
  const uploader = staging => async argv => uploads.push([staging, ...argv])
  equal(await publishUi({ cf, plan: uiPlan, layer, artifact, workDir, cwd, uploader }), { ...uiPlan, deployment_id: 'dep1', dns_id: 'dns12', domain_id: 'domain12',
    url: 'https://pr12.ui.example.org', upstream: 'https://site.example.org', viewers: 'access', replaced: ['_worker.js', '_routes.json'] })
  const staging = resolve(workDir, 'ui-site')
  equal(uploads, [[staging, 'pages', 'deploy', '--branch', 'pr-12', '--project-name', 'site-ui', '--commit-hash', sha, '--commit-message', uiPlan.owner, '--commit-dirty=false']])
  equal(writes, [['POST', `/zones/${zone}/dns_records`], ['POST', `/accounts/${account}/pages/projects/site-ui/domains`]])
  equal(JSON.parse(readFileSync(resolve(staging, '_routes.json'), 'utf8')), { version: 1, include: ['/*'], exclude: [] })
  equal([existsSync(resolve(staging, 'index.html')), existsSync(resolve(staging, 'assets', 'index.js'))], [true, true])
  // The generated worker is self-contained: the layer with this publish's settings, and nothing of the PR's `_worker.js`.
  copyFileSync(resolve(staging, '_worker.js'), resolve(root, 'worker.mjs'))
  const worker = (await import(pathToFileURL(resolve(root, 'worker.mjs')).href)).default
  const response = await worker.fetch(new Request(`${host}/__preview/revision`), { CF_PREVIEW_READ_TOKEN: 'read-grant', ASSETS: {} })
  equal([response.status, await response.text()], [200, `${sha}\n`])
  equal(readFileSync(resolve(staging, '_worker.js'), 'utf8').includes('evil.example'), false)

  writes.length = 0
  uploads.length = 0
  project.deployment_configs.preview.d1_databases = { DB: { id: 'prod' } }
  await rejects(publishUi({ cf, plan: uiPlan, layer, artifact, workDir, cwd, uploader }), { message: 'ui project has preview bindings: d1_databases' })
  equal([writes, uploads], [[], []])
  equal(stageUi({ artifact: resolve(staging, 'assets'), staging: resolve(root, 'again'), layer, plan: uiPlan }), [])
})
