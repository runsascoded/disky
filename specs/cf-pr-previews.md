# Reusable Cloudflare PR previews

Status: `full` mode implemented in source and unit-tested, November 11, 2026, replacing the `ui` mode built earlier the same day. Not live: pending the gates below (the broker's preview client, a live pilot, the cleanup API's behavior). `stack` and `pipeline` are specified, not built. Requested by Ryan while setting up the four OA deployments. Applies to arbitrary callers, not only HF; HF is the intended pilot. The existing dev/prod release controller (`oa-release.yml`) remains a separate lane.

## Decision record

- **2026-11-10**: source prototype: one deployment's Pages branch at a per-PR custom host `prN.<domain>`, with a DNS record and Pages custom domain per PR.
- **2026-11-11 (morning)**: three modes approved (`ui`, `stack`, `pipeline`); `ui` built: only the PR's static build, in a separate Pages project, behind a trusted forwarding layer (`layer.mjs`) that sent `GET` reads to production with one read grant, viewers gated by Cloudflare Access, at `prN.<domain>` with DNS.
- **2026-11-11 (evening), Ryan**: `ui` rejected; redesign approved (the root session's open questions, #1). A preview is what a deployment's `dev.<host>` already is: the PR's **full build** (front end + Functions), deployed as a **branch of the deployment's existing Pages project**, so it runs on that project's **preview environment** (its bindings and secrets: production D1/R2 on Disky, as `dev`), served at Cloudflare's automatic branch host `pr-N.<project>.pages.dev`. No custom domain, DNS record, per-PR certificate, Cloudflare Access, forwarding layer or separate Pages project. Viewers sign in through the OA broker with their real roles, through a separate preview OAuth client per Pages project (below).

Why `ui` goes rather than staying as an option: its point was that no PR backend code ran with credentials. But the PR's front-end JavaScript runs same-origin with whatever the viewer can do, so the protection only covered the server half, and it cost a second viewer list (Access), a read grant everyone shared (no real roles, no admin pages), a separate project per deployment, and a DNS-zone token. Under the same author/approval gate, a `full` preview is no more trusted than a push to `dev`, and it shows the PR exactly as it will behave. A front-end change from an author nobody will vouch for doesn't get a preview; a maintainer reads it first and approves the exact head.

Why the name `full`: it deploys the full build onto the deployment's full preview environment (its data, secrets and sign-in), as `dev` does. `dev` itself was rejected as a name: it would collide with the `dev` branch and alias, which retire must never touch. `stack` is also a full build, but on a scratch stack; the names contrast the data, which is the difference that matters.

## Prior art

[JCT]'s preview workflow has the reusable trust pattern: current repository write/maintain/admin authors get automatic previews; others need a current maintainer's `/preview` approval of the captured head SHA. Builds execute without CF credentials, then a trusted base checkout uploads the built Worker/assets with an allowlisted configuration. It publishes a native `workers.dev` URL; retirement is manual.

[OA www] deploys Pages branch previews, updates a PR preview link, and has a cleanup workflow. Its cleanup covers only one of its two preview projects, looks at the first deployment page, and selects branch names without a resource ownership receipt. Do not copy these gaps into the shared tool.

## Deliverable

A dependency-free Node toolkit and composite action under `.github/actions/cf-preview`, consumable from another repo by a pinned `Open-Athena/disky/.github/actions/cf-preview@<sha>` reference. Caller-owned build/test instructions and explicit preview configuration stay in the caller; the common component owns authorization, mode selection, naming, exact-head revalidation, deployment readback, the revision probe, a secret-free receipt and cleanup. It must not execute PR-supplied config or build hooks in the credentialed job. A future move to a dedicated repository must preserve this small interface.

## Trust model (kept from the prototype)

- **Gate.** An automatic run (`opened`, `synchronize`, …) needs a current write/maintain/admin PR author. Anyone else's head needs a current write collaborator's `/preview <full-sha>` comment (or dispatch), approving that one head; a later push needs another approval.
- **Credential-free build.** The caller's build job checks out the captured head with no credentials and uploads the built site as an artifact. On Disky that includes the PR's Functions compiled to `dist/_worker.js` (`wrangler pages functions build`), so the credentialed job never bundles PR source.
- **Trusted publish.** The publish job checks out the base revision gate captured, installs its pinned Wrangler, and runs it as argv from the trusted config (`wrangler`), in the trusted `working_directory`. The PR supplies only the artifact, checked for symlinks and special files.
- **Exact head.** Publish re-reads the PR (open, non-draft, same base, head = captured SHA, approver still write+) before deploying and again before reporting success.
- **Ownership.** Every deployment the tool creates carries the owner marker `cf-preview-v1:<repo>#<N>:<project>` as its commit message. Publish refuses to take over a `pr-N` branch holding anything else; retire deletes only marked deployments.
- **Probe.** Publish proves the custom origin serves the captured SHA: `https://pr-N.<subdomain>` + the target's `verification` (Disky: `/api/health`, header `x-disky-build`, whose first token is the build's SHA from `VITE_BUILD_SHA`), no redirects, bounded retries. Reachability, a redirect or another build cannot pass.
- **Receipts and comment.** A secret-free receipt artifact, a GitHub deployment per publish/retire, and one bot comment updated in place.

What approval now means: an approved head's server code runs with the deployment's preview bindings and secrets, i.e. the dev stack's production data. That is the same trust as a push to `dev`; approve accordingly.

## Modes

| Mode | Deploys | Data it can touch | Status |
|---|---|---|---|
| `full` | the PR's full build, as branch `pr-N` of the deployment's own Pages project | the project's preview environment (on Disky: the dev stack's production D1/R2, secrets) | implemented (Pages) |
| `stack` | the PR's full build | a scratch D1 + R2 created for the PR | specified |
| `pipeline` | a job image built from the PR head, run from GHA | a scratch store prefix (reads of declared inputs) | specified |

### Selection

- A `/preview <mode>` comment (`/preview <mode> <full-head-sha>` for an external head), or the dispatch `mode` input, selects the mode for that run.
- Otherwise a `preview:<mode>` label on the PR selects it, on every automatic run. Labels are the persistent selector; only triage+ collaborators can set them.
- Otherwise `full`.

Two different mode labels, an unknown `preview:*` label (including the retired `preview:ui`) or an unknown comment mode fail the gate instead of guessing. Gate passes the chosen mode to publish as an output, so a label changed between the jobs cannot switch it. `stack` and `pipeline` are refused at the gate until built.

### `full`: the PR's build on the deployment's preview environment

As built (`preview.mjs` `previewPlan`, `modePlan`, `publishFull`, `retireFull`; `run.mjs`):

- **Plan.** A Pages target names the deployment's own `project`, optionally its `subdomain` (default `<project>.pages.dev`; Cloudflare suffixes it when the name was taken) and extra `protected_branches` (`dev` is always protected). The plan is branch `pr-N`, host `pr-N.<subdomain>`. Gate refuses a Workers target, an unimplemented mode, or a target without a `verification` probe, before anything is built.
- **Publish**, step by step:
  1. Re-read the PR and the approver's permission (exact head).
  2. Check the artifact (no symlinks/special files), and that the deploy directory's `functions/` (if any) can't be deployed: the artifact must then carry its own `_worker.js`. Wrangler builds the working directory's `functions/` unless the output has `_worker.js`; without this check the base branch's Functions would go out under the PR's front end.
  3. Read the project: its `subdomain` must be the plan's, and `pr-N` must be neither its production branch nor protected.
  4. List the project's preview deployments (fully paginated). Every `pr-N` deployment must be a preview, not canonical, carry the owner marker, and hold no alias of the production or a protected branch. Other branches are ignored.
  5. `wrangler pages deploy <artifact> --branch pr-N --project-name <project> --commit-hash <sha> --commit-message <owner> --commit-dirty=false`, from the trusted `working_directory`, whose `wrangler.toml` is the deployment's own: `[env.preview]` applies exactly as on `dev`.
  6. Read back: the `pr-N` deployments pass step 4 again, and the newest with the captured SHA has stage `success`.
  7. Re-read the PR (a head pushed during upload is never advertised).
  8. Probe `https://pr-N.<subdomain>` for the captured SHA.
  9. Record a GitHub deployment; create or update the PR comment.
- **Retire**, on close: the PR must be closed. Read the project and the `pr-N` deployments with the same checks as publish step 3–4; any unowned, production, canonical or protected-alias deployment refuses the whole retire before a delete. Delete each owned deployment (`DELETE …/deployments/<id>?force=true`). Production, `dev` and every other branch are never selected. A repeat finds nothing and succeeds; a partial failure leaves a receipt and is retryable.
- **Credentials.** A Pages deployment token for the account (`CF_PREVIEW_DEPLOY_TOKEN`), in the publish/retire job's environment only. No DNS token, no Zero Trust.
- **Bindings.** The PR's build sees the project's preview environment as configured by the base branch's `wrangler.toml` (or the dashboard where there is none). A PR that adds a binding gets it only once the base carries it.

Known edges, to settle in the pilot:

- `pages.dev` is on the public suffix list, so `<project>.pages.dev` is one site: a PR's code can set cookies on `.<project>.pages.dev`, reaching other previews and `dev.<project>.pages.dev` (not the production host or `dev.<host>`). A `__Host-` prefixed session cookie closes this; check what the auth package sets.
- The preview environment is shared with `dev`, so a PR preview reads and writes the same D1 sessions, grants and staged plans as `dev` and production.

### Sign-in on preview hosts

Viewers sign in through the OA broker (login.oa.dev) with their real roles, through a **separate preview OAuth client per Pages project**, being built by the auth session:

- a one-label pattern callback, `https://*.<project subdomain>/<exact path>`;
- its own secret, held only in the project's Pages preview environment;
- its own audience, so a session or code minted for it is not one for the deployment's own client;
- a one-click consent instead of silent sign-in.

The preview environment's code is the PR's, so it can read that secret: the separate client and audience bound what a leaked preview secret is worth.

TODO(auth session): the preview client's id variable name, its secret's name in the Pages preview environment, and the exact callback path. Fill these in here and in the action README once the broker side exists; don't invent them.

Site side (not implemented here): on a `pages.dev` branch host the site must derive its broker `redirectUri` from the request origin (as `oidcConfig` already does for Google), choose the preview client instead of the deployment's own, and skip the silent `prompt=none` redirect (`_lib/silentSignIn.ts`). `dev.<host>` and production keep today's client.

### `stack`: the PR's full build over scratch data (specified)

Deploys: the PR's whole site, front end and Functions, as built by the credential-free job.

Data: a scratch D1 database and R2 bucket per PR, both named in the reserved namespace (`<project>-pr<N>`), created at publish and deleted at retire. The D1 gets the PR's own `site/schema.sql` (a PR that changes the schema is what `stack` is for) and a small seed; the R2 bucket gets a fixture store (a few scans' served files, generated or copied from a public demo store), never a copy of a gated deployment's data. No production binding, secret or store credential is given to the deployment, so it can't be a branch of the deployment's project (whose preview environment holds production's): it needs its own project, or a native Workers Preview with an explicit template (`publishWorker`, kept in `preview.mjs` for this).

Credentials: the deploy token additionally needs D1 Edit and R2 Edit on the account. Sign-in: to be specified with the auth owner (the scratch D1 holds no sessions or grants).

Ownership and cleanup: a `_cf_preview_owner` row in the D1 and a `.cf-preview-owner` object in the bucket carry the owner marker; retire proves both before deleting, empties the bucket with full pagination, then deletes bucket, database and the deployment. A mode switch (`full` ↔ `stack`) retires the PR's other mode first.

### `pipeline`: the PR's job code over a scratch prefix (specified)

Deploys: nothing to Cloudflare. A credential-free job builds the job image from the PR head and uploads it as an artifact; a trusted job pushes it to a preview image repository under an owned tag (`pr-<N>-<sha>`) and dispatches the deployment's job (Batch, through `batch.submit_job`, in the bucket's region) with that image.

Data: reads of declared inputs only (a fixed listing or a public demo store), writes only under a scratch prefix (`<scratch bucket>/pr-<N>/`), through a service account whose write grant is conditioned on that prefix. The job's results (exit status, summary, links to the outputs) are commented on the PR.

Credentials and gates: `pipeline` runs PR code with cloud credentials, so besides the usual authorization it is never the default; it runs only when a `preview:pipeline` label or a `/preview pipeline` comment asks for it. Cleanup on close deletes the prefix (and a bucket lifecycle rule expires it as a backstop) and the image tag.

## Providers

Pages only, for `full`. Workers targets are refused at gate: a Worker has no preview environment carrying the dev stack's bindings, so a native Preview's bindings would be pointed at production by hand, a different trust decision, and no Disky deployment is a Worker. Not trivial, so not supported. The Workers adapter (`publishWorker` / `retireWorker`: a native Preview `prN` under a parent custom domain with `previews_enabled`, `wrangler preview --ignore-base-config`, Wrangler ≥ 4.135.0) stays, tested, as `stack`'s candidate provider.

## Remaining gates

1. **The broker's preview client** (auth session): the client per Pages project, with the pattern callback, its own secret and audience, and consent; then the site-side changes above, and the TODOs filled in.
2. **A live pilot on one deployment** (hf first): the caller workflow on the branch GitHub reads its events from, the `.github/cf-preview.json` target, the deploy token in a protected `preview` environment. Then a real disposable preview's create, update (new head), close, and a retried close; the probe passing on `pr-N.<subdomain>`; sign-in through the preview client with real roles; `dev` and production untouched throughout. Each per-deployment step needs Ryan's go.
3. **The cleanup API's behavior**: whether `DELETE …/deployments/<id>?force=true` deletes the branch's latest (aliased) deployment, in which order deletes must go, and whether the `pr-N` alias then stops serving. If Cloudflare refuses the last one, retire must report the failure with its receipt, never a false success.
4. Publish the reviewed action and pin callers to its source SHA. Then expand to the other deployments.

## Validation

19 dependency-free Node tests (`node --test .github/actions/cf-preview/preview.test.mjs`), exact-equality specs: the `full` plan (branch, subdomain, protected branches, host; a suffixed subdomain; invalid inputs), mode selection and comment parsing (`ui` now unknown), gate refusals (unimplemented modes, Workers targets, no probe), collaborator/current-head and closed-state checks, pagination, owned-deployment selection (production, canonical, unmarked and protected-alias refusals; `dev` ignored), the publish (exact Wrangler argv, readback, no other writes, refusals before any upload), the base-Functions check, the trusted Wrangler's argv/cwd/env as executed, retire (only owned `pr-N` deployments deleted, a repeat no-op, every refusal before any delete), the revision probe on `https://pr-N.<subdomain>/api/health`, error redaction, symlink rejection, and the Workers adapter. No Cloudflare deployment, OAuth client, runtime binding or GitHub environment was created.

[JCT]: https://github.com/HackJerseyCity/jc-taxes/blob/main/.github/workflows/preview.yml
[OA www]: https://github.com/Open-Athena/open-athena.github.io/blob/main/.github/workflows/deploy-preview.yml
