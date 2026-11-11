import { execFileSync } from 'node:child_process'
import { cpSync, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { DEFAULT_ALLOW, DEFAULT_DENY, DEFAULT_FORWARD, REVISION_PATH, TOKEN_VAR } from './layer.mjs'

const LAYER_SOURCE = join(dirname(fileURLToPath(import.meta.url)), 'layer.mjs')

export const PROTOCOL = 'cf-preview-v1'
const WRITERS = new Set(['write', 'maintain', 'admin'])
export function requireThat(condition, message) {
  if (!condition) throw new Error(message)
}

/** No credentials or source-controlled commands belong in this plan. Config is read from the trusted base checkout. */
export function previewPlan(config, target, number, sha) {
  requireThat(Number.isSafeInteger(number) && number > 0, 'Invalid PR number')
  requireThat(/^[0-9a-f]{40}$/.test(sha), 'Expected full head SHA')
  requireThat(/^[\w.-]+\/[\w.-]+$/.test(config.repository), 'Invalid repository')
  const selected = config.targets[target]
  requireThat(selected && ['pages', 'workers'].includes(selected.provider), 'Unknown preview target/provider')
  requireThat(/^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$/.test(selected.domain) && !selected.domain.includes('..'), 'Invalid preview base domain')
  requireThat(/^[a-f0-9]{32}$/.test(selected.account_id), 'Invalid account ID')
  requireThat(/^[a-z0-9][a-z0-9-]{0,45}$/.test(selected.project), 'Invalid project namespace')
  requireThat(/^[a-f0-9]{32}$/.test(selected.zone_id), 'Invalid zone ID')
  return {
    protocol: PROTOCOL, repository: config.repository, target, pr: number, sha,
    provider: selected.provider, account: selected.account_id, zone: selected.zone_id,
    project: selected.project, branch: `pr-${number}`, preview: `pr${number}`,
    hostname: `pr${number}.${selected.domain}`, owner: `${PROTOCOL}:${config.repository}#${number}:${selected.project}`,
  }
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

export function dnsClaim(plan, record) {
  return record?.name === plan.hostname && record.type === 'CNAME' && record.proxied === true
    && record.content === `${plan.branch}.${plan.project}.pages.dev` && record.comment === plan.owner
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

export async function pagesDeployments(cf, plan) {
  return cfList(cf, `/accounts/${plan.account}/pages/projects/${plan.project}/deployments?env=preview`)
}

export function ownedPagesDeployments(plan, project, deployments) {
  requireThat(project.production_branch !== plan.branch, 'Refusing production branch cleanup')
  const selected = deployments.filter(d => d.deployment_trigger.metadata.branch === plan.branch)
  requireThat(selected.every(d => d.environment === 'preview' && d.id !== project.canonical_deployment?.id
    && d.deployment_trigger.metadata.commit_message === plan.owner), 'Unowned or production deployment in preview namespace')
  return selected
}

export async function publishPages({ cf, plan, upload }) {
  const projectPath = `/accounts/${plan.account}/pages/projects/${plan.project}`
  const project = await cf('GET', projectPath)
  requireThat(project.production_branch !== plan.branch, 'Preview branch is production')
  ownedPagesDeployments(plan, project, await pagesDeployments(cf, plan))
  const dnsPath = `/zones/${plan.zone}/dns_records`
  const records = await cfList(cf, `${dnsPath}?name=${encodeURIComponent(plan.hostname)}`)
  requireThat(records.length <= 1 && records.every(r => dnsClaim(plan, r)), 'Preview hostname has an unowned DNS record')
  const existingDomain = await cf('GET', `${projectPath}/domains/${plan.hostname}`, undefined, { missing: true })
  requireThat(!existingDomain || records.length === 1, 'Existing custom domain has no ownership claim')
  await upload(['pages', 'deploy', '--branch', plan.branch, '--project-name', plan.project,
    '--commit-hash', plan.sha, '--commit-message', plan.owner, '--commit-dirty=false'])
  const deployments = await pagesDeployments(cf, plan)
  const deployment = deployments.find(d => d.deployment_trigger.metadata.branch === plan.branch
    && d.deployment_trigger.metadata.commit_hash === plan.sha && d.deployment_trigger.metadata.commit_message === plan.owner)
  requireThat(deployment?.environment === 'preview' && deployment.latest_stage.status === 'success', 'Preview deployment readback failed')
  const record = records[0] ?? await cf('POST', dnsPath, {
    type: 'CNAME', name: plan.hostname, content: `${plan.branch}.${plan.project}.pages.dev`,
    proxied: true, ttl: 1, comment: plan.owner,
  })
  const domain = existingDomain ?? await cf('POST', `${projectPath}/domains`, { name: plan.hostname })
  return { ...plan, deployment_id: deployment.id, dns_id: record.id, domain_id: domain.id, url: `https://${plan.hostname}` }
}

export async function retirePages({ cf, plan }) {
  const projectPath = `/accounts/${plan.account}/pages/projects/${plan.project}`
  const project = await cf('GET', projectPath)
  const deployments = ownedPagesDeployments(plan, project, await pagesDeployments(cf, plan))
  const records = await cfList(cf, `/zones/${plan.zone}/dns_records?name=${encodeURIComponent(plan.hostname)}`)
  requireThat(records.length <= 1 && records.every(r => dnsClaim(plan, r)), 'Refusing unowned DNS cleanup')
  const domain = await cf('GET', `${projectPath}/domains/${plan.hostname}`, undefined, { missing: true })
  // An already removed DNS record cannot prove ownership of a remaining domain. Delete domain first, then DNS.
  requireThat(!domain || records.length === 1, 'Custom domain has no ownership claim; reconcile before cleanup')
  if (domain) await cf('DELETE', `${projectPath}/domains/${plan.hostname}`)
  if (records[0]) await cf('DELETE', `/zones/${plan.zone}/dns_records/${records[0].id}`)
  for (const deployment of deployments) await cf('DELETE', `${projectPath}/deployments/${deployment.id}?force=true`)
  return { ...plan, retired: true, removed_deployments: deployments.map(d => d.id), removed_dns: records.map(r => r.id), removed_domain: domain?.id ?? null }
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

/** Preview modes (specs/cf-pr-previews.md § Modes). Only `ui` is implemented; the others are specified. */
export const MODES = ['ui', 'stack', 'pipeline']
export const IMPLEMENTED_MODES = ['ui']
export const DEFAULT_MODE = 'ui'
const MODE_LABEL = /^preview:(.*)$/

/** An explicit `/preview <mode>` (or dispatch input) wins for its run; else one `preview:<mode>` label; else `ui`. */
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

const bareHttpsOrigin = value => {
  let url
  try { url = new URL(value) } catch { return false }
  return url.protocol === 'https:' && value.replace(/\/$/, '') === url.origin
}
const rules = (value, fallback, name) => {
  const list = value ?? fallback
  requireThat(Array.isArray(list) && list.every(rule => typeof rule === 'string' && rule.startsWith('/') && !rule.includes('..')),
    `Invalid ui ${name} rules`)
  return list
}

/** A `ui` target: the base target with its `modes.ui` overrides, and the read-only layer's settings, validated. */
export function uiSettings(target) {
  const ui = target?.modes?.ui
  requireThat(ui && typeof ui === 'object', 'Target has no ui mode configured')
  requireThat(target.provider === 'pages', 'ui mode is implemented for Pages targets only')
  requireThat(typeof ui.project === 'string' && ui.project !== target.project, 'ui mode needs its own dedicated Pages project')
  requireThat(bareHttpsOrigin(ui.upstream), 'ui upstream must be a bare https origin')
  const viewers = ui.viewers
  if (viewers !== 'public') {
    requireThat(viewers && /^[a-z0-9-]+\.cloudflareaccess\.com$/.test(viewers.access?.team_domain ?? '')
      && /^[a-f0-9]{64}$/.test(viewers.access?.aud ?? ''), 'ui viewers need a Cloudflare Access team domain and AUD tag, or "public"')
    const lists = [viewers.emails ?? [], viewers.domains ?? []]
    requireThat(lists.every(list => Array.isArray(list) && list.every(x => typeof x === 'string' && x.length > 0 && x === x.toLowerCase()))
      && lists.some(list => list.length > 0), 'ui viewers need a lower-case email or domain allow list')
  }
  const { modes: _modes, verification: _verification, ...base } = target
  return {
    target: {
      ...base, project: ui.project, domain: ui.domain ?? target.domain,
      wrangler: ui.wrangler ?? target.wrangler, working_directory: ui.working_directory ?? target.working_directory,
      verification: { path: REVISION_PATH },
    },
    layer: {
      upstream: ui.upstream.replace(/\/$/, ''),
      viewers: viewers === 'public' ? 'public'
        : { access: { team_domain: viewers.access.team_domain, aud: viewers.access.aud }, emails: viewers.emails ?? [], domains: viewers.domains ?? [] },
      forward: rules(ui.forward, DEFAULT_FORWARD, 'forward'),
      deny: rules(ui.deny, DEFAULT_DENY, 'deny'),
      allow: rules(ui.allow, DEFAULT_ALLOW, 'allow'),
    },
  }
}

/** One mode's plan for one target: the provider-neutral plan of its effective target, plus the mode. */
export function modePlan(config, target, number, sha, mode) {
  requireImplemented(mode)
  requireThat(config.targets[target], 'Unknown preview target/provider')
  const { target: effective } = uiSettings(config.targets[target])
  return { ...previewPlan({ repository: config.repository, targets: { [target]: effective } }, target, number, sha), mode }
}

// Pages preview settings that are not bindings. Anything else non-empty in the project's preview config is one.
const PAGES_SETTINGS = new Set(['env_vars', 'compatibility_date', 'compatibility_flags', 'fail_open', 'always_use_latest_compatibility_date',
  'build_image_major_version', 'usage_model', 'placement', 'limits', 'wrangler_config_hash'])
const empty = value => value === null || value === undefined || (typeof value === 'object' && Object.keys(value).length === 0)
const describe = vars => `[${vars.map(([name, type]) => `${name} (${type})`).join(', ')}]`

/** A `ui` project's preview environment holds no binding, and no variable but the read grant (nothing when public). */
export function uiProjectCheck(project, layer) {
  const preview = project.deployment_configs?.preview ?? {}
  const bindings = Object.keys(preview).filter(key => !PAGES_SETTINGS.has(key) && !empty(preview[key])).sort()
  requireThat(bindings.length === 0, `ui project has preview bindings: ${bindings.join(', ')}`)
  const actual = Object.entries(preview.env_vars ?? {}).filter(([, value]) => value !== null).map(([name, value]) => [name, value.type]).sort()
  const expected = layer.viewers === 'public' ? [] : [[TOKEN_VAR, 'secret_text']]
  requireThat(JSON.stringify(actual) === JSON.stringify(expected),
    `ui project preview variables must be exactly ${describe(expected)}, found ${describe(actual)}`)
}

/** Neither Pages Functions nor a wrangler config may be picked up from the directory Wrangler runs in. */
export function uiCwdCheck(cwd) {
  const found = ['functions', 'wrangler.toml', 'wrangler.json', 'wrangler.jsonc'].filter(name => existsSync(join(cwd, name)))
  requireThat(found.length === 0, `ui deploy directory must not hold ${found.join(', ')}`)
}

/** The preview's `_worker.js`: the trusted layer's source with this publish's settings, nothing from the PR. */
export function uiWorker(layer, plan) {
  const config = { ...layer, sha: plan.sha, pr: plan.pr, hostname: plan.hostname }
  return `${readFileSync(LAYER_SOURCE, 'utf8')}\nconst CONFIG = ${JSON.stringify(config)}\nexport default { fetch: (request, env) => handle(request, env, CONFIG) }\n`
}

export const UI_ROUTES = { version: 1, include: ['/*'], exclude: [] }

/** Copy the PR's static build, replacing whatever worker/routing it carried with the layer's. Returns what was replaced. */
export function stageUi({ artifact, staging, layer, plan }) {
  validateArtifact(artifact)
  rmSync(staging, { recursive: true, force: true })
  cpSync(artifact, staging, { recursive: true })
  const replaced = ['_worker.js', '_routes.json'].filter(name => existsSync(join(staging, name)))
  for (const name of replaced) rmSync(join(staging, name), { recursive: true, force: true })
  writeFileSync(join(staging, '_worker.js'), uiWorker(layer, plan))
  writeFileSync(join(staging, '_routes.json'), `${JSON.stringify(UI_ROUTES)}\n`)
  return replaced
}

/** `ui` publish: the dedicated project is binding-free, the staged build carries only the layer, then the Pages lifecycle. */
export async function publishUi({ cf, plan, layer, artifact, workDir, cwd, uploader }) {
  uiCwdCheck(cwd)
  uiProjectCheck(await cf('GET', `/accounts/${plan.account}/pages/projects/${plan.project}`), layer)
  const staging = join(workDir, 'ui-site')
  const replaced = stageUi({ artifact, staging, layer, plan })
  const receipt = await publishPages({ cf, plan, upload: uploader(staging) })
  return { ...receipt, mode: 'ui', upstream: layer.upstream, viewers: layer.viewers === 'public' ? 'public' : 'access', replaced }
}

export async function retireUi({ cf, plan }) {
  return { ...await retirePages({ cf, plan }), mode: 'ui' }
}
