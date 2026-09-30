# Cloud resources as code (Pulumi): codify the existing public-demo deploy

Design-only. Everything below **already exists**, provisioned by hand (`wrangler`, CF API, console) across `specs/done/cloud-reduce.md`, `pages-auth.md`, and `cfn-demo-and-flask-localhost-peer.md`. The goal is drift detection + reproducibility, not new resources — so Pulumi **imports** the live objects and the first `pulumi up` is a no-op diff. Adapts mgu's `specs/cf-iac.md` (that plan's own first `pulumi up` is also import-then-no-op); the transferable core is below, the deltas are dt-specific.

## What transfers from mgu's `cf-iac.md`

- **"Import, don't recreate."** Everything is live; create-from-scratch would 409. Bootstrap the program, `pulumi import` each object, land the code so the first diff is empty — that alone buys drift detection. (mgu's Order step 1, verbatim intent.)
- **One CF provider, account-pinned, one program parameterized by deployment.** mgu runs two stacks (`gcs`, `cw-s3`) off one program; dt runs `demo` (+ optionally `private`) off one program. Env vars from a small config block; secrets as Pulumi secrets *only where they aren't already Pages secrets* (dt keeps them as Pages secrets — see Manual).
- **First-class providers exist for all of it.** `@pulumi/cloudflare` has `R2Bucket`, `PagesProject`, `PagesDomain`, `DnsRecord`, `D1Database`, `ZeroTrustAccessApplication`/`…Policy`. No custom resources needed.
- **Deployments stay out of IaC.** Pulumi owns the *container* (project shell, bucket, domain, D1); `wrangler pages deploy` keeps *filling* it, and D1 migrations stay with the app (`ui/migrations/`, `d1 migrations apply`). This is the load-bearing split.

What does **not** transfer: mgu's `@pulumi/gcp` half (Cloud Scheduler / Batch / Secret Manager) — dt has no GCP. dt's re-scan cadence is GHA cron (`.github/workflows/rescan-demo.yml`, `reduce.yml`), which is repo-tracked YAML, not a cloud resource; leave it as-is. mgu's Zero Trust Access apps map onto dt's **private** stack only (the demo is `PUBLIC_OPEN`, no auth). And mgu is an OA-org project living in the shared `~/c/oa/ops` Pulumi repo; dt is self-contained (see Where).

## Accounts (resolved 2026-09-10, by the user)

Three CF accounts, confirmed:
- **RAC** (`Ryan@runsascoded.com`) — `0dcad5654e9744de6616f74b8df4af63` = `.envrc`'s `CLOUDFLARE_ACCOUNT_ID` and the R2 endpoint host. **Holds the demo:** `disk-tree-demo` bucket + Pages project + the `r2.rbw.sh`/`rbw.sh` zone (+ the private `disk-tree` project/bucket + `disk-tree-auth` D1). This is dt's one **managing** account.
- **OA** (Open Athena) — `74981a43be0de7712369306c7b19133d`. mgu's home; nothing of dt's. (`cfn-demo-and-flask-localhost-peer.md`'s "Open Athena" label on `0dcad…` was wrong — that id is RAC.)
- **HCCS** — `2363642879f18d37d52dca114059937e`. The source buckets are moving here (`ctbk`/`nj-crashes`/`path` read via the `CF_HCCS_R2_*` RO key; `jc-taxes` appears to still be RAC per the `disk-tree-wrangler` R2 policy). **Foreign-owned — dt reads, never manages.**

So one managing CF provider, pinned to RAC `0dcad…`; the source buckets (RAC or HCCS) are reference-only either way.

## 1. Resource inventory

Demo (`disk-tree-demo`, public, `PUBLIC_OPEN`) is the target; the private `disk-tree` (gated laptop scan) is a second, optional stack. All rows **EXIST**; "new" column is empty by design.

| # | Resource | Identity | Account | Config source today | Pulumi type | Manage? |
|---|---|---|---|---|---|---|
| D1 | R2 bucket | `disk-tree-demo` | `0dcad…` | manual (`wrangler r2`/console) | `R2Bucket` | **demo stack** |
| D2 | Pages project | `disk-tree-demo` (`disk-tree-demo.pages.dev`) | `0dcad…` | `ui/wrangler.demo.toml` + first `wrangler pages deploy` | `PagesProject` | **demo stack** (shell only) |
| D3 | Custom domain | `r2.rbw.sh` on project `disk-tree-demo` | `0dcad…` | CF API `pages/projects/…/domains` | `PagesDomain` | **demo stack** |
| D4 | DNS record | `CNAME r2 → disk-tree-demo.pages.dev` in zone `rbw.sh` | `0dcad…` | auto-provisioned by D3 (same-account) | `DnsRecord` | **demo stack, `import`+`ignoreChanges` or leave implicit** (see §2) |
| D5 | R2 binding `SCANS`→`disk-tree-demo`, `[vars] SCANS_PREFIX`, `PUBLIC_OPEN` | attributes of D2 | `0dcad…` | `ui/wrangler.demo.toml` `[[r2_buckets]]`/`[vars]` | `PagesProject.deploymentConfigs` | **NO — stays wrangler's** (see §2 two-writer note) |
| — | Source buckets `ctbk`, `nj-crashes`, `jc-taxes` (`path`/`hbt` later) | read-only corpus | `0dcad…` today / HCCS future | owned by other public-data projects | `R2Bucket` (ref only) | **NO — reference/`import`-not, foreign-owned** |
| P1 | R2 bucket | `disk-tree` | `0dcad…` | manual | `R2Bucket` | private stack (optional) |
| P2 | Pages project | `disk-tree` (`disk-tree.pages.dev`) | `0dcad…` | `ui/wrangler.toml` + deploy | `PagesProject` | private stack (shell only) |
| P3 | D1 database | `disk-tree-auth` `c17a63c1-1d6f-443c-9d35-f1e9c5e674ac` | `0dcad…` | `wrangler d1 create` | `D1Database` | private stack |
| P4 | R2 binding `SCANS`, D1 binding `DB`, `[vars] SCANS_PREFIX` | attributes of P2 | `0dcad…` | `ui/wrangler.toml` | `PagesProject.deploymentConfigs` | **NO — stays wrangler's** |
| P5 | Zero Trust Access app **disk-tree** + reusable policy **disk-tree allowlist** + Google/OTP IdPs | on `disk-tree.pages.dev/auth/sso` | `0dcad…` Zero Trust org | console/API (`pages-auth.md` §Deployed) | `ZeroTrustAccessApplication` + `…Policy` | private stack, **optional** (see §2/§5) |
| P6 | D1 schema / migrations `0001`–`0007` | rows in P3 | — | `ui/migrations/*.sql`, `d1 migrations apply` | — | **NO — stays app** |

The private deploy is optional for a first pass — the task's primary target is the public demo. Doing demo-only keeps the blast radius tiny and defers the auth-app import (the fiddliest part).

## 2. Import strategy

Import ID shapes for `@pulumi/cloudflare` (verify against the pinned provider major — v5→v6 renamed `Record`→`DnsRecord` and moved Access policies to account-level reusable objects; treat these as the shape, confirm the exact string in the provider docs at pin time):

| Resource | Pulumi type | `pulumi import` ID | Notes |
|---|---|---|---|
| R2 bucket | `R2Bucket` | `<account_id>/<bucket_name>` | e.g. `0dcad…/disk-tree-demo`. Cleanly importable. |
| Pages project | `PagesProject` | `<account_id>/<project_name>` | Importable; pulls current `deploymentConfigs` (bindings/vars) into state — see two-writer note below. |
| Custom domain | `PagesDomain` | `<account_id>/<project_name>/<domain>` | e.g. `0dcad…/disk-tree-demo/r2.rbw.sh`. |
| DNS record | `DnsRecord` | `<zone_id>/<record_id>` | Needs zone id of `rbw.sh` + the record id (list via API). See implicit-record note. |
| D1 database | `D1Database` | `<account_id>/<database_id>` | e.g. `0dcad…/c17a63c1-…`. |
| Access app | `ZeroTrustAccessApplication` | `<account_id>/<app_id>` | Account-level (Pages self-hosted app). |
| Access policy | `ZeroTrustAccessPolicy` | `<account_id>/<policy_id>` | v6 reusable policy; get id from the app. |

**Two-writer hazard (the central caveat).** A `PagesProject` import drags the live `deploymentConfigs` — R2/D1 bindings and `[vars]` (D5/P4) — into Pulumi state, but those are authored in `ui/wrangler*.toml` and re-applied on **every** `wrangler pages deploy`. If Pulumi also declares them, the two writers fight and each `up`/`deploy` shows spurious drift. **Resolution: `wrangler.toml` stays the sole source of truth for bindings/vars; Pulumi manages the project *shell* only** and drops `deploymentConfigs` via `ignoreChanges: ["deploymentConfigs"]` (or declares them to *match* and accepts wrangler as the effective writer — less clean). This preserves the mgu split (Pulumi owns the container, wrangler fills it) at the binding granularity.

**Implicit DNS record (D4).** Attaching the custom domain same-account auto-created the `r2` CNAME; CF "manages" it implicitly. Two options: (a) `import` it and `ignoreChanges` its content so Pulumi only records existence, or (b) manage only `PagesDomain` (D3) and leave the CNAME as CF-implicit (don't import). Recommend (b) for the demo — fewer moving parts, the record's lifecycle is already bound to the domain attachment. Revisit if a bare-`rbw.sh`/`gcs.rbw.sh` record ever needs explicit management.

**Not importable / stays manual:** the Access **IdPs** (Google-runsascoded OAuth client + One-time-PIN) are Zero Trust org-level identity config, not per-app resources — the provider can reference an IdP by id in a policy but the OAuth client/secret provisioning (GCP consent screen, redirect URI) is out of band (`pages-auth.md` §5). Pages **secrets** (below) are never in state. D1 **migrations** are app-owned.

## 3. Provider / stack layout

- **One CF provider**, `accountId = 0dcad…` (from config/`CLOUDFLARE_ACCOUNT_ID`), token from `CLOUDFLARE_API_TOKEN` — the same `disk-tree-wrangler` token already in `.envrc`, which needs Pages/R2 read at minimum for `refresh`/`up` (import/manage may need broader scopes than deploy-only; widen the token or use a dedicated Pulumi token). **No HCCS provider now** — dt owns nothing in HCCS; the source buckets are foreign-owned (reference-only) whether they sit in `0dcad…` today or HCCS later. If Phase-4 `path`/`hbt` ever need *referencing* by id, do it with data-source lookups, not a managing provider.
- **Stacks:** `demo` (D1–D4, primary) and optionally `private` (P1–P3, +P5 later). One program, a per-stack config block selecting `{ projectName, bucketName, domain?, d1?, access? }` — mirrors mgu's one-program/two-stack shape. Start `demo`-only; add `private` once the demo import is a verified no-op.
- Pulumi state backend: reuse whatever dt/personal standard is (mgu uses `gs://oa-pulumi` + KMS — an OA convention that doesn't fit a personal project). For a self-contained dt, prefer a personal backend (Pulumi Cloud free tier, or an R2/S3 self-managed backend); **decide before `pulumi login`.** Open question.

## 4. Where it lives

**A dt-repo `iac/` dir** — dt stays self-contained (its CLAUDE.md vision is a standalone tool + its own demo), unlike mgu which is an OA-org project that belongs in the shared `~/c/oa/ops`. `iac/` sits beside `ui/`, holds the `@pulumi/cloudflare` TS program + `Pulumi.yaml` + `Pulumi.demo.yaml`/`Pulumi.private.yaml`. No secret material in git: `Pulumi.<stack>.yaml` holds only non-secret config (account id is already in `.envrc`/specs; bucket/project/domain names are public); any Pulumi secret is stored encrypted by the state backend, and dt's identity secrets don't enter Pulumi at all (§5). `~/c/oa/ops` remains mgu's home; if a shared `CfnDashboard` component ever materializes (mgu's cf-iac §"Should upstream offer primitives"), dt's `iac/` is the natural first consumer, but that's a later extraction, not this spec.

## 5. What stays manual

- **Pages secrets** (identity-bearing, `wrangler pages secret put`): private stack's `SESSION_SECRET`, `ALLOWED_EMAILS`, `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`. The demo has **none** (`PUBLIC_OPEN`, no D1). Never in git, never in Pulumi state — Pages is their store, `pages-auth.md` §3 is their runbook.
- **Deployments**: `wrangler pages deploy` (both projects) — Pulumi never builds/uploads.
- **Bindings + `[vars]`** (D5/P4): `wrangler*.toml` (§2 two-writer note).
- **D1 migrations** (P6): `ui/migrations/*.sql` + `d1 migrations apply`.
- **Access IdPs / OAuth client** (§2): Zero Trust org config + GCP consent screen, out of band.
- **Re-scan pipelines**: `.github/workflows/{rescan-demo,reduce}.yml` (repo YAML, not cloud resources); their R2 tokens are GHA secrets.
- **Source buckets** `ctbk`/`nj-crashes`/`jc-taxes` (+ future `path`/`hbt`): foreign-owned; dt reads via keys, never manages. At most a commented reference.

## 6. Phasing

1. **Bootstrap `iac/`** — `Pulumi.yaml`, `@pulumi/cloudflare`, provider pinned to `0dcad…`; pick the state backend (§3 open question); resolve the account label (§Reconciliation). No resources declared yet.
2. **Import the demo shell** — declare + `pulumi import` D1 (`R2Bucket disk-tree-demo`), D2 (`PagesProject disk-tree-demo`, `ignoreChanges: deploymentConfigs`), D3 (`PagesDomain r2.rbw.sh`). Leave D4 implicit (option b).
3. **Verify no-diff** — `pulumi refresh` + `pulumi up` shows an **empty** plan. Cross-check a `wrangler pages deploy` still lands cleanly (no binding/var drift reported) — proves the two-writer split holds. This is the whole payoff of the exercise: drift detection with zero behavior change.
4. **Manage forward** — from here, bucket/project/domain changes go through `iac/`; bindings/vars/secrets/deploys stay on their manual tracks (§5).
5. **(Optional) private stack** — repeat 2–4 for P1–P3; import P5 (Access app + policy) last, or defer indefinitely if the auth app is stable and rarely touched (import value is low, import friction is highest there).
6. **(Optional, later) HCCS reference** — if `path`/`hbt` land in HCCS and the demo needs to *name* them, add data-source lookups (not a managing provider); revisit whether a read-only HCCS provider is worth it.

## Open questions

- ~~Account identity/label~~ — **resolved** (see Accounts): `0dcad…` = RAC, holds the demo; one managing provider.
- **State backend** — Pulumi Cloud vs self-managed (R2/S3) for a personal, self-contained dt. (§3)
- **Token scope** — does `disk-tree-wrangler` (Pages Write + R2 Read [+ D1 Write]) cover `pulumi import`/`refresh` of PagesProject/PagesDomain/R2/D1, or is a broader/dedicated Pulumi token needed?
- **Private stack now or later** — demo-only first pass is recommended; confirm.
- **D4 DNS record** — leave implicit (recommended) or import with `ignoreChanges`?
