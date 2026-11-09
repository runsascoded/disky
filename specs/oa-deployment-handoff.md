# OA deployment handoff and operator runbook

Status: source audit and shared tooling fixes, 2026-11-09. Requested by Ryan so Louisa can operate all four deployments during his two-month leave. Operator access and a release rehearsal remain unverified. No site, image, database, infrastructure or permission was changed by this audit.

## What is shared

All four have the same broad architecture: the shared Python/TypeScript code, a Cloudflare Pages site over deployment-owned D1/R2 resources, GCP-built scan images, GCP Batch/Scheduler pipelines, and separate Cloudflare/GCP Pulumi stacks. The object-store API differs; the control plane remains Cloudflare plus GCP even for HF and R2. A Git push is not the OA site's deploy command.

There are four separate release actions: site code/config, scan image, D1 schema recipes, and infrastructure/secrets. `site/deploy` performs only the first; it does not ship a new scanner or apply recipes. An operator should learn one release protocol with deployment-specific configuration, rather than four unrelated procedures.

## Published-source comparison

Audited freshly fetched `o/gcs` (`8683dbf19477`), `o/cw` (`4e8d412dc01a`), `o/hf` (`24ef71d1f434`) and `o/r2` (`497b04b8e07a`). These identify published source, not independently verified running revisions. Several local owner worktrees differ materially from these branches; use the published release checkout for handoff. In particular, the local GCS checkout was older while the local CW checkout had an unpublished newer deploy helper.

A subsequent read-only query of the four Pages projects confirmed successful canonical production deployments and the production-branch settings below. Cloudflare's reported site revisions were GCS `4755a970ecbc` (clean), CW `ff6cc4eac769` (dirty), HF `24ef71d1f434` (dirty) and R2 `5f94cba5c82f` (clean). The dirty flags do not prove a particular site file changed; they mean the commit alone cannot certify the build's exact input tree. Establish a reproducible committed baseline for CW/HF before handoff. A site revision can legitimately differ from a later job-only source revision; record each component separately. Minimal nonsecret readback is saved in root `tmp/oa-handoff/live-pages.json`.

| Item | GCS | CW | HF | OA R2 |
| --- | --- | --- | --- | --- |
| Source branch / host | `gcs` / `gcs.oa.dev` | `cw` / `cw.oa.dev` | `hf` / `hf.oa.dev` | `r2` / `r2.oa.dev` |
| Pages project | `oa-gcs-usage` | `oa-cw-s3-usage` | `oa-hf-usage` | `oa-r2-usage` |
| Site entrypoint | `site/deploy` | `site/deploy` | `site/deploy` | `site/deploy` |
| Pages prod / preview branch | `main` / `dev` | `main` / `dev` | `main` / `dev` | `r2-prod` / `r2` |
| Published dry-run/config guard | Present | Older 110-line helper; upgrade required | Present | Present |
| Job builder | `job/build.sh` | `job/build.sh` | `job/build.sh` | `job/r2-scan build TAG` |
| Image context | Root Dockerfile + filtered checkout | Root Dockerfile + filtered checkout | Minimal staged context + `job/Dockerfile` | Minimal staged context + `job/Dockerfile` |
| Default image destination | Live `:latest` tag | Live `:latest` tag | Live `:hf` tag | Explicit build tag; scheduler pins digest |
| GCP region | `us-central1` | `us-central1` | `us-central1` | `us-west1` |
| CF + GCP state backend | `gs://oa-pulumi` | `gs://oa-pulumi` | `gs://oa-pulumi` | Local `infra/{cf,gcp}/state/` |
| Pulumi secret protection | GCP KMS | GCP KMS | GCP KMS | Local passphrase |

**Every published preview binds the same D1 database and R2 serving bucket as its production site.** A preview is a code/auth-preview environment over production data, not an isolated data sandbox. A migration, deletion or admin operation from preview can therefore affect production. App admin policy and secrets must remain consistent where the same database is shared.

The status helper was byte-identical across all four published branches, but selected the newest successful deployment regardless of environment. The shared fix now selects the canonical production deployment, or a successful deployment of the exact configured preview branch, and refuses to certify dirty deployment/local trees by commit alone. The deploy wrapper forwards `--status --dev`. It also has safe `--help` and avoids Bash-4-only host expansion, so the offline preflight works under macOS's system Bash. Adoption is still required on each deployment branch; root-local changes are not deployed changes.

## Before leave: completion criteria

1. **Recoverable state.** Move R2's two state backends to an agreed OA-controlled backend, or establish an OA-controlled encrypted backup and demonstrate restore on another machine. The passphrase must be recoverable independently of Ryan's laptop. Do not merely copy a state directory without testing decryption. A backend move is reviewed infrastructure work; do not change the backend URL or initialize a second stack over live resources blindly.
2. **Uniform tooling.** Carry the shared deploy/status fixes to all four published branches; CW also needs the already-existing TOML validation/dry-run guard (`616b6d44`, `25d05693`, `d7c65eb0`, `e0ac5bde`, in history order; review their accompanying tests/CI changes). Its published branch does not yet contain `site/toml-check`. Reconcile the operator sections of `AGENTS.md`: the current helpers keep `<deployment>-prod/dev` pointers locally and push the source branch, while some older prose says a prod pointer is pushed too. Use the script as authority until the prose is fixed.
3. **Candidate first.** Use an explicit candidate image destination for GCS/CW/HF; their default build commands update the live tags. Resolve and record the candidate digest, verify its source stamp and run the owner-approved smoke against that digest before promotion. Keep R2's existing scheduler digest pin. All four release records must retain the previous image digest.
4. **A clean published source baseline.** Do not hand off local-only commits, dirty worktrees or untracked source as the authoritative release. Current image guards inspect tracked changes; included untracked source is an additional provenance gap to audit before calling a build reproducible. The site build marks tracked changes dirty, but a commit alone still cannot reconstruct those bytes.
5. **Schema ordering.** Verify the live schema read-only before applying a recipe, apply only the unapplied recipes required by the release, and keep site/image compatibility explicit. A recipe present in Git may already have been applied; HF currently has local deletions of applied recipes that are not in its published branch. Do not apply every file blindly or load `schema.sql` over a live database.
6. **Independent operator rehearsal.** Louisa must perform the workflow using her own account and a clean checkout, without Ryan's private `.envrc`, browser profile, ADC cache or untracked root helper. Record pass/fail and the missing permission rather than substituting Ryan's successful deployment.

## Operator access matrix

All rows below are **unverified for Louisa**. A source audit cannot establish her effective IAM, credential access or recovery ability.

| Surface | Required ability | Verification |
| --- | --- | --- |
| GitHub | Read source, push reviewed deployment branches, view/dispatch relevant Actions; shared-code contribution path to `runsascoded/disky` | Clone OA branches and fetch the shared base from a clean machine; verify a harmless reviewed push/CI run |
| Cloudflare Pages | Read each project/deployment, deploy preview and prod | Correct project's status read, preview deployment and authenticated smoke |
| Cloudflare D1 | Read/check each live schema; apply a reviewed recipe when required | `dt-cloud d1-schema check -d DB -c site/wrangler.toml`; recipe apply remains separate |
| Cloudflare IaC | Access the intended CF stack token and resources; DNS/KV/R2 changes where needed | Decrypt the stack and produce a reviewed `pulumi preview`; do not confuse the Pages/D1 token with `CF_IAC_TOKEN` |
| GCP | Cloud Build submission, image read/promotion, Batch/Scheduler operations and logs; appropriate service-account impersonation | Candidate build, source-stamp smoke and job/log readback under her account |
| Pulumi state/KMS | Read/write the correct state backend and decrypt its stack secrets | Preview from a clean machine; R2 restore proof as above |
| Secret recovery | Locate service-owned credentials, required versions/expiries and renewal owners | Verify secret names/version existence without printing values; no fresh secret rotation during rehearsal |
| App/on-call | Sign in to each site and reach intended diagnostic pages | Browser verification; app admin scope is a separate decision from cloud deployment permissions |

GCP CLI user login and application-default credentials used by SDKs/Pulumi are distinct. Both must work where required. Prefer OA-managed credentials and documented renewal/recovery over a copy of Ryan's personal tokens. The untracked root `scripts/infra-preview` is not a portable bootstrap dependency.

## Site release

From the intended clean deployment checkout, with its credentials loaded privately:

```bash
site/deploy --help
site/deploy --dry-run -P
site/deploy --dry-run --dev
site/deploy --status
site/deploy --status --dev
```

These commands are safe only after the guarded helper is adopted; the published old CW helper does not reject unknown arguments. `--dry-run` checks local settings and does not prove cloud access. Check project, host, Pages branch, source branch and remote in its output before continuing. The personal `r2.rbw.sh` demo is not OA `r2.oa.dev`.

Run required tests/build on the dev node or CI, then deploy preview with `site/deploy --dev`. Verify sign-in, map, drill, a supported search, a diff, `/scans` and required admin diagnostics; compare the build identity with the release record. Confirm recipes are already in place. Deploy production with `site/deploy`, verify the same smoke and canonical production status, and watch CI after its source push. A failed Git push can leave a successful site deploy unpublished, so verify both outcomes.

## Job and infrastructure release

Choose an explicit candidate tag via `IMAGE=... job/build.sh` for GCS/CW/HF, or `job/r2-scan build TAG` for R2. Cloud Build runs remotely; local Docker is not required. Verify `dt-cloud image-sha -e SHA` inside the candidate image addressed by digest, plus the deployment owner's bounded functional smoke. Preserve the digest and build ID before retagging/pinning. A candidate scan is not automatically harmless: a primary profile can publish data, record runs and post messages. Use the documented scratch/maintenance smoke and review its exact effects.

For GCS/CW/HF promotion, move only the intended live tag to the tested digest. For R2, update the scheduler's digest through its reviewed GCP stack plan. A scheduler/secret/IAM change requires a separate Pulumi preview and reviewed apply in the correct stack; ordinary code deployment does not require replaying every infrastructure apply. Do not publish a scheduler that references an empty secret container. Verify the next run's actual image/source and outcomes, not only a successful image upload.

## Release record and rollback

Keep a small record per deployment: source SHA and dirty state; successful CI run; prod/dev Pages deployment IDs; image build ID and candidate/previous/promoted digests; live schema check and applied recipe names; Pulumi stack/plan; data generation/pointers if changed; authenticated smoke results; operator and time. Keep secret values out of the record.

Site rollback restores the previous verified production deployment/source; job rollback restores the previous verified image digest/tag or scheduler pin. Schema and data-generation rollback are separate compatibility decisions, not automatic consequences of restoring old code. Rehearse a site rollback and an image rollback, and verify the result through the corrected status/source-stamp checks. Never destroy/recreate D1, move a data `CURRENT` pointer, or replay a migration as a generic code-rollback step.

## Outstanding shared auth release

The root source adopted shared session logging with an actor recipe and a temporary package patch. Auth has since supplied verified dist `851a558198f0a90bdfc949fc1159c1fcb6010b58` (source `ded8fb7`) that removes the need for the patch, but it still needs frozen-install/build validation and coordinated source adoption. The actor recipe remains required before new session-log server code. Auth's nested `migrations/retained-login/0001_admission.sql` applies only where retained D1 login is enabled; root-only migration runners do not discover that track. Treat this as an explicit release item or freeze an already-verified baseline before leave, rather than asking Louisa to infer compatibility from package versions.

## Audit verification

The shared deploy/TOML/status regression suite passed 25 tests on `mgu`; prod and preview dry-run paths were also exercised under macOS system Bash. These verify tooling behavior, not Louisa's cloud access or a production release. No live access grant, state migration, recipe apply or deployment was performed. Owner handoffs were requested through Agent Mail.

Cloudflare's [project API] exposes the canonical production deployment; its [deployment-list API] supports an environment filter and pagination. The status tests cover newer previews, a canonical production record, unrelated/failed previews, named preview pagination and dirty provenance.

[project API]: https://developers.cloudflare.com/api/resources/pages/subresources/projects/methods/get/
[deployment-list API]: https://developers.cloudflare.com/api/resources/pages/subresources/projects/subresources/deployments/methods/list/
