# Reusable Cloudflare PR previews

Status: source prototype implemented and tested, November 10, 2026; live activation pending the gates below. Requested by Ryan while setting up the four OA deployments. Applies to arbitrary callers, not only HF; HF is merely the initial dev/prod release pilot. The existing dev/prod controller remains a separate lane.

## Prior art

[JCT]'s preview workflow has the reusable trust pattern: current repository write/maintain/admin authors get automatic previews; others need a current maintainer's `/preview` approval of the captured head SHA. Builds execute without CF credentials, then a trusted base checkout uploads the built Worker/assets with an allowlisted configuration. It publishes a native `workers.dev` URL; `prN.jct.rbw.sh` is not provisioned and retirement is manual.

[OA www] deploys Pages branch previews, updates a PR preview link, and has a cleanup workflow. It does not register a custom hostname per PR. Its cleanup currently covers only one of the two preview projects, looks at the first deployment page, and selects branch names without a resource ownership receipt. Do not copy these gaps into the shared tool.

## Deliverable

A dependency-free Node toolkit and composite action under `.github/actions/cf-preview`, consumable from another repo by a pinned `Open-Athena/disky/.github/actions/cf-preview@<sha>` reference. Caller-owned build/test instructions and explicit preview resource configuration stay in the caller; the common component owns authorization, naming, exact-head revalidation, CF domain/DNS lifecycle, deployment readback, a secret-free receipt and cleanup. It must work with Pages and Workers, without executing PR-supplied config or build hooks in the credentialed job. A future move to a dedicated repository must preserve this small interface rather than duplicate four Disky workflows.

The caller builds the approved head on a separate credential-free runner, retains all Worker modules (including WASM), and uploads an artifact. The publish job checks out trusted base tooling/config and installs a pinned Wrangler from that trusted checkout. The action invokes Wrangler as argv, never a shell command supplied by the PR. Pages deploys a reserved `pr-N` branch of the configured project; Workers creates a native named Preview `prN` under an existing parent, using caller-provided preview bindings, no inherited production routes/build hooks/triggers and `--ignore-base-config`. Wrangler must be at least 4.135.0; installed 4.150.0 was inspected for the Preview API/annotation contract. Built code still receives its configured runtime bindings: hiding the upload token does not make a malicious backend harmless.

`prN.<base-domain>` is registered for that PR, with a per-PR custom-domain binding and proxied DNS record for Pages. Workers uses a one-time parent custom-domain configuration with `previews_enabled: true`; Cloudflare owns its shared wildcard DNS/certificate and the named Preview owns the per-PR route. Native Preview cleanup removes its deployments, not the parent or shared wildcard baseline. Publish rechecks the PR head and permission immediately before changes. External updates require another explicit approval. Closed PR cleanup verifies current PR state, provider/project, exact reserved name, ownership marker and production exclusions; it paginates complete result sets, handles missing owned resources idempotently and refuses to delete unknown resources. Partial CF failures remain visible and retryable, never a false cleanup success. A custom-origin probe must return the captured full SHA (text endpoint or configured header), so a reachable host accidentally serving production cannot pass.

The existing project's production deployment and stable dev alias must remain untouched. Preview backend data isolation, runtime secret provisioning and broker callbacks are explicit caller policy. Disky's present dev stacks share production D1/R2; Ryan has been asked whether PR previews may do the same. No such policy is inferred from the generic action. The auth owner has been asked for the supported dynamic callback-registration contract before enabling live preview sign-in.

## Activation gates

1. Pass lifecycle/ownership/authorization tests and a real disposable preview's create, update, close/retry cleanup, DNS and certificate verification on each provider adapter.
2. Publish the reviewed reusable action and pin callers to its source SHA. Adopt the small caller workflow on the branch from which GitHub actually reads its events.
3. Review each caller's actual preview bindings/secrets and scoped Pages/Workers versus DNS tokens. Never give an untrusted build job those credentials.
4. Reconcile the exact custom-host callback with OA broker/client policy; no wildcard callback widening or per-PR manual Google client creation.
5. Enable one caller/target, then expand. Do not hold normal dev/prod release setup hostage to a broader preview-data architecture decision.

[JCT]: https://github.com/HackJerseyCity/jc-taxes/blob/main/.github/workflows/preview.yml
[OA www]: https://github.com/Open-Athena/open-athena.github.io/blob/main/.github/workflows/deploy-preview.yml

Validation: 19 dependency-free Node tests cover both adapters, collaborator/current-head checks, pagination, ownership/production exclusions, repeat cleanup, native Preview readback/config, symlink rejection, bounded TLS and exact-revision host checks. The example workflow and CI workflow pass actionlint. No CF preview, DNS record, runtime binding or GH environment was created by this prototype. This spec stays open until real create/update/close/retry, auth and data-policy acceptance pass.
