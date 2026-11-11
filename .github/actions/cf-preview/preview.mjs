import { execFileSync } from 'node:child_process'
import { lstatSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, resolve } from 'node:path'

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
