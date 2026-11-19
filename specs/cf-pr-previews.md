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
| `sandbox` | the PR's full build, as branch `pr-N` of a per-deployment `<project>-sandbox` Pages project | a shared sandbox D1 copied from production; production's serving R2, read-only; no outside-effect secrets | approved (Ryan, 2026-11-14), not built |
| `pipeline` | a job image built from the PR head, run from GHA | a scratch store prefix (reads of declared inputs) | specified |

### Selection

Ryan, 2026-11-14: the data a preview gets is an option, not a mandate. Each deployment picks its default, a PR can always choose safer data, and only a maintainer can choose riskier.

**Risk order:** `stack` (fixture data) < `sandbox` (a copy of production's D1, production's R2 read-only, no outside-effect secrets) < `full` (production's D1 read-write and the preview secrets). `pipeline` is outside the order: it runs PR code with cloud credentials, so it is never a default and only a writer selects it.

**Per-deployment default:** the target's `default_mode` in `.github/cf-preview.json` (`stack`, `sandbox` or `full`), set by the deployment's owner. A target that doesn't name one gets `sandbox` once built; until then `full`, as today.

**Safer is free:** the PR's author (anyone who can push to it) lowers the mode with a directive, either a `Preview-Mode: <mode>` trailer on the head commit or a `Preview-Mode: <mode>` line in the PR body. A directive is honored only when it is no riskier than what would otherwise run. Anyone can write a commit message, so a directive naming a riskier mode is ignored (the bot comment says so), never an error that blocks the safer preview.

**Riskier needs a maintainer:** a current write collaborator raises it, after reading the head: a `preview:<mode>` label (labels need triage+), a `/preview <mode>` comment (`/preview <mode> <full-head-sha>` for an external head), or the dispatch `mode` input. A writer's selection may also lower.

**Effective mode:** the writer's selection if there is one (comment or dispatch for that run, else the label), else the deployment's default; then lowered by the author's directive if it names a safer mode. Two mode labels, an unknown `preview:*` label (including the retired `preview:ui`) or an unknown comment mode fail the gate rather than guessing. Gate passes the effective mode to publish as an output, so a label changed between the jobs can't switch it. Unbuilt modes are refused at the gate.

**A new push under a raised mode:** the raise belongs to the head a writer reviewed, not to the PR. On a new head:
- **The author is a current writer:** the raised mode carries over. A writer can push to `dev` anyway, so their own head is no riskier.
- **Anyone else:** the raised-mode preview isn't updated. It stays at the reviewed head, and the bot comment marks it stale with the command that approves the new head, `/preview <mode> <full-head-sha>`. The label stays as the standing intent, so approving needs only that comment. A preview at or below the deployment's default still runs for the new head, under the usual approval rules for external heads.

A mode change between runs retires the PR's preview in the other mode first (each mode deploys to a different project or store).

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
- **Credentials.** A Pages deployment token for the account (`CF_PAGES_DEPLOY_TOKEN`), in the publish/retire job's environment only. No DNS token, no Zero Trust.
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

### `sandbox`: the PR's build over a copy of production's data (design approved 2026-11-14, not built)

Why: `full` gives a PR production's D1 read-write and every preview secret, so exercising an admin write (staging, grants, the admin list, "Run a scan") on a preview acts on production. `stack`'s fixture store keeps production safe but can't show a change against real data. `sandbox` sits between them: real data, no effect outside the sandbox. Ryan asked (2026-11-12) that a preview can point at dummy data, the public demo, or a copy of production's; `stack` covers the first two, `sandbox` the third.

Why a second Pages project: Pages gives every branch of a project the same preview bindings, so a branch of the deployment's own project can't get different data. One `<project>-sandbox` project per deployment, created by its owner (Pulumi `cf/`), whose preview environment binds:

- **D1:** `<project>-sandbox-db`, a copy of production's (`wrangler d1 export` → import), refreshed on a schedule (laggy by design) and on demand before a preview that needs current rows. Sessions and grants are dropped from the copy; viewers sign in fresh.
- **R2:** production's serving bucket through a read-only token. Nothing a preview does writes there.
- **Secrets:** sign-in (the broker's preview client) only. No `GCP_SA_KEY`, Slack, Discord or Batch credentials, so dispatches, digests, sweeps and notices can't reach outside (routes like `/api/admin/history` already 503 without `GCP_SA_KEY`; the pilot audits every route that sends anything without one), and a write lands only in the sandbox D1.

Selection: per Selection above; the default for a target that doesn't name one, once built. `full` then needs a writer's `preview:full` label or `/preview full`, for a change only production's environment can show. Cleanup is `full`'s (owned `pr-N` deployments in the sandbox project); the project and its D1 outlive PRs.

Ryan's go (2026-11-14) is on the design. Creating each sandbox Pages project and copied D1 is a `pulumi` / D1 apply in that deployment's owner session, each with its own go from Ryan.

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

**cw's pilot (2026-11-17, PR #5, `rw/cw-preview-pilot`)** passes gates 2 and 3 for cw. Create: run 44166278920, the probe read the head on `pr-5.oa-cw-s3-usage.pages.dev`. Update: run 44167745843, a new head redeployed and the comment was edited in place. Sign-in: OA through the broker's `oa-cw-s3-usage-preview` client, consent then signed in (Ryan). Retire on close: run 44168673676; `DELETE …/deployments/<id>?force=true` removes the branch's latest (aliased) deployment too, no pr-5 deployment remains, and the host answers 404 (`/api/health`, checked by root). Not exercised: a retried close. `front-door`'s `pr-preview.yml` serves `hf`, `cw` and `gcs` (c268eca3e).

**gcs's previews (2026-11-17, PRs #6 `work/gcs/release-docs` and #7 `work/gcs/badged-avatars`)** pass gate 2 for gcs, through `front-door`'s `pr-preview.yml` with the `gcs-preview` environment's deploy token. Create: run 44169157251 deployed 82d215f43 to `pr-6.oa-gcs-usage.pages.dev`, whose `x-disky-build` read it, behind the same 401 wall as dev.gcs.oa.dev. Update: #7's runs 44183644596, 44192267450 and 44196606873 redeployed each new head (88d165434, 8073a1aa6, 7b9bf8f57), with one cf-preview comment per PR edited in place. Retire on close: run 44172783591 (#6) and 44203947862 (#7), and both hosts answer 404 (`/api/health`, 2026-11-18). gcs.oa.dev and dev.gcs.oa.dev were untouched. One run failed, 44178174733 (#7's first head b9156faed): the frozen build's tests caught a real `stagedThread.test.ts` failure, fixed in the next head; the preview step never ran. Not yet exercised: sign-in through the broker's gcs preview client (`OA_LOGIN_PREVIEW_CLIENT_ID` / `_SECRET` are set on the Pages project's preview environment) on a live `pr-N` host, which needs an open gcs PR and Ryan; nor a retried close.

**r2's pilot (2026-11-19, PR #9, `rw/r2-preview-pilot`)** passes gates 2 and 3 for r2, through `front-door`'s `pr-preview.yml` (9615f006f). Create: run 44431964618 deployed 15b67549d, and `x-disky-build` on `pr-9.oa-r2-usage.pages.dev` read it. Its first attempt failed at `cf-preview: Account ID must come from exactly one of the target config and the account-id input` because `r2-preview` lacked the `CLOUDFLARE_ACCOUNT_ID` variable the other `<d>-preview` environments carry; once that was set, a rerun passed. Update: run 44432867931 deployed 7403fe602, and the one comment was edited in place. Sign-in: OA through the broker's `oa-r2-usage-preview` client, consent then signed in (Ryan). Retire on close: run 44435197783, no pr-9 deployment remained, and the host answered 404. Reopen: run 44435725878 redeployed, and run 44435734154 deployed 27b426327, a visible marker in the Map's root hint, read in Chrome on pr-9. r2.oa.dev (c781c9bfc) and dev.r2.oa.dev (b3e3de66f) were untouched throughout. Not exercised: a retried close.

**Onboarding a deployment** (all four done by 2026-11-19): its branch's `.github/cf-preview.json` target; a `<d>-preview` GitHub environment (branch policy `front-door`) holding the secret `CF_PAGES_DEPLOY_TOKEN` (Pages Write on the account) **and** the variable `CLOUDFLARE_ACCOUNT_ID` (without it, publish fails at "Account ID must come from exactly one of the target config and the account-id input"); the broker's `<project>-preview` client in the Pages project's preview environment (`OA_LOGIN_PREVIEW_CLIENT_ID` / `_SECRET`); and the branch in `front-door`'s `pr-preview.yml` (`branches` and `PREVIEW_BASES`).

**GitHub environments (fixed 2026-11-18):** each run records a GitHub deployment, and these were recorded under a per-PR environment (`preview/<d>/pr-N`). GitHub creates an environment for every name a deployment uses, and deleting one takes administration rights `GITHUB_TOKEN` can't have, so every PR left one behind (cw pr-5, gcs pr-6/7 and hf pr-3/8; deleted by hand 11-18 with Ryan's ok). Now one environment per target (`preview/<d>`): `auto_inactive` is off, since it would end other PRs' previews there, and each run ends its own PR's earlier record still showing `success` (`record` in `run.mjs`), so a retire's `inactive` record leaves nothing of that PR live. **Known gaps (2026-11-18, harmonize's `prvw` survey):** the 11-14 Selection design (§ Selection: per-target `default_mode`, the lower-only `Preview-Mode:` directive, writer-only raises pinned to the reviewed head, `sandbox`) is unbuilt, so the action still defaults to `full` by label or comment; and a PR whose base changes keeps its old target's preview until that target's preview is retired by hand (`pr-preview.yml` retires only under the final base, and `authorize` refuses a retire for an open PR or another base). Both are planned once, in the shared `prvw` library (`~/c/oa/prvw/specs/prvw.md`), which this action moves onto.

**hf's pilot (2026-11-18, PR #8, `rw/hf-preview-pilot-2`)** passes gates 2 and 3 for hf, through `front-door`'s `pr-preview.yml`. Create: run 44296024236 deployed 73eb66ee3, and `x-disky-build` on `pr-8.oa-hf-usage.pages.dev` read it. Update: run 44296824531 deployed the new head c88132343, and the one cf-preview comment was edited in place. Sign-in: OA through the broker's `oa-hf-usage-preview` client (Ryan). Retire on close: run 44302644312, and the host answers 404 (`/api/health`, checked by root). hf.oa.dev and dev.hf.oa.dev were untouched throughout. Not exercised: a retried close. As `sandbox` (below) says, `full` runs with the dev stack's preview environment, secrets included: page loads on pr-8 posted an activity burst to #hf-admin (1795004193.155349) linking pr-8's `/admin/activity`.

## Validation

22 dependency-free Node tests (`node --test .github/actions/cf-preview/preview.test.mjs`), exact-equality specs: the `full` plan (branch, subdomain, protected branches, host; a suffixed subdomain; invalid inputs), mode selection and comment parsing (`ui` now unknown), gate refusals (unimplemented modes, Workers targets, no probe), collaborator/current-head and closed-state checks, pagination, owned-deployment selection (production, canonical, unmarked and protected-alias refusals; `dev` ignored), the publish (exact Wrangler argv, readback, no other writes, refusals before any upload), the base-Functions check, the trusted Wrangler's argv/cwd/env as executed, retire (only owned `pr-N` deployments deleted, a repeat no-op, every refusal before any delete), the revision probe on `https://pr-N.<subdomain>/api/health`, error redaction, symlink rejection, and the Workers adapter. No Cloudflare deployment, OAuth client, runtime binding or GitHub environment was created.

[JCT]: https://github.com/HackJerseyCity/jc-taxes/blob/main/.github/workflows/preview.yml
[OA www]: https://github.com/Open-Athena/open-athena.github.io/blob/main/.github/workflows/deploy-preview.yml
