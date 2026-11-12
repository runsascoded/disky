import { test } from 'node:test'
import { deepStrictEqual as equal, rejects, throws } from 'node:assert'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { apiClient, eventRequest, revisionProbe, verifyRevision } from './run.mjs'
import {
  authorize, cfList, fullCwdCheck, modePlan, ownedPagesDeployments, previewPlan, publishFull, publishWorker, requireImplemented,
  retireFull, retireWorker, selectMode, validateArtifact, workerConfig, wranglerUpload,
} from './preview.mjs'

const sha = 'a'.repeat(40)
const account = '1'.repeat(32), zone = '2'.repeat(32)
const verification = { path: '/api/health', header: 'x-disky-build' }
const pagesTarget = { provider: 'pages', project: 'site', account_id: account, verification }
const config = { repository: 'org/repo', targets: { main: pagesTarget } }
const plan = modePlan(config, 'main', 12, sha, 'full')
const projectPath = `/accounts/${account}/pages/projects/site`
const project = { subdomain: 'site.pages.dev', production_branch: 'main', canonical_deployment: { id: 'prod' } }
const deployment = (n, metadata = {}, extra = {}) => ({
  id: `dep${n}`, environment: 'preview', latest_stage: { status: 'success' }, aliases: n === 1 ? ['https://pr-12.site.pages.dev'] : null,
  deployment_trigger: { metadata: { branch: 'pr-12', commit_hash: sha, commit_message: plan.owner, ...metadata } }, ...extra,
})
const devDeployment = { id: 'devdep', environment: 'preview', latest_stage: { status: 'success' }, aliases: ['https://dev.site.pages.dev'],
  deployment_trigger: { metadata: { branch: 'dev', commit_hash: 'b'.repeat(40), commit_message: 'release' } } }
const envelope = rows => ({ result: rows, result_info: { total_pages: 1 } })
const workersConfig = { repository: 'org/repo', targets: { main: { provider: 'workers', project: 'site', domain: 'preview.example.org', account_id: account, zone_id: zone } } }
const workerPlan = previewPlan(workersConfig, 'main', 12, sha)
const workerPreview = { id: 'preview12', name: 'pr12', slug: 'pr12', urls: ['https://pr12.preview.example.org'] }
const workerDeployment = { id: 'w-dep12', annotations: { 'workers/message': workerPlan.owner, 'workers/tag': sha } }
const workerPath = `/accounts/${account}/workers/workers/site/previews/pr12`

function scratch(t, prefix) {
  mkdirSync(resolve('tmp'), { recursive: true })
  const dir = mkdtempSync(resolve(`tmp/${prefix}-`))
  t.after(() => rmSync(dir, { recursive: true }))
  return dir
}

test('a full plan is the pr-N branch of the deployment’s own Pages project, at its pages.dev branch host, with no credentials', () => {
  equal(plan, { protocol: 'cf-preview-v1', repository: 'org/repo', target: 'main', pr: 12, sha, provider: 'pages', account,
    project: 'site', owner: 'cf-preview-v1:org/repo#12:site', branch: 'pr-12', subdomain: 'site.pages.dev',
    protected_branches: ['dev'], hostname: 'pr-12.site.pages.dev', mode: 'full' })
  const custom = { ...pagesTarget, subdomain: 'site-4xq.pages.dev', protected_branches: ['staging', 'dev'] }
  equal(modePlan({ ...config, targets: { main: custom } }, 'main', 7, sha, 'full'), { protocol: 'cf-preview-v1', repository: 'org/repo',
    target: 'main', pr: 7, sha, provider: 'pages', account, project: 'site', owner: 'cf-preview-v1:org/repo#7:site', branch: 'pr-7',
    subdomain: 'site-4xq.pages.dev', protected_branches: ['dev', 'staging'], hostname: 'pr-7.site-4xq.pages.dev', mode: 'full' })
  for (const number of [0, -1, 1.5, NaN]) throws(() => previewPlan(config, 'main', number, sha), { message: 'Invalid PR number' })
  throws(() => previewPlan(config, 'main', 12, 'a'.repeat(10)), { message: 'Expected full head SHA' })
  throws(() => previewPlan(config, 'prod', 12, sha), { message: 'Unknown preview target/provider' })
  throws(() => previewPlan({ ...config, targets: { main: { ...pagesTarget, subdomain: 'site.example.org' } } }, 'main', 12, sha), { message: 'Invalid Pages subdomain' })
  throws(() => previewPlan({ ...config, targets: { main: { ...pagesTarget, protected_branches: ['pr-12'] } } }, 'main', 12, sha), { message: 'Preview branch pr-12 is protected' })
})

test('mode selection: an explicit /preview <mode> wins, else one preview:<mode> label, else full; only full is built, on Pages, with a probe', () => {
  equal(selectMode({ labels: [] }), { mode: 'full', source: 'default' })
  equal(selectMode({ labels: ['bug', 'preview:stack'] }), { mode: 'stack', source: 'label' })
  equal(selectMode({ labels: ['preview:stack', 'preview:stack'] }), { mode: 'stack', source: 'label' })
  equal(selectMode({ requested: 'full', labels: ['preview:pipeline'] }), { mode: 'full', source: 'comment' })
  throws(() => selectMode({ labels: ['preview:full', 'preview:stack'] }), { message: 'Conflicting preview mode labels: preview:full, preview:stack' })
  throws(() => selectMode({ labels: ['preview:ui'] }), { message: 'Unknown preview mode label: preview:ui' })
  throws(() => selectMode({ requested: 'ui' }), { message: 'Unknown preview mode: ui' })
  equal(requireImplemented('full'), undefined)
  for (const mode of ['stack', 'pipeline']) {
    throws(() => requireImplemented(mode), { message: `Preview mode ${mode} is specified but not implemented (specs/cf-pr-previews.md)` })
    throws(() => modePlan(config, 'main', 12, sha, mode), { message: `Preview mode ${mode} is specified but not implemented (specs/cf-pr-previews.md)` })
  }
  throws(() => modePlan(workersConfig, 'main', 12, sha, 'full'), { message: 'full mode is implemented for Pages targets only' })
  throws(() => modePlan({ ...config, targets: { main: { ...pagesTarget, verification: undefined } } }, 'main', 12, sha, 'full'),
    { message: 'An explicit same-origin revision verification path is required' })
})

test('a /preview comment may name a mode and a head; anything else is not a command', () => {
  const comment = body => eventRequest({ issue: { number: 12, pull_request: {} }, comment: { body } }, 'issue_comment', 'operator')
  equal(comment('/preview'), { number: 12, mode: undefined, requestedSha: undefined, actor: 'operator', manual: true })
  equal(comment(`/preview ${sha}`), { number: 12, mode: undefined, requestedSha: sha, actor: 'operator', manual: true })
  equal(comment('/preview FULL'), { number: 12, mode: 'full', requestedSha: undefined, actor: 'operator', manual: true })
  equal(comment(`/preview stack ${sha}`), { number: 12, mode: 'stack', requestedSha: sha, actor: 'operator', manual: true })
  for (const body of ['/preview ui', `/preview ${sha} full`, '/preview full please', '/preview do something else']) {
    throws(() => comment(body), { message: 'Not a preview command on a PR' })
  }
  equal(eventRequest({ inputs: { pr: '12', mode: 'full' } }, 'workflow_dispatch', 'operator'), { number: 12, mode: 'full', actor: 'operator', manual: true })
  equal(eventRequest({ pull_request: { number: 12 } }, 'pull_request_target', 'author'), { number: 12, mode: undefined, actor: 'author', manual: false })
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

test('cleanup checks actual closed state, including after the PR was retargeted', async () => {
  const closed = { state: 'closed', base: { ref: 'new-base', repo: { full_name: 'org/repo' } } }
  equal(await authorize({ github: async () => closed, repository: 'org/repo', target: 'main', number: 12, closed: true }), closed)
  await rejects(authorize({ github: async () => ({ ...closed, state: 'open' }), repository: 'org/repo', target: 'main', number: 12, closed: true }), { message: 'Cleanup requires a closed PR' })
})

test('all deployment pages are collected, including a full last page', async () => {
  const calls = []
  const rows = await cfList(async (method, path) => {
    calls.push([method, path]); return path.endsWith('page=1') ? { result: Array.from({ length: 25 }, (_, i) => i) } : { result: [25] }
  }, '/deployments?env=preview')
  equal(rows, Array.from({ length: 26 }, (_, i) => i))
  equal(calls, [['GET', '/deployments?env=preview&per_page=25&page=1'], ['GET', '/deployments?env=preview&per_page=25&page=2']])
})

test('only pr-N’s owned preview deployments are selected; production, protected and unmarked ones are refused', () => {
  equal(ownedPagesDeployments(plan, project, [deployment(1), devDeployment, deployment(2)]), [deployment(1), deployment(2)])
  throws(() => ownedPagesDeployments(plan, { ...project, production_branch: 'pr-12' }, []), { message: 'Refusing a production or protected branch' })
  throws(() => ownedPagesDeployments(plan, { ...project, subdomain: 'site-4xq.pages.dev' }, []),
    { message: 'Pages project site serves site-4xq.pages.dev, not site.pages.dev; set the target\'s subdomain' })
  const refused = { message: 'Unowned or production deployment in preview namespace' }
  throws(() => ownedPagesDeployments(plan, project, [deployment(1, {}, { environment: 'production' })]), refused)
  throws(() => ownedPagesDeployments(plan, project, [deployment(1, {}, { id: 'prod' })]), refused)
  throws(() => ownedPagesDeployments(plan, project, [deployment(1, { commit_message: 'manual' })]), refused)
  for (const alias of ['https://dev.site.pages.dev', 'https://main.site.pages.dev/']) {
    throws(() => ownedPagesDeployments(plan, project, [deployment(2, {}, { aliases: [alias] })]), { message: 'Preview deployment holds a production or protected alias' })
  }
})

/** A fake CF API over one Pages project: the `pr-12` branch's deployments appear once uploaded; writes are recorded. */
function pagesApi({ before = [], after = [deployment(1)], projectDoc = project } = {}) {
  const writes = [], uploads = []
  const cf = async (method, path) => {
    if (method !== 'GET') { writes.push([method, path]); return null }
    if (path === projectPath) return projectDoc
    if (path.startsWith(`${projectPath}/deployments?env=preview&`)) return envelope([...(uploads.length ? after : before), devDeployment])
    throw new Error(path)
  }
  return { cf, writes, uploads, upload: async argv => uploads.push(argv) }
}

test('full publish uploads the PR’s build as pr-N of the deployment’s project, reads it back, and writes nothing else', async t => {
  const root = scratch(t, 'cf-preview-full')
  const artifact = resolve(root, 'dist'), cwd = resolve(root, 'site')
  for (const dir of [resolve(artifact, '_worker.js'), resolve(cwd, 'functions')]) mkdirSync(dir, { recursive: true })
  writeFileSync(resolve(artifact, 'index.html'), '<html></html>')
  writeFileSync(resolve(artifact, '_worker.js', 'index.js'), 'export default {}')
  const api = pagesApi({ before: [deployment(3, { commit_hash: 'c'.repeat(40) })], after: [deployment(1), deployment(3, { commit_hash: 'c'.repeat(40) })] })
  equal(await publishFull({ cf: api.cf, plan, artifact, cwd, upload: api.upload }), { ...plan, deployment_id: 'dep1', url: 'https://pr-12.site.pages.dev' })
  equal(api.uploads, [['pages', 'deploy', '--branch', 'pr-12', '--project-name', 'site', '--commit-hash', sha, '--commit-message', plan.owner, '--commit-dirty=false']])
  equal(api.writes, [])

  const failed = pagesApi({ after: [deployment(1, {}, { latest_stage: { status: 'failure' } })] })
  await rejects(publishFull({ cf: failed.cf, plan, artifact, cwd, upload: failed.upload }), { message: 'Preview deployment readback failed' })
  const unowned = pagesApi({ before: [deployment(1, { commit_message: 'manually deployed' })] })
  await rejects(publishFull({ cf: unowned.cf, plan, artifact, cwd, upload: unowned.upload }), { message: 'Unowned or production deployment in preview namespace' })
  const production = pagesApi({ projectDoc: { ...project, production_branch: 'pr-12' } })
  await rejects(publishFull({ cf: production.cf, plan, artifact, cwd, upload: production.upload }), { message: 'Refusing a production or protected branch' })
  equal([unowned.uploads, production.uploads], [[], []])
})

test('the base branch’s Functions can never be deployed under the PR’s front end', async t => {
  const root = scratch(t, 'cf-preview-cwd')
  const artifact = resolve(root, 'dist'), cwd = resolve(root, 'site')
  for (const dir of [artifact, cwd]) mkdirSync(dir)
  equal(fullCwdCheck(cwd, artifact), undefined)
  mkdirSync(resolve(cwd, 'functions'))
  throws(() => fullCwdCheck(cwd, artifact), { message: 'The deploy directory holds functions/ and the artifact has no _worker.js: Wrangler would deploy the base branch’s '
    + 'Functions under the PR’s front end. Build the PR’s Functions into the artifact (`wrangler pages functions build functions --outdir <artifact>/_worker.js`)' })
  writeFileSync(resolve(artifact, '_worker.js'), 'export default {}')
  equal(fullCwdCheck(cwd, artifact), undefined)
  const api = pagesApi()
  rmSync(resolve(artifact, '_worker.js'))
  await rejects(publishFull({ cf: api.cf, plan, artifact, cwd, upload: api.upload }), { message: /^The deploy directory holds functions\// })
  equal(api.uploads, [])
})

test('the trusted Wrangler runs as argv in the trusted directory, with the artifact after `pages deploy`', async t => {
  const root = scratch(t, 'cf-preview-argv')
  const out = resolve(root, 'argv.json')
  const script = `require('fs').writeFileSync(${JSON.stringify(out)}, JSON.stringify([process.cwd(), process.argv.slice(1), process.env.CLOUDFLARE_API_TOKEN, process.env.CLOUDFLARE_ACCOUNT_ID]))`
  const upload = wranglerUpload([process.execPath, '-e', script], { artifact: '/built/dist', cwd: root, token: 'deploy-token', account })
  await upload(['pages', 'deploy', '--branch', 'pr-12', '--project-name', 'site', '--commit-hash', sha, '--commit-message', plan.owner, '--commit-dirty=false'])
  equal(JSON.parse(readFileSync(out, 'utf8')), [root,
    ['pages', 'deploy', '/built/dist', '--branch', 'pr-12', '--project-name', 'site', '--commit-hash', sha, '--commit-message', plan.owner, '--commit-dirty=false'],
    'deploy-token', account])
  throws(() => wranglerUpload('wrangler pages deploy', { artifact: '/built/dist', cwd: root }), { message: 'Expected trusted Wrangler argv' })
})

test('full retire deletes only pr-N’s owned deployments, never production or dev; a repeat is a no-op', async () => {
  let live = true
  const deleted = []
  const cf = async (method, path) => {
    if (method === 'DELETE') { deleted.push(path); return null }
    if (path === projectPath) return project
    if (path.startsWith(`${projectPath}/deployments?`)) return envelope([...(live ? [deployment(1), deployment(2)] : []), devDeployment])
    throw new Error(path)
  }
  equal(await retireFull({ cf, plan }), { ...plan, retired: true, removed_deployments: ['dep1', 'dep2'] })
  equal(deleted, [`${projectPath}/deployments/dep1?force=true`, `${projectPath}/deployments/dep2?force=true`])
  live = false
  equal(await retireFull({ cf, plan }), { ...plan, retired: true, removed_deployments: [] })
  equal(deleted.length, 2)
})

test('full retire refuses, before any delete, anything it does not own or that is production or dev', async () => {
  const cases = [
    [project, [deployment(1), deployment(2, { commit_message: 'manual' })], 'Unowned or production deployment in preview namespace'],
    [project, [deployment(1, {}, { id: 'prod' })], 'Unowned or production deployment in preview namespace'],
    [project, [deployment(1, {}, { environment: 'production' })], 'Unowned or production deployment in preview namespace'],
    [project, [deployment(1), deployment(2, {}, { aliases: ['https://dev.site.pages.dev'] })], 'Preview deployment holds a production or protected alias'],
    [{ ...project, production_branch: 'pr-12' }, [deployment(1)], 'Refusing a production or protected branch'],
  ]
  for (const [projectDoc, rows, message] of cases) {
    const deleted = []
    const cf = async (method, path) => {
      if (method === 'DELETE') { deleted.push(path); return null }
      return path === projectPath ? projectDoc : envelope(rows)
    }
    await rejects(retireFull({ cf, plan }), { message })
    equal(deleted, [])
  }
})

test('the probe reads the captured SHA from the pr-N host’s build header; another build, a redirect or another origin cannot pass', async () => {
  const calls = [], waits = []
  const responses = [
    { status: 200, headers: new Headers({ 'x-disky-build': `${'b'.repeat(40)} 2026-11-10T00:00:00Z` }) },
    { status: 302, headers: new Headers({ 'x-disky-build': `${sha} 2026-11-11T00:00:00Z` }) },
    { status: 401, headers: new Headers({ 'x-disky-build': `${sha} 2026-11-11T00:00:00Z` }) },
  ]
  equal(await verifyRevision(`https://${plan.hostname}`, verification, sha, {
    fetcher: async (url, options) => { calls.push([url, options.method, options.redirect]); return responses.shift() },
    wait: async ms => waits.push(ms),
  }), 401)
  equal(calls, Array(3).fill(['https://pr-12.site.pages.dev/api/health', 'GET', 'manual']))
  equal(waits, [10000, 10000])
  equal(await verifyRevision(`https://${plan.hostname}`, { path: '/__revision' }, sha, { fetcher: async () => ({ status: 200, text: async () => `${sha}\n` }) }), 200)
  await rejects(verifyRevision(`https://${plan.hostname}`, verification, sha, {
    fetcher: async () => ({ status: 200, headers: new Headers() }), wait: async () => {}, attempts: 2,
  }), { message: 'Preview host did not serve the captured full source SHA within the bounded wait' })
  for (const path of ['//elsewhere.example/revision', '/\\elsewhere', 'https://elsewhere.example/']) {
    throws(() => revisionProbe({ path }), { message: 'An explicit same-origin revision verification path is required' })
  }
})

test('provider errors expose status and route, never the response body or token', async () => {
  const client = apiClient('https://api.cloudflare.com/client/v4', () => 'fake-secret', async () => ({ ok: false, status: 403, json: () => ({ errors: ['fake-secret'] }) }))
  await rejects(client('GET', '/projects/site'), { message: 'GET /projects/site: HTTP 403' })
})

test('a PR artifact cannot use symlinks to upload files outside its build', async t => {
  const root = scratch(t, 'cf-preview-artifact')
  writeFileSync(resolve(root, 'index.html'), '<html></html>')
  equal(validateArtifact(root), undefined)
  symlinkSync('../outside', resolve(root, 'escaped'))
  throws(() => validateArtifact(root), { message: 'Artifact contains a symlink or special file' })
})

// Workers native Previews: `stack`'s candidate provider, which no implemented mode reaches.
test('a Workers plan keeps the custom-domain native Preview naming', () => {
  equal(workerPlan, { protocol: 'cf-preview-v1', repository: 'org/repo', target: 'main', pr: 12, sha, provider: 'workers', account,
    project: 'site', owner: 'cf-preview-v1:org/repo#12:site', zone, preview: 'pr12', hostname: 'pr12.preview.example.org' })
})

test('Workers preview config excludes all production routing/hooks and pins the owner marker', () => {
  const template = { main: 'edge/worker.js', assets: { directory: 'www/dist' }, vars: { APP: 'demo' }, compatibility_date: '2026-11-10', routes: ['prod.example/*'], triggers: { crons: ['* * * * *'] }, build: { command: 'not-run' } }
  equal(workerConfig(template, workerPlan, '/built'), { name: 'site', account_id: account, main: '/built/edge/worker.js', find_additional_modules: true, no_bundle: true,
    compatibility_date: '2026-11-10', assets: { directory: '/built/www/dist' }, previews: { vars: { APP: 'demo', CF_PREVIEW_OWNER: workerPlan.owner } } })
  throws(() => workerConfig({ main: '../outside.js' }, workerPlan, '/built'), { message: 'Worker entry must be relative to the artifact' })
  throws(() => workerConfig({ main: 'worker.js', assets: { directory: '../outside' } }, workerPlan, '/built'), { message: 'Assets must be relative to the artifact' })
  throws(() => workerConfig({ main: 'worker.js', durable_objects: {} }, workerPlan, '/built'), { message: 'Unsupported preview template fields: durable_objects' })
})

test('Worker publication uses native Previews without modifying parent domains or inheriting Base settings', async t => {
  const workDir = scratch(t, 'cf-preview-worker')
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
  equal(await publishWorker({ cf, plan: workerPlan, template: { main: 'worker.js', compatibility_date: '2026-11-10' }, artifact: '/built', workDir,
    upload: async argv => { uploads.push(argv); uploaded = true } }), { ...workerPlan, preview_id: 'preview12', deployment_id: 'w-dep12', url: 'https://pr12.preview.example.org' })
  equal(uploads, [['preview', '--config', resolve(workDir, 'worker.json'), '--worker-name', 'site', '--name', 'pr12', '--ignore-base-config', '--tag', sha, '--message', workerPlan.owner]])
  equal(writes, [])
  equal(JSON.parse(readFileSync(resolve(workDir, 'worker.json'), 'utf8')), {
    name: 'site', account_id: account, main: '/built/worker.js', find_additional_modules: true, no_bundle: true,
    compatibility_date: '2026-11-10', previews: { vars: { CF_PREVIEW_OWNER: workerPlan.owner } },
  })
})

test('Worker close removes only its owned native Preview; a repeated close is a no-op; a mismatched slug is refused', async () => {
  const deleted = []
  const cf = async (method, path) => {
    if (method === 'DELETE') { deleted.push(path); return null }
    if (path === workerPath) return workerPreview
    if (path === `${workerPath}/deployments/latest`) return workerDeployment
    throw new Error(path)
  }
  equal(await retireWorker({ cf, plan: workerPlan }), { ...workerPlan, retired: true, removed_preview: 'preview12' })
  equal(deleted, [workerPath])
  equal(await retireWorker({ cf: async () => null, plan: workerPlan }), { ...workerPlan, retired: true, removed_preview: null })
  await rejects(retireWorker({ cf: async (_, p) => p === workerPath ? workerPreview : { annotations: {} }, plan: workerPlan }), { message: 'Refusing an unowned native Worker Preview' })
  const writes = []
  await rejects(retireWorker({ cf: async method => { if (method !== 'GET') writes.push(method); return { ...workerPreview, slug: 'production' } }, plan: workerPlan }),
    { message: 'Unexpected native Preview name/slug' })
  equal(writes, [])
})
