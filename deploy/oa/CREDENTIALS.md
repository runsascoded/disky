# OA deployment credentials

The release target is a Cloudflare Pages project. GitHub environments wrap the existing direct upload with secret access, approval and history; they do not own the application, its indexes or its runtime credentials. The same build and upload commands remain usable from a checkout.

## Site releases

Each of `gcs-dev`, `gcs-prod`, `cw-dev`, `cw-prod`, `hf-dev`, `hf-prod`, `r2-dev`, `r2-prod` needs only:

| Name | Type/location | Consumer | Required authority |
| --- | --- | --- | --- |
| `CF_PAGES_DEPLOY_TOKEN` | GitHub environment secret | Approved upload and Pages readback steps | Cloudflare account Pages Edit, restricted to the OA account; record the actual provider policy when provisioned. Pages tokens are not assumed to be project-scoped. |
| `CLOUDFLARE_ACCOUNT_ID` | GitHub environment variable | Wrangler and Pages readback | Nonsecret account selector; use the OA account already owning the allowlisted project. |
| `GITHUB_TOKEN` | Job-scoped token minted by Actions | Plan, CI proof, explicit deployment/status records | Contents Read + Actions Read; Deployments Write only in the approved release job. No repository push permission. |

A release does not need D1 write, R2 data access, zone/DNS edit, GCP credentials, source bucket credentials, broker secrets or Slack credentials. Dependency installation and building receive no Cloudflare secret. Never copy an entire worktree environment into an Actions environment. The Pages project's existing runtime secrets remain in Pages; this release job does not read or rewrite their values.

Use separate dev/prod token instances for revocation and audit where practical, while recognizing that account-wide Pages Edit does not itself prevent a token from uploading another project. The controller's explicit project/branch allowlist is an additional software check, not a provider permission boundary. Store operator recovery material in the team's vault, with owner, token ID, policy, expiry and rotation procedure. Do not document token values in Git.

## Existing roles and observed local names

This is a **source-use and variable-name audit**, November 10, 2026. It does not certify live token policies, expiry, equality or effective inherited values. Reproduce the safe inventory with `scripts/oa-release credential-names <explicit .envrc paths>`. That command emits assignment identifiers and an inheritance flag only; it never prints source lines, values or arguments to `source_up`/`source_env`. An inherited value cannot be inferred solely from the child's assignments.

| Role | Observed names | Consumers and required separation |
| --- | --- | --- |
| Manual site deploy / pipeline D1 writer | `CLOUDFLARE_API_TOKEN` | `site/deploy`, `index_footer._d1_query`, scan registration and index publication all use this generic name. They require different permissions. Its name does not establish which a local value actually grants; separate Pages-only CI from pipeline D1 Edit. |
| Pipeline's Pages publisher | `CF_PAGES_TOKEN` | `scan_steps` can override the generic token for its digest Pages upload. Existing source supports the role; absence in a local file does not establish absence from Secret Manager. |
| Infrastructure changes | `CF_IAC_TOKEN` | Deployment infrastructure provider; inventory its actual resource policy separately. Do not place this in routine site release environments. |
| Usage snapshots | `CF_ANALYTICS_TOKEN` | Optional `scan_steps` CF usage snapshots. Intended Account Analytics Read; not a site deployment credential. |
| GCS pipeline CF credential | `CF_BATCH_TOKEN` | GCS-specific local name. Exact consumer and live policy require owner verification; do not assume it is interchangeable with the D1 or Pages token. |
| Derived index object read/write | `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`, `R2_STATIC_IDX_RW_*` | Publication, static/interval builds and generation maintenance. Scope to the deployment's derived bucket; cleanup/delete authority is separate from a reader. These are S3-compatible keys, not a reason to grant Pages deploy jobs R2 data access. |
| Public demo cross-account listing | `R2_HCCS_RO_*`, `R2_RAC_RO_*`, `R2_RO_*` | Root/demo ingest, source-read roles across accounts. Keep out of OA site's release environments. |
| Public demo publication | `R2_RW_*` | Root/demo derived outputs; distinct from OA deployments' buckets. |
| Front-door site / observability | `CF_FRONT_DOOR_TOKEN`, `CF_OBSERVABILITY_TOKEN`, `CF_GHA_DISPATCH_TOKEN` | Root coordination names. Their names alone do not prove effective permissions. The front-door workflow and fleet reader use dedicated roles; do not reuse these for four deployment releases. |
| Job notification / actions | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `DISKY_SLACK_BOT_TOKEN` | Pipeline digests, Slack actions and fleet rollup respectively. Signing secrets belong at the request-verifying runtime, not in a Pages upload job. |
| Broker and app sessions | `OA_LOGIN_CLIENT_SECRET`, `SESSION_SECRET`, other application secrets | Existing Pages runtime/secret manager. A deployment does not mint broker clients or Google clients. |

| Worktree | Locally assigned role names | Inheritance |
| --- | --- | --- |
| GCS | Generic CF token/account, IaC, Batch, analytics, static-index R2 keys, Slack | No `source_up`/`source_env`/dotenv directive detected |
| CW (`wt/cw`, branch `cw`) | Generic CF token/account, IaC, analytics, R2 key pair, Slack, CoreWeave source keys | Yes |
| HF | Generic CF token/account, IaC, analytics, zone ID | Yes |
| R2 | Analytics token | Yes; other effective credentials are inherited or runtime-provided |

Remaining credential audit gates: owner-confirm each live token's ID/policy/expiry and all Secret Manager/runtime consumers; identify unused or overbroad credentials without revoking them; provision/reuse the Pages-only instances; record vault recovery and environment ownership. Token verification alone confirms status, not the full policy. No token has been rotated, revoked, created or copied into GitHub by this implementation.

## Qualified source for the first pilot

Cloudflare account-console review on November 10 identified active token `26aeaa3d6a7309d54e4dd66768c427b6`, named `oa-hf-usage pages (job: digest plot)`, with Pages Write for the OA account and no other displayed roles. HF source names its Secret Manager key `hf-usage-cf-pages-token` in project `oa-internal-450019`; this is a candidate for owner-approved reuse, not a claim its stored value has been compared or installed into GitHub. D1/R2 credentials remain separate. GCS automation remains held pending its dedicated Pages-only token qualification.

PR custom hostnames add a separate zone DNS Write role for Pages. The OAx console currently has an unsubmitted `disky-pr-preview-dns (CI)` token review restricted to `oa.dev`, with a proposed 90-day expiry (February 8, 2027). User creation/save as `CF_PREVIEW_DNS_TOKEN` is pending. No one-time secret should be pasted into chat or copied from a browser transcript. Workers native Previews use a separate Worker authority and a preconfigured parent wildcard domain, not this Pages-only deploy token. See [preview lifecycle] for the explicit binding/auth policy and live-pilot requirements.

[preview lifecycle]: ../../.github/actions/cf-preview/README.md
