#!/usr/bin/env node
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  PROTOCOL, authorize, previewPlan, publishPages, publishWorker, requireThat,
  retirePages, retireWorker, validateArtifact, wranglerUpload,
} from './preview.mjs'

const input = name => process.env[`PREVIEW_${name.toUpperCase()}`] || ''
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

export function apiClient(origin, tokenForPath, fetcher = fetch) {
  return async (method, path, body, options = {}) => {
    const response = await fetcher(`${origin}${path}`, {
      method, redirect: 'error', headers: { Authorization: `Bearer ${tokenForPath(path)}`, Accept: 'application/json', 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(30000),
    })
    if (response.status === 404 && options.missing) return null
    requireThat(response.ok, `${method} ${path}: HTTP ${response.status}`)
    if (response.status === 204) return null
    const doc = await response.json()
    requireThat(origin !== 'https://api.cloudflare.com/client/v4' || doc.success === true, `${method} ${path}: Cloudflare request failed`)
    return origin === 'https://api.cloudflare.com/client/v4' && !options.envelope ? doc.result : doc
  }
}

export function eventRequest(event, eventName, actor) {
  if (eventName === 'issue_comment') {
    const command = event.comment.body.trim().match(/^\/preview(?:\s+([0-9a-f]{40}))?$/i)
    requireThat(event.issue.pull_request && command, 'Not a preview command on a PR')
    return { number: event.issue.number, requestedSha: command[1]?.toLowerCase(), actor, manual: true }
  }
  requireThat(['pull_request_target', 'pull_request', 'workflow_dispatch'].includes(eventName), 'Unsupported preview event')
  return { number: Number(event.pull_request?.number ?? event.inputs?.pr), actor, manual: eventName === 'workflow_dispatch' }
}

async function record(github, plan, receipt, state, runUrl) {
  const env = `preview/${plan.target}/pr-${plan.pr}`
  const deployment = await github('POST', `/repos/${plan.repository}/deployments`, {
    ref: plan.sha, auto_merge: false, required_contexts: [], production_environment: false, transient_environment: true,
    environment: env, payload: { ...receipt, protocol: PROTOCOL, run_url: runUrl },
  })
  await github('POST', `/repos/${plan.repository}/deployments/${deployment.id}/statuses`, {
    state, log_url: runUrl, environment_url: receipt.url || '', auto_inactive: state === 'success',
  })
  return deployment.id
}

async function comment(github, plan, receipt, runUrl) {
  const marker = `<!-- ${PROTOCOL}:${plan.target} -->`
  const body = receipt.retired ? `${marker}\nPreview retired after PR closure. [Cleanup receipt](${runUrl}).` : `${marker}\nPreview of \`${plan.sha}\`: ${receipt.url}\n\n[Build and deployment](${runUrl}). Preview runtime data/bindings follow this repository's preview policy.`
  const comments = []
  for (let page = 1; page <= 100; page++) {
    const batch = await github('GET', `/repos/${plan.repository}/issues/${plan.pr}/comments?per_page=100&page=${page}`)
    comments.push(...batch)
    if (batch.length < 100) break
    requireThat(page < 100, 'PR comment pagination exceeded its bound')
  }
  const held = comments.find(c => c.user.login === 'github-actions[bot]' && c.body.startsWith(marker))
  return held
    ? github('PATCH', `/repos/${plan.repository}/issues/comments/${held.id}`, { body })
    : github('POST', `/repos/${plan.repository}/issues/${plan.pr}/comments`, { body })
}

export async function waitForHost(url, { fetcher = fetch, wait = sleep, attempts = 12 } = {}) {
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const response = await fetcher(url, { method: 'HEAD', redirect: 'manual', signal: AbortSignal.timeout(10000) })
      if (response.status >= 200 && response.status < 500) return response.status
    } catch {
      // A newly registered hostname can lack DNS/certificate propagation until the next bounded attempt.
    }
    if (attempt + 1 < attempts) await wait(10000)
  }
  throw new Error('Preview hostname did not become reachable with valid TLS within the bounded wait')
}

/** A reachable hostname can still route to production. Require the caller's built revision, without redirects. */
export function revisionProbe(verification) {
  requireThat(verification && typeof verification.path === 'string' && verification.path.startsWith('/')
    && !verification.path.startsWith('//') && !verification.path.includes('\\'), 'An explicit same-origin revision verification path is required')
  requireThat(!verification.header || /^[a-z0-9-]+$/i.test(verification.header), 'Invalid revision header')
  return verification
}

export async function verifyRevision(origin, verification, sha, { fetcher = fetch, wait = sleep, attempts = 12 } = {}) {
  const { path, header } = revisionProbe(verification)
  const url = new URL(path, origin)
  requireThat(url.origin === new URL(origin).origin, 'Revision probe must remain on the preview origin')
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const response = await fetcher(url.href, { method: 'GET', redirect: 'manual', signal: AbortSignal.timeout(10000), headers: { 'Cache-Control': 'no-cache' } })
      const value = header ? response.headers.get(header)?.trim().split(/\s+/)[0] : (await response.text()).trim()
      if ((response.status >= 200 && response.status < 300 || response.status >= 400 && response.status < 500) && value === sha) return response.status
    } catch {
      // The domain may still be propagating. A bounded retry must still prove the captured revision.
    }
    if (attempt + 1 < attempts) await wait(10000)
  }
  throw new Error('Preview hostname did not serve the captured full source SHA within the bounded wait')
}

export async function main() {
  const configPath = resolve(input('config'))
  const config = JSON.parse(readFileSync(configPath, 'utf8'))
  requireThat(config.repository === process.env.GITHUB_REPOSITORY, 'Config repository differs from the workflow repository')
  const event = JSON.parse(readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'))
  const request = eventRequest(event, process.env.GITHUB_EVENT_NAME, process.env.GITHUB_ACTOR)
  const github = apiClient('https://api.github.com', () => input('github_token'))
  const pr = await github('GET', `/repos/${config.repository}/pulls/${request.number}`)
  const target = input('target') || pr.base.ref
  const sha = input('sha') || request.requestedSha || pr.head.sha
  const plan = previewPlan(config, target, request.number, sha)
  requireThat(!request.requestedSha || request.requestedSha === sha, 'Comment approval and requested SHA differ')
  const operation = input('operation')
  requireThat(['gate', 'publish', 'retire'].includes(operation), 'Unknown preview operation')
  const authorized = await authorize({ github, repository: plan.repository, target, number: request.number, sha,
    actor: request.actor, manual: request.manual, closed: operation === 'retire' })
  const output = (key, value) => {
    if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `${key}=${value}\n`)
  }
  output('sha', plan.sha); output('pr', plan.pr); output('target', target)
  if (authorized.base.sha) output('base_sha', authorized.base.sha)
  output('url', `https://${plan.hostname}`)
  if (operation === 'gate') { console.log(JSON.stringify(plan, null, 2)); return }
  requireThat(input('deploy_token'), 'Missing provider deployment token')
  if (plan.provider === 'pages') requireThat(input('dns_token'), 'Pages custom hostnames need a separate DNS token')
  const cfApi = apiClient('https://api.cloudflare.com/client/v4', path => path.startsWith('/zones/') ? input('dns_token') : input('deploy_token'))
  const selected = config.targets[target]
  if (operation === 'publish') revisionProbe(selected.verification)
  const workDir = resolve(input('work_directory') || `${process.env.RUNNER_TEMP || 'tmp'}/cf-preview-${request.number}`)
  mkdirSync(workDir, { recursive: true })
  const receiptPath = joinReceipt(workDir)
  const progress = { ...plan, operation, status: 'in_progress', mutations: [] }
  const checkpoint = () => writeFileSync(receiptPath, `${JSON.stringify(progress, null, 2)}\n`)
  checkpoint()
  output('receipt', receiptPath)
  const cf = async (method, path, body, options) => {
    const result = await cfApi(method, path, body, options)
    if (method !== 'GET') {
      progress.mutations.push({ method, path, id: result?.id ?? null })
      checkpoint()
    }
    return result
  }
  let receipt
  try {
  if (operation === 'retire') {
    receipt = await (plan.provider === 'pages' ? retirePages : retireWorker)({ cf, plan })
  } else {
    requireThat(input('artifact'), 'Missing built artifact path')
    const artifact = resolve(input('artifact'))
    requireThat(existsSync(artifact), 'Missing built artifact')
    validateArtifact(artifact)
    const upload = wranglerUpload(selected.wrangler, { artifact, cwd: resolve(dirname(configPath), selected.working_directory || '.'),
      token: input('deploy_token'), account: plan.account })
    receipt = plan.provider === 'pages'
      ? await publishPages({ cf, plan, upload })
      : await publishWorker({ cf, plan, upload, artifact, workDir, template: selected.worker_template })
    // A pushed head during upload must not be advertised as the current PR preview.
    await authorize({ github, repository: plan.repository, target, number: request.number, sha,
      actor: request.actor, manual: request.manual })
    receipt.tls_http_status = await verifyRevision(receipt.url, selected.verification, plan.sha)
    receipt.verified_sha = plan.sha
  }
  } catch (error) {
    progress.status = 'failed'
    progress.error = error.message
    checkpoint()
    throw error
  }
  writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`)
  output('receipt', receiptPath)
  const runUrl = `https://github.com/${plan.repository}/actions/runs/${process.env.GITHUB_RUN_ID}`
  receipt.github_deployment_id = await record(github, plan, receipt, operation === 'retire' ? 'inactive' : 'success', runUrl)
  writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`)
  await comment(github, plan, receipt, runUrl)
  console.log(JSON.stringify(receipt, null, 2))
}

const joinReceipt = dir => resolve(dir, 'receipt.json')
if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch(error => { console.error(`cf-preview: ${error.message}`); process.exitCode = 1 })
}
