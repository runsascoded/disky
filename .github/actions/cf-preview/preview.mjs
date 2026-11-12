import { execFileSync } from 'node:child_process'
import { existsSync, lstatSync, mkdirSync, readdirSync, renameSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, resolve } from 'node:path'

export const PROTOCOL = 'cf-preview-v1'
const WRITERS = new Set(['write', 'maintain', 'admin'])
/** Pages branches a preview never deploys to or deletes from, beside the project's production branch. */
export const PROTECTED_BRANCHES = ['dev']

export function requireThat(condition, message) {
  if (!condition) throw new Error(message)
}

/** The Cloudflare account: the target's `account_id` or the action's `account-id` input (a public repository keeps it
 *  out of tracked config, `vars.CLOUDFLARE_ACCOUNT_ID`), exactly one of them, 32 hex. Gate needs none (null). */
export function accountFor(selected, given, required = true) {
  const sources = [selected.account_id, given].filter(value => value !== undefined && value !== null && value !== '')
  if (!required && sources.length === 0) return null
  requireThat(sources.length === 1, 'Account ID must come from exactly one of the target config and the account-id input')
  requireThat(/^[a-f0-9]{32}$/.test(sources[0]), 'Invalid account ID')
  return sources[0]
}

/** No credentials or source-controlled commands belong in this plan. Config is read from the trusted base checkout.
 *  Pages: the reserved branch `pr-N` of the deployment's own project, served at `pr-N.<project subdomain>`.
 *  Workers (no mode reaches it yet): a native Preview `prN` under the parent's custom domain.
 *  `account`: the `account-id` input; `requireAccount` false (gate) lets both sources be absent. */
export function previewPlan(config, target, number, sha, { account: given = '', requireAccount = true } = {}) {
  requireThat(Number.isSafeInteger(number) && number > 0, 'Invalid PR number')
  requireThat(/^[0-9a-f]{40}$/.test(sha), 'Expected full head SHA')
  requireThat(/^[\w.-]+\/[\w.-]+$/.test(config.repository), 'Invalid repository')
  const selected = config.targets[target]
  requireThat(selected && ['pages', 'workers'].includes(selected.provider), 'Unknown preview target/provider')
  const account = accountFor(selected, given, requireAccount)
  requireThat(/^[a-z0-9][a-z0-9-]{0,45}$/.test(selected.project), 'Invalid project namespace')
  const base = {
    protocol: PROTOCOL, repository: config.repository, target, pr: number, sha,
    provider: selected.provider, account, project: selected.project,
    owner: `${PROTOCOL}:${config.repository}#${number}:${selected.project}`,
  }
  if (selected.provider === 'pages') {
    const subdomain = selected.subdomain ?? `${selected.project}.pages.dev`
    requireThat(/^[a-z0-9][a-z0-9-]*\.pages\.dev$/.test(subdomain), 'Invalid Pages subdomain')
    const extra = selected.protected_branches ?? []
    requireThat(Array.isArray(extra) && extra.every(b => typeof b === 'string' && b), 'Invalid protected branches')
    const branch = `pr-${number}`
    const protectedBranches = [...new Set([...PROTECTED_BRANCHES, ...extra])]
    requireThat(!protectedBranches.includes(branch), `Preview branch ${branch} is protected`)
    return { ...base, branch, subdomain, protected_branches: protectedBranches, hostname: `${branch}.${subdomain}` }
  }
  requireThat(/^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$/.test(selected.domain) && !selected.domain.includes('..'), 'Invalid preview base domain')
  requireThat(/^[a-f0-9]{32}$/.test(selected.zone_id), 'Invalid zone ID')
  return { ...base, zone: selected.zone_id, preview: `pr${number}`, hostname: `pr${number}.${selected.domain}` }
}

/** A collaborator's approval authorizes one captured head. Later external updates require another approval. */
export async function authorize({ github, repository, target, number, sha, actor, manual = false, closed = false }) {
  const pr = await github('GET', `/repos/${repository}/pulls/${number}`)
  requireThat(pr.base.repo.full_name === repository, 'PR targets a different repository')
  if (closed) {
    requireThat(pr.state === 'closed', 'Cleanup requires a closed PR')
    return pr
  }
  requireThat(pr.base.ref === target, 'PR targets a different branch')
  requireThat(pr.state === 'open' && !pr.draft, 'Expected open, non-draft PR')
  requireThat(pr.head.sha === sha, 'PR head changed; rebuild or approve the new head')
  const login = manual ? actor : pr.user.login
  const permission = await github('GET', `/repos/${repository}/collaborators/${encodeURIComponent(login)}/permission`, undefined, { missing: true })
  requireThat(WRITERS.has(permission?.permission), 'A current write collaborator must approve this head')
  return pr
}

/** Native Preview under an existing parent. Only the explicit template supplies runtime bindings. */
export function workerConfig(template, plan, artifact) {
  const globals = ['compatibility_date', 'compatibility_flags', 'rules']
  const bindings = ['r2_buckets', 'd1_databases', 'kv_namespaces', 'services', 'observability', 'limits', 'vars']
  const allowed = [...globals, ...bindings]
  const ignored = ['name', 'account_id', 'workers_dev', 'find_additional_modules', 'routes', 'route', 'build', 'triggers', 'main', 'assets']
  const unknown = Object.keys(template).filter(key => !allowed.includes(key) && !ignored.includes(key))
  requireThat(unknown.length === 0, `Unsupported preview template fields: ${unknown.join(', ')}`)
  requireThat(template.main && !isAbsolute(template.main) && !template.main.split(/[\\/]/).includes('..'), 'Worker entry must be relative to the artifact')
  requireThat(!template.assets || (template.assets.directory && !isAbsolute(template.assets.directory) && !template.assets.directory.split(/[\\/]/).includes('..')), 'Assets must be relative to the artifact')
  const pick = keys => Object.fromEntries(keys.filter(key => template[key] !== undefined).map(key => [key, template[key]]))
  return {
    ...pick(globals), name: plan.project, account_id: plan.account, main: resolve(artifact, template.main),
    find_additional_modules: true, no_bundle: true,
    ...(template.assets ? { assets: { ...template.assets, directory: resolve(artifact, template.assets.directory) } } : {}),
    previews: { ...pick(bindings), vars: { ...template.vars, CF_PREVIEW_OWNER: plan.owner } },
  }
}

/** Deployment input is data: refuse symlinks/devices before credentialed tooling can traverse them. */
export function validateArtifact(root) {
  requireThat(lstatSync(root).isDirectory(), 'Artifact must be a directory')
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    requireThat(!entry.isSymbolicLink() && (entry.isFile() || entry.isDirectory()), 'Artifact contains a symlink or special file')
    if (entry.isDirectory()) validateArtifact(join(root, entry.name))
  }
}

/** Paginated CF responses. Never silently truncate cleanup to the first deployment page. */
export async function cfList(cf, path) {
  const result = []
  for (let page = 1; page <= 1000; page++) {
    const envelope = await cf('GET', `${path}${path.includes('?') ? '&' : '?'}per_page=25&page=${page}`, undefined, { envelope: true })
    requireThat(Array.isArray(envelope.result), 'Expected a paginated Cloudflare list')
    result.push(...envelope.result)
    if (envelope.result_info?.total_pages ? page >= envelope.result_info.total_pages : envelope.result.length < 25) return result
  }
  throw new Error('Cloudflare pagination exceeded its explicit bound')
}

const pagesProjectPath = plan => `/accounts/${plan.account}/pages/projects/${plan.project}`

export async function pagesDeployments(cf, plan) {
  return cfList(cf, `${pagesProjectPath(plan)}/deployments?env=preview`)
}

/** The deployment's project, as the plan expects it: serving the plan's subdomain, with `pr-N` neither its production
 *  branch nor protected. */
export function checkPagesProject(plan, project) {
  requireThat(project.subdomain === plan.subdomain,
    `Pages project ${plan.project} serves ${project.subdomain}, not ${plan.subdomain}; set the target's subdomain`)
  requireThat(project.production_branch !== plan.branch && !plan.protected_branches.includes(plan.branch),
    'Refusing a production or protected branch')
}

/** The `pr-N` branch's deployments, every one of which must be this tool's (owner marker), a preview, not the canonical
 *  deployment, and not holding the production or a protected branch's alias. Other branches' deployments are ignored. */
export function ownedPagesDeployments(plan, project, deployments) {
  checkPagesProject(plan, project)
  const reserved = [project.production_branch, ...plan.protected_branches].map(branch => `https://${branch}.${plan.subdomain}`)
  const selected = deployments.filter(d => d.deployment_trigger.metadata.branch === plan.branch)
  requireThat(selected.every(d => d.environment === 'preview' && d.id !== project.canonical_deployment?.id
    && d.deployment_trigger.metadata.commit_message === plan.owner), 'Unowned or production deployment in preview namespace')
  requireThat(selected.every(d => !(d.aliases ?? []).some(alias => reserved.includes(alias.replace(/\/$/, '')))),
    'Preview deployment holds a production or protected alias')
  return selected
}

/** Neither the base branch's Functions nor anything else may stand in for the PR's: Wrangler builds `functions/` from
 *  the directory it runs in unless the artifact carries its own `_worker.js` (file or directory). */
export function fullCwdCheck(cwd, artifact) {
  requireThat(!existsSync(join(cwd, 'functions')) || existsSync(join(artifact, '_worker.js')),
    'The deploy directory holds functions/ and the artifact has no _worker.js: Wrangler would deploy the base branch’s '
    + 'Functions under the PR’s front end. Build the PR’s Functions into the artifact '
    + '(`wrangler pages functions build functions --outdir <artifact>/_worker.js`)')
}

/** Where the deploy directory's `functions/` waits while Wrangler uploads (`withoutFunctions`). */
export const HELD_FUNCTIONS = '.functions.cf-preview-held'

/** Run `run` with the trusted deploy directory's `functions/` moved aside, restored after, success or not. `wrangler pages
 *  deploy` bundles the CWD's `functions/` even when the artifact carries its own `_worker.js`: from a fresh base checkout it
 *  fails (an ungenerated import, `./build.gen.js`), and otherwise it would ship the base branch's Functions. */
export async function withoutFunctions(cwd, run) {
  const dir = join(cwd, 'functions'), held = join(cwd, HELD_FUNCTIONS)
  if (!existsSync(dir)) return run()
  requireThat(!existsSync(held), `${HELD_FUNCTIONS} already exists in the deploy directory; restore it to functions/ first`)
  renameSync(dir, held)
  try {
    return await run()
  } finally {
    renameSync(held, dir)
  }
}

/** `full` publish: the PR's whole build as the `pr-N` branch of the deployment's own Pages project, so it runs on that
 *  project's preview environment (the dev stack's bindings and secrets), at `https://pr-N.<subdomain>`. */
export async function publishFull({ cf, plan, artifact, cwd, upload }) {
  requireThat(plan.provider === 'pages', 'full mode is implemented for Pages targets only')
  validateArtifact(artifact)
  fullCwdCheck(cwd, artifact)
  const project = await cf('GET', pagesProjectPath(plan))
  ownedPagesDeployments(plan, project, await pagesDeployments(cf, plan))
  await withoutFunctions(cwd, () => upload(['pages', 'deploy', '--branch', plan.branch, '--project-name', plan.project,
    '--commit-hash', plan.sha, '--commit-message', plan.owner, '--commit-dirty=false']))
  const deployments = ownedPagesDeployments(plan, project, await pagesDeployments(cf, plan))
  const deployment = deployments.find(d => d.deployment_trigger.metadata.commit_hash === plan.sha)
  requireThat(deployment?.latest_stage.status === 'success', 'Preview deployment readback failed')
  return { ...plan, mode: 'full', deployment_id: deployment.id, url: `https://${plan.hostname}` }
}

/** `full` retire: delete the `pr-N` branch's deployments, all of which must be owned; production, `dev` and every other
 *  branch are untouched. A repeat finds nothing and succeeds. */
export async function retireFull({ cf, plan }) {
  requireThat(plan.provider === 'pages', 'full mode is implemented for Pages targets only')
  const project = await cf('GET', pagesProjectPath(plan))
  const deployments = ownedPagesDeployments(plan, project, await pagesDeployments(cf, plan))
  for (const deployment of deployments) await cf('DELETE', `${pagesProjectPath(plan)}/deployments/${deployment.id}?force=true`)
  return { ...plan, mode: 'full', retired: true, removed_deployments: deployments.map(d => d.id) }
}

const workerPreviewPath = plan => `/accounts/${plan.account}/workers/workers/${plan.project}/previews/${plan.preview}`
const workerOwned = (deployment, plan) => deployment?.annotations?.['workers/message'] === plan.owner

async function existingWorkerPreview(cf, plan) {
  const path = workerPreviewPath(plan)
  const preview = await cf('GET', path, undefined, { missing: true })
  if (!preview) return null
  requireThat(preview.name === plan.preview && preview.slug === plan.preview, 'Unexpected native Preview name/slug')
  const deployment = await cf('GET', `${path}/deployments/latest`)
  requireThat(workerOwned(deployment, plan), 'Refusing an unowned native Worker Preview')
  return { preview, deployment }
}

/** Native Workers Preview (`stack`'s candidate provider; no implemented mode reaches it). */
export async function publishWorker({ cf, plan, upload, template, artifact, workDir }) {
  // Never let Wrangler create a missing parent or alter production routing as a side effect.
  await cf('GET', `/accounts/${plan.account}/workers/workers/${plan.project}`)
  await existingWorkerPreview(cf, plan)
  const domains = (await cfList(cf, `/accounts/${plan.account}/workers/domains`))
    .filter(d => d.hostname === plan.hostname.slice(plan.preview.length + 1))
  requireThat(domains.length === 1 && domains[0].service === plan.project
    && domains[0].zone_id === plan.zone && domains[0].previews_enabled === true,
  'Parent custom domain must already enable native Previews')
  mkdirSync(workDir, { recursive: true })
  const configPath = join(workDir, 'worker.json')
  writeFileSync(configPath, `${JSON.stringify(workerConfig(template, plan, artifact), null, 2)}\n`)
  await upload(['preview', '--config', configPath, '--worker-name', plan.project, '--name', plan.preview,
    '--ignore-base-config', '--tag', plan.sha, '--message', plan.owner])
  const current = await existingWorkerPreview(cf, plan)
  requireThat(current?.deployment.annotations?.['workers/tag'] === plan.sha, 'Worker Preview SHA readback failed')
  const url = `https://${plan.hostname}`
  requireThat(current.preview.urls.some(value => value.replace(/\/$/, '') === url), 'Native Preview did not advertise the configured hostname')
  return { ...plan, preview_id: current.preview.id, deployment_id: current.deployment.id, url }
}

export async function retireWorker({ cf, plan }) {
  const current = await existingWorkerPreview(cf, plan)
  if (current) await cf('DELETE', workerPreviewPath(plan))
  return { ...plan, retired: true, removed_preview: current?.preview.id ?? null }
}

/** Wrangler argv comes from trusted caller config; the PR artifact never supplies a shell command/config. */
export function wranglerUpload(command, { artifact, cwd, token, account }) {
  requireThat(Array.isArray(command) && command.length > 0 && command.every(x => typeof x === 'string' && x), 'Expected trusted Wrangler argv')
  return async args => {
    const argv = args[0] === 'pages' ? [args[0], args[1], artifact, ...args.slice(2)] : args
    execFileSync(command[0], [...command.slice(1), ...argv], {
      cwd, stdio: 'inherit', env: { ...process.env, CLOUDFLARE_API_TOKEN: token, CLOUDFLARE_ACCOUNT_ID: account },
    })
  }
}

/** A reachable host can still serve another build. Require the caller's built revision, same-origin, without redirects. */
export function revisionProbe(verification) {
  requireThat(verification && typeof verification.path === 'string' && verification.path.startsWith('/')
    && !verification.path.startsWith('//') && !verification.path.includes('\\'), 'An explicit same-origin revision verification path is required')
  requireThat(!verification.header || /^[a-z0-9-]+$/i.test(verification.header), 'Invalid revision header')
  return verification
}

/** Preview modes (specs/cf-pr-previews.md § Modes). Only `full` is implemented; the others are specified. */
export const MODES = ['full', 'stack', 'pipeline']
export const IMPLEMENTED_MODES = ['full']
export const DEFAULT_MODE = 'full'
const MODE_LABEL = /^preview:(.*)$/

/** An explicit `/preview <mode>` (or dispatch input) wins for its run; else one `preview:<mode>` label; else `full`. */
export function selectMode({ requested, labels = [] }) {
  const named = [...new Set(labels.map(label => label.match(MODE_LABEL)?.[1]).filter(mode => mode !== undefined))].sort()
  const unknown = named.filter(mode => !MODES.includes(mode))
  requireThat(unknown.length === 0, `Unknown preview mode label: ${unknown.map(mode => `preview:${mode}`).join(', ')}`)
  requireThat(named.length <= 1, `Conflicting preview mode labels: ${named.map(mode => `preview:${mode}`).join(', ')}`)
  if (requested !== undefined) {
    requireThat(MODES.includes(requested), `Unknown preview mode: ${requested}`)
    return { mode: requested, source: 'comment' }
  }
  return named.length ? { mode: named[0], source: 'label' } : { mode: DEFAULT_MODE, source: 'default' }
}

export function requireImplemented(mode) {
  requireThat(IMPLEMENTED_MODES.includes(mode), `Preview mode ${mode} is specified but not implemented (specs/cf-pr-previews.md)`)
}

/** One mode's plan for one target, checked at gate: an implemented mode, a Pages target, an explicit revision probe. */
export function modePlan(config, target, number, sha, mode, account = {}) {
  requireImplemented(mode)
  const selected = config.targets[target]
  requireThat(selected, 'Unknown preview target/provider')
  requireThat(selected.provider === 'pages', 'full mode is implemented for Pages targets only')
  revisionProbe(selected.verification)
  return { ...previewPlan(config, target, number, sha, account), mode }
}
