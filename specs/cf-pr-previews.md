# Reusable Cloudflare PR previews

Status: source prototype implemented and tested, November 10, 2026; three preview modes approved by Ryan November 11, 2026, with `ui` implemented in source the same day (`stack` and `pipeline` specified below, not built). Live activation pending the gates below. Requested by Ryan while setting up the four OA deployments. Applies to arbitrary callers, not only HF; HF is merely the initial dev/prod release pilot. The existing dev/prod controller remains a separate lane.

## Prior art

[JCT]'s preview workflow has the reusable trust pattern: current repository write/maintain/admin authors get automatic previews; others need a current maintainer's `/preview` approval of the captured head SHA. Builds execute without CF credentials, then a trusted base checkout uploads the built Worker/assets with an allowlisted configuration. It publishes a native `workers.dev` URL; `prN.jct.rbw.sh` is not provisioned and retirement is manual.

[OA www] deploys Pages branch previews, updates a PR preview link, and has a cleanup workflow. It does not register a custom hostname per PR. Its cleanup currently covers only one of the two preview projects, looks at the first deployment page, and selects branch names without a resource ownership receipt. Do not copy these gaps into the shared tool.

## Deliverable

A dependency-free Node toolkit and composite action under `.github/actions/cf-preview`, consumable from another repo by a pinned `Open-Athena/disky/.github/actions/cf-preview@<sha>` reference. Caller-owned build/test instructions and explicit preview resource configuration stay in the caller; the common component owns authorization, naming, exact-head revalidation, CF domain/DNS lifecycle, deployment readback, a secret-free receipt and cleanup. It must work with Pages and Workers, without executing PR-supplied config or build hooks in the credentialed job. A future move to a dedicated repository must preserve this small interface rather than duplicate four Disky workflows.

The caller builds the approved head on a separate credential-free runner, retains all Worker modules (including WASM), and uploads an artifact. The publish job checks out trusted base tooling/config and installs a pinned Wrangler from that trusted checkout. The action invokes Wrangler as argv, never a shell command supplied by the PR. Pages deploys a reserved `pr-N` branch of the configured project; Workers creates a native named Preview `prN` under an existing parent, using caller-provided preview bindings, no inherited production routes/build hooks/triggers and `--ignore-base-config`. Wrangler must be at least 4.135.0; installed 4.150.0 was inspected for the Preview API/annotation contract. Built code still receives its configured runtime bindings: hiding the upload token does not make a malicious backend harmless.

`prN.<base-domain>` is registered for that PR, with a per-PR custom-domain binding and proxied DNS record for Pages. Workers uses a one-time parent custom-domain configuration with `previews_enabled: true`; Cloudflare owns its shared wildcard DNS/certificate and the named Preview owns the per-PR route. Native Preview cleanup removes its deployments, not the parent or shared wildcard baseline. Publish rechecks the PR head and permission immediately before changes. External updates require another explicit approval. Closed PR cleanup verifies current PR state, provider/project, exact reserved name, ownership marker and production exclusions; it paginates complete result sets, handles missing owned resources idempotently and refuses to delete unknown resources. Partial CF failures remain visible and retryable, never a false cleanup success. A custom-origin probe must return the captured full SHA (text endpoint or configured header), so a reachable host accidentally serving production cannot pass.

The existing project's production deployment and stable dev alias must remain untouched. Disky's present dev stacks share production D1/R2; PR previews do not. What a preview may touch is now its mode's, below. The auth owner has been asked for the supported dynamic callback-registration contract; no mode depends on it (`ui` signs viewers in through Cloudflare Access, not the deployment's own sign-in).

## Modes

A PR's preview runs in exactly one of three modes. They differ in what code is deployed and what data it can reach:

| Mode | Deploys | Data it can touch | Write paths | Status |
|---|---|---|---|---|
| `ui` | the PR's built static assets + a trusted read-only layer from the base branch | production, `GET`/`HEAD` of allowlisted paths, through one read grant | none | implemented (Pages) |
| `stack` | the PR's full build: front end + Functions | a scratch D1 + R2 created for the PR | the scratch resources only | specified |
| `pipeline` | a job image built from the PR head, run from GHA | a scratch store prefix (reads of declared inputs) | the scratch prefix only | specified |

### Selection

- A `/preview <mode>` comment (`/preview <mode> <full-head-sha>` for an external head), or the dispatch `mode` input, selects the mode for that run.
- Otherwise a `preview:<mode>` label on the PR selects it, on every automatic run (`opened`, `synchronize`, `labeled`, …). Labels are the persistent selector; only triage+ collaborators can set them, so an external author cannot pick a mode.
- Otherwise `ui`.

`ui` is the default because it is the least-privileged mode by construction (no PR backend code runs, no write binding exists, nothing is created beyond the host) and it covers the common case, reviewing a front-end change on real data. `stack` and `pipeline` run the PR's own server or job code against credentials and cost resources, so they must be asked for. Two different mode labels, an unknown `preview:*` label or an unknown comment mode fail the gate instead of guessing. Gate passes the chosen mode to publish as an output, so a label changed between the jobs cannot switch it. `stack` and `pipeline` are refused at the gate until built.

Authorization is the same for every mode: an automatic run needs a current write/maintain/admin author; a comment or dispatch needs a current write collaborator as the actor, approving one captured head.

### `ui`: the PR's front end over production, read-only

Deploys: the PR's static build (the credential-free build job's artifact), with any `_worker.js` / `_routes.json` it carries replaced, plus `_worker.js` generated from the trusted action checkout's `layer.mjs` and this publish's settings (upstream, viewers, path rules, SHA, PR, hostname), with `_routes.json` sending every path through it. The PR's Functions and backend code are never deployed. Provider: Pages, into a dedicated project per deployment (`modes.ui.project`), never the deployment's own (on Disky, that project's preview environment is the dev stack, with production bindings).

Data: only what the layer forwards. It answers `GET`/`HEAD` and nothing else (405 for every other method, on every path); forwards reads of `/api/`, `/data/`, `/v1/files/` to the deployment's production origin, the same set the `API_ORIGIN` dev proxy forwards (`site/vite.config.ts`); refuses `/api/auth/*` (except `/api/auth/whoami`) and `/api/token`, encoded separators, and `?key=` (the gate reads it as a share link); serves every other path from the PR's assets. The deny list is defence in depth: the read grant is not an admin, so production refuses the admin reads (`/api/db`, `/api/footer-cache`, `/api/admin/*`, `/api/activity`) by itself.

Credentials:

- Forwarded reads authenticate as **one read grant held by the layer**: a production grant with the deployment's base scope (never `admin`; with `ADMIN_LIST` production refuses to mint one anyway), minted once by an admin and set as the dedicated project's preview secret `CF_PREVIEW_READ_TOKEN`. The layer adds `Authorization: Bearer <grant>` and forwards no other credential: the viewer's cookies and any `Authorization` the page sets are dropped, upstream `Set-Cookie` is dropped, and responses leave `private, no-store`. Production's usage records attribute every preview read to that grant. A path-restricted grant (`<tag>:hide:<prefix>`) works too, with its restrictions (no `q=` filter).
- Viewers of a gated deployment (gcs, cw, hf) sign in through **Cloudflare Access**: one Access application on `*.<preview domain>`, its policy naming the reviewers, and a Bypass application on `*.<preview domain>/__preview/revision` for the publish probe. The layer does not trust the edge alone: on every request but the probe it verifies the `Cf-Access-Jwt-Assertion` (RS256 against `https://<team>/cdn-cgi/access/certs`, issuer, AUD tag, expiry) and checks the email against its own trusted `emails` / `domains` list, and it serves only `prN.<domain>`, so the `pr-N.<project>.pages.dev` aliases that Access does not cover get a 421. Without a valid assertion the answer is a 403 before any asset or forward.
- A public deployment (the r2 demo, r2.rbw.sh) declares `viewers: "public"`: no gate, no grant, reads forwarded without credentials. Public is never a default; a target must say it.

Why a layer-held grant behind Access, and not the viewer's own credentials:

- Production's session cookie is host-only on `<host>`, so it never reaches `prN.<domain>`. Widening it to a parent domain would change production auth and hand the session to every sibling host, PR-code hosts included.
- Signing in on the preview host itself needs a per-host OAuth or broker callback: gate 4, with no broker change on offer.
- A Bearer agent token typed into the preview origin is exposed to the PR's own JavaScript on that origin (it can draw a look-alike form under any path with `pushState`), and agent tokens are non-expiring and can stage deletions. So no credential of the viewer's may enter the PR's origin. With Access, sign-in happens on Cloudflare's origin; the preview origin holds only an HttpOnly Access cookie whose sole power there is these read-only forwards, and the grant never leaves the layer.

The costs: Access is a second list of who may look, beside production's own allowlist (keep it to the reviewers, usually the staff domain, and repeat it in the trusted config, which the layer enforces); everyone sees the grant's view, not their own (no admin pages, no staff-only scopes); revoking a viewer is an Access change. The OA account already runs Zero Trust (cw's Access application); a deployment on an account without it uses `public` only if the deployment itself is public.

Satisfied by construction:

- No write binding: publish reads the dedicated project's preview configuration and refuses any binding (anything that is not a plain setting) and any variable but the read grant as a secret (none when public); the layer refuses to serve (503) if its environment holds any binding but `ASSETS`. Wrangler runs where there is no `functions/` and no wrangler config, so neither the base branch's backend nor its production bindings can ride along.
- No PR code with credentials: the PR supplies only static files, run in the viewer's browser; the layer and its settings come from the base branch's action checkout. The build job stays credential-free as before.
- No new data resources: nothing is created but the existing Pages branch, DNS record and custom domain, so cleanup is the existing Pages retirement.
- Production data cannot be changed by a preview: there is no write path. Who may read it is the viewer gate's job.

Revision probe: the layer answers `/__preview/revision` with the captured SHA (production's `/api/health` would report production's build), and stamps `x-cf-preview-revision` on every response.

Cleanup: the Pages lifecycle as before: owned domain, DNS record and `pr-N` deployments of the dedicated project, ownership proven by the marker (which names that project). Retire removes every implemented mode's resources (today `ui`'s) whatever the labels say at close. The read grant and the Access applications are per deployment, not per PR, and outlive every preview.

### `stack`: the PR's full build over scratch data (specified)

Deploys: the PR's whole site, front end and Functions, as built by the credential-free job.

Data: a scratch D1 database and R2 bucket per PR, both named in the reserved namespace (`<project>-pr<N>`), created at publish and deleted at retire. The D1 gets the PR's own `site/schema.sql` (a PR that changes the schema is what `stack` is for) and a small seed; the R2 bucket gets a fixture store (a few scans' served files, generated or copied from the public demo store), never a copy of a gated deployment's data. No production binding, secret or store credential is given to the deployment.

Credentials: the deploy token additionally needs D1 Edit and R2 Edit on the account; the deployment's only bindings are its own scratch D1 + R2. Viewers sign in through the same Access application as `ui`; the PR's own sign-in routes cannot work on preview hosts (gate 4), so the deployment runs with a fixed scratch identity (to be specified with the site's auth owner) instead of real sessions.

Ownership and cleanup: a `_cf_preview_owner` row in the D1 and a `.cf-preview-owner` object in the bucket carry the owner marker; retire proves both before deleting, empties the bucket with full pagination, then deletes bucket, database and the Pages resources. Retire must also handle a mode switch (a `ui` preview replaced by `stack` and back): a publish first retires the PR's other mode's resources on the same host. The Workers native Previews adapter (`publishWorker`) is the alternative provider here.

### `pipeline`: the PR's job code over a scratch prefix (specified)

Deploys: nothing to Cloudflare. A credential-free job builds the job image from the PR head and uploads it as an artifact; a trusted job pushes it to a preview image repository under an owned tag (`pr-<N>-<sha>`) and dispatches the deployment's job (Batch, through `batch.submit_job`, in the bucket's region) with that image.

Data: reads of declared inputs only (a fixed listing or a public demo store), writes only under a scratch prefix (`<scratch bucket>/pr-<N>/`), through a service account whose write grant is conditioned on that prefix. The job's results (exit status, summary, links to the outputs) are commented on the PR.

Credentials and gates: `pipeline` runs PR code with cloud credentials, so besides the usual authorization it is never the default; it runs only when a `preview:pipeline` label or a `/preview pipeline` comment asks for it. Cleanup on close deletes the prefix (and a bucket lifecycle rule expires it as a backstop) and the image tag.

## Activation gates

1. Pass lifecycle/ownership/authorization tests and a real disposable preview's create, update, close/retry cleanup, DNS and certificate verification on each provider adapter.
2. Publish the reviewed reusable action and pin callers to its source SHA. Adopt the small caller workflow on the branch from which GitHub actually reads its events.
3. Review each caller's actual preview bindings/secrets and scoped Pages/Workers versus DNS tokens. Never give an untrusted build job those credentials.
4. Reconcile the exact custom-host callback with OA broker/client policy; no wildcard callback widening or per-PR manual Google client creation.
5. Enable one caller/target, then expand. Do not hold normal dev/prod release setup hostage to a broader preview-data architecture decision.

For `ui`:

- Gate 3 is met by construction for bindings and secrets: publish refuses a dedicated project with any binding or any variable but the read grant, the layer refuses to run with one, and Wrangler runs where no Functions or wrangler config can be picked up. What stays to review per caller is the read grant (base scope, not admin; whether to path-restrict it), the Access policy (who may look) and the deploy/DNS tokens' scopes.
- Gate 4 does not apply: `ui` uses neither the deployment's sign-in nor the broker; viewers sign in through Access. It still applies to `stack`.
- Gate 1, per caller before enabling: a real disposable preview's create, update, close/retry; Access redirects an anonymous visit, refuses the `pages.dev` alias (421) and admits a listed reviewer; a `POST` gets a 405 and `/api/token` a 403 from the live host; `wrangler pages deploy` uploads the generated `_worker.js` as the deployment's worker (advanced mode) with the replaced `_routes.json`; and the SPA fallback works through the layer (Pages applies the PR's `_redirects` to `ASSETS.fetch`; if it does not, the layer needs an `/index.html` fallback for unmatched navigations).
- Per deployment, once (each needs Ryan's go): the dedicated Pages project with its preview secret, the Access application on `*.<preview domain>` plus the revision Bypass application, the read grant, the `.github/cf-preview.json` `modes.ui` block, and the caller workflow.

[JCT]: https://github.com/HackJerseyCity/jc-taxes/blob/main/.github/workflows/preview.yml
[OA www]: https://github.com/Open-Athena/open-athena.github.io/blob/main/.github/workflows/deploy-preview.yml

Validation: 29 dependency-free Node tests (`node --test .github/actions/cf-preview/preview.test.mjs`) cover mode selection and comment parsing, the `ui` plan and settings validation, the layer's method/path classification, forwarding (exactly which headers and credential go upstream, cookies and redirects on the way back), refusal of writes and non-allowlisted paths without reaching production, the Access gate (valid viewer, missing / malformed / wrong-key / wrong-signature / other-team / other-application / expired / not-listed assertions), host and binding refusals, the dedicated project and Wrangler-directory checks, staging of the PR's assets under a self-contained generated `_worker.js`, both full-build adapters, collaborator/current-head checks, pagination, ownership/production exclusions, repeat cleanup, symlink rejection, bounded TLS and exact-revision host checks. No CF preview, DNS record, Access application, grant, runtime binding or GH environment was created. This spec stays open until real create/update/close/retry and the `ui` gate-1 checks above pass on one caller.
