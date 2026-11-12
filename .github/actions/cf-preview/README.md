# Cloudflare PR preview lifecycle

Source prototype: executable and unit-tested, **not yet a live-verified or published reusable release**. Pin the reviewed provider commit when adopting it. This composite action can be used by another repository as `Open-Athena/disky/.github/actions/cf-preview@<full-sha>`. Node 24 is installed by the action; the caller installs its pinned Wrangler from a trusted base checkout. The design, including what each mode may touch, is [the spec].

The interface is `gate`, `publish`, `retire`. `gate` checks the live PR and current collaborator permission, selects the preview mode, then returns `sha`, `base_sha`, `pr`, `target`, `mode`, and the preview URL. Automatic requests require a current write/maintain/admin PR author; `/preview [<mode>] [<full-head-sha>]` and manual dispatch require a current write collaborator as the actor. An external contributor's later commit needs another approval. `publish` takes gate's `sha` and `mode`, rechecks the captured head before deployment and before reporting success, deploys, and proves the served build is the captured SHA. `retire` requires a closed PR and removes only the `pr-N` branch's owned deployments.

## Modes

| Mode | Deploys | Data | Status |
|---|---|---|---|
| `full` | the PR's whole build (front end + Functions) as branch `pr-N` of the deployment's own Pages project | the project's preview environment: the dev stack's bindings and secrets (production D1/R2 on Disky) | implemented (Pages) |
| `stack` | the PR's whole build | a scratch D1 + R2 per PR | specified only |
| `pipeline` | a job image built from the PR's head, dispatched from GHA | a scratch store prefix | specified only |

Selection: a `/preview <mode>` comment (or the dispatch `mode` input) applies to that run; otherwise exactly one `preview:<mode>` label on the PR; otherwise `full`. Two different mode labels, or an unknown `preview:*` label, fail the gate. `stack` and `pipeline` are refused at the gate until implemented.

A `full` preview is what the deployment's `dev.<host>` already is, for one PR: same project, same preview environment, served at Cloudflare's automatic branch host `https://pr-N.<project subdomain>` (e.g. hf: `https://pr-12.oa-hf-usage.pages.dev`, if that project kept its plain subdomain). There is no custom domain, DNS record, certificate, Access application or forwarding layer, and nothing is created but the branch's deployments. Approving a head (automatically for a write+ author, by `/preview <sha>` otherwise) runs its server code with the dev stack's data and secrets: approve only what you would push to `dev`.

## Trusted configuration

```json
{
  "repository": "your-org/your-repo",
  "targets": {
    "main": {
      "provider": "pages",
      "account_id": "<CF account ID>",
      "project": "your-existing-pages-project",
      "subdomain": "your-existing-pages-project.pages.dev",
      "working_directory": "../site",
      "wrangler": ["node", "node_modules/wrangler/bin/wrangler.js"],
      "verification": { "path": "/api/health", "header": "x-disky-build" }
    }
  }
}
```

Place this in the trusted repository as `.github/cf-preview.json`. Paths are relative to that file. The target key is the permitted PR base branch. Disky can use `gcs`, `cw`, `hf`, `r2`; a shared source PR must explicitly select a preview target rather than inherit an unrelated production config.

- `project`: the deployment's **own** Pages project (the one `dev` and production deploy to).
- `subdomain` (optional, default `<project>.pages.dev`): the project's `pages.dev` subdomain, which Cloudflare suffixes when the name was taken. Publish and retire refuse a project whose actual `subdomain` differs.
- `protected_branches` (optional): branches beside `dev` and the production branch that a preview must never deploy to or delete from. `dev` is always protected.
- `working_directory`: where the trusted Wrangler runs, in the base checkout. Its `wrangler.toml` (if any) is the deployment's own, so `[env.preview]` applies exactly as on `dev`. If it holds `functions/`, the artifact must carry the PR's compiled Functions as `_worker.js` (`wrangler pages functions build functions --outdir dist/_worker.js` in the build job); otherwise publish refuses, since Wrangler would deploy the base branch's Functions under the PR's front end.
- `verification`: required. The probe runs on `https://pr-N.<subdomain>` and must read the captured full SHA, from the body or (with `header`) the header's first token; Disky's Functions stamp `x-disky-build: <sha> <time>` on every response, from `VITE_BUILD_SHA` at build time.

Workers targets are refused for `full`: a Worker has no preview environment carrying the dev stack's bindings, so a native Preview's bindings would have to be pointed at production by hand, a different trust decision. The native Workers Previews adapter (`publishWorker`, `wrangler preview --ignore-base-config`, Wrangler ≥ 4.135.0) stays in `preview.mjs` as `stack`'s candidate provider.

## Sign-in

Viewers sign in through the OA broker (login.oa.dev) with their real roles, as on `dev`, through a **separate preview OAuth client per Pages project**: its callback is the one-label pattern `https://*.<project>.pages.dev/<exact path>`, its secret is held only in the project's preview environment, it has its own audience, and it asks a one-click consent instead of signing in silently. Built by the auth session; not yet available:

- TODO(auth session): the preview client's id variable name.
- TODO(auth session): the preview client's secret name in the Pages preview environment.
- TODO(auth session): the exact callback path.

The site side then needs, on a `pages.dev` branch host: its broker `redirectUri` derived from the request origin, the preview client chosen instead of the deployment's own, and no silent (`prompt=none`) sign-in. Not implemented here.

## Credentials

- Gate: repository read token only. Publish: PR comment + Deployments Write for the job's `GITHUB_TOKEN`, and a Pages deployment token (`CF_PREVIEW_DEPLOY_TOKEN`) for the account. Retire: the same. No DNS or Zero Trust token.
- Secrets belong only in the publish/retire job's protected environment, not in the build. Secret values never appear in action argv, receipts or comments.

## Publish and retire

Publish, step by step: the live PR is re-read (open, non-draft, same base, head = the captured SHA, author or approver still write+); the artifact is checked for symlinks/special files and for carrying its own `_worker.js` if the deploy directory has `functions/`; the project is read (subdomain matches, `pr-N` is neither production nor protected); the `pr-N` branch's existing deployments must all be this tool's (owner marker in the commit message), previews, not canonical, holding no production or protected alias; then `wrangler pages deploy <artifact> --branch pr-N --project-name <project> --commit-hash <sha> --commit-message <owner> --commit-dirty=false`; the new deployment is read back (owned, the captured SHA, stage `success`); the PR is re-read again (a head pushed during upload is never advertised); the probe proves `https://pr-N.<subdomain>` serves the captured SHA; a GitHub deployment and the PR comment are written.

Retire: the PR must be closed; the project and the `pr-N` branch's deployments get the same ownership checks, and any unowned, production, canonical or protected-alias deployment refuses the whole retire before a delete; then each owned deployment is deleted (`?force=true`). Production, `dev` and every other branch are never selected. A repeat finds nothing and succeeds. Lists are fully paginated; a partial failure is a job failure with a receipt and is retryable.

After a PR is retargeted, call `retire` for **every configured target it could have used**, not just its final base. Caller concurrency should serialize all publish/close operations for that PR; a closed-state recheck prevents a new publish after closure.

Receipts identify repository, target, mode, PR, SHA, provider, project, branch, host, the deployment IDs and the GitHub run. A bot comment is updated rather than appended on every successful rebuild; close marks it retired. No existing deployments are cleaned by merely adding this action's source to a repository.

A small caller workflow is shown in [the example]: a trusted gate, a credential-free build, and a trusted publisher. PR source/config is never checked out or installed in the credentialed publisher. Provide `sha` and `mode` from gate, not inputs editable by the PR. Do not interpolate comment bodies, branch names or PR titles into shell commands.

Run `node --test .github/actions/cf-preview/preview.test.mjs` for the dependency-free tests. Remaining acceptance: the broker's preview client, a live pilot on one deployment (create, update, close, retry), and the cleanup API's behavior on a branch's aliased deployment.

[the spec]: ../../../specs/cf-pr-previews.md
[the example]: examples/preview.yml
