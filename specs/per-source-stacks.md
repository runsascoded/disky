# Stacks per cloud account, turnkey

**Status:** design only, 2026-10-17. Research from the root checkout (`cloud`) and the deployment branches (`gcs`, `cw`, `hf`, `r2`, `m3`). Nothing here has been built, applied or deployed. §0 records Ryan's decisions (2026-10-17); §1–2 are the research behind them.

**Question (Ryan):** should each public demo bucket get its own small disky stack (e.g. `disky.ctbk.dev` for `ctbk`, `disky.crashes.hccs.dev` for `crashes`, `disky.jct.rbw.sh` for `jc-taxes`), with r2.rbw.sh either retired or turned into a "thin" multi-source aggregator over them? The design goal: standing up a stack should be turnkey, especially a one-bucket one, so per-project tailoring stays inside that project's stack, and scaling doesn't concentrate in an aggregator.

## 0. Decisions (Ryan, 2026-10-17)

- **One disky ≈ one cloud account.** A stack's scope is a billing account: the same boundary as its credentials, its bill and its owner. So a stack never holds keys into another account, and "which stack scans this bucket" has one answer. R2 to start:
  - **RAC** keeps r2.rbw.sh (`demo` branch): jc-taxes, plus Awair when it lands (RAC's GitHub org and Cloudflare account).
  - **HCCS** gets its own R2 disky, `r2.hccs.dev`: ctbk and crashes, in HCCS's Cloudflare account, its own IaC token and Pulumi state, its digest in HCCS's Slack.
  - **OA** has its deployments already (gcs/cw/hf/r2.oa.dev).
- **The aggregator is shelved** (§3): a spec, not work. It costs more than a single stack even when implemented well, and it draws org lines (one site reading many accounts) that don't match the ones that matter (billing, ownership, access).
- **Ruthless turnkey** (§4): every manual step removed is worth removing, even where some remain.

The per-bucket framing below (`disky.jct.rbw.sh` etc.) is the research's starting question; the decisions replace it with per-account stacks. jc-taxes stays in r2.rbw.sh; crashes and ctbk move to r2.hccs.dev, after which r2.rbw.sh drops them and the HCCS key leaves disky's repo secrets.

## 1. Turnkey inventory: what a new stack needs today

The existing runbook is `infra/README.md` "Starting a deployment" (6 steps: CF stack → `wrangler.toml` + store registry → D1 schema → secrets → build/deploy → first index). It's written for a generic deployment. The most recent real launches go well past it:
- hf: `hf:specs/hf-deployment.md` "Apply checklist", 11 steps;
- r2.oa.dev: `r2:specs/r2-deployment.md` "Ryan's steps", 8 steps, plus `r2:specs/done/r2-launch.md`.

No spec covers a turnkey or bootstrap command. [iac-templates.md] goes furthest. Its goal is "stand up a deployment … by writing config, not code", and phases 1–4 are done for Cloudflare: a shared `infra/cf/stack/` program plus `Pulumi.stack.example.yaml`. Its open items are the GCP stacks as one config-only program, and scripts that still carry literals.

### 1.1 Per item: how it is created today

"Public" is a no-auth demo stack like r2.rbw.sh (`AUTH_MODE = "public"`, `PUBLIC_READ = "1"`). "Org" is an authenticated stack like hf.oa.dev.

| item | public | org | how today | automated? |
|---|---|---|---|---|
| Cloudflare IaC token (Pages, D1, DNS on the zone, + R2 / API Tokens / KV per optional block) | yes | yes | dashboard, per account (permissions in `infra/cf/stack/Pulumi.stack.example.yaml`) | **manual**, once per account (RAC has one; whether HCCS has an IaC token is unverified) |
| Pulumi project + state backend + secrets provider | yes | yes | copy `infra/cf/stack/Pulumi.yaml.example` / `Pulumi.stack.example.yaml` to the branch; `pulumi stack init` (gcpkms on OA, passphrase + local `state/` on RAC/m3) | config by hand, then CLI |
| Pages project, custom domain + proxied CNAME, preview branch alias (`dev.<host>`) | yes | yes | `CfnDashboard` (`infra/cf/cfn_dashboard.py`) via `pulumi up` | **automated** (Pulumi). r2.rbw.sh itself predates it: `disk-tree-demo` / `-dev` Pages, DNS and D1 were hand-built and are in no stack |
| D1 database | yes | yes | `CfnDashboard` | automated (Pulumi) |
| D1 schema | yes (footer tables only; the rest stay empty) | yes | `wrangler d1 execute <db> --remote --file site/schema.sql` (+ `dt-cloud d1-schema check -d`) | CLI by hand; not in Pulumi |
| R2 index (serving) bucket | yes | yes | `CfnDashboard` `r2Bucket` | automated (Pulumi) |
| Publisher (job) R/W token on the index bucket | yes | yes | `CfnDashboard` mints it; outputs `r2_s3_access_key_id` / `_secret_access_key` | automated (Pulumi); needs "API Tokens" on the IaC token |
| Site read-only token on the index bucket | yes | yes | hf: `hf_stack.py` adds `hf-site-r2-reader`; the r2 demo's "disky demo RO" was made in the dashboard | **per-branch code** (hf) or manual (demo); not in the shared program |
| Job D1 token (`index-sync`) | yes | yes | hf: `hf_stack.py` `hf-job-d1`; the demo uses the repo's `CLOUDFLARE_API_TOKEN` | per-branch code or manual |
| Source-listing read-only token (the scanned bucket) | yes | yes | dashboard, in the **source's** account ("disky demo bkts RO" on RAC, `R2_HCCS_RO_*` on HCCS) | **manual** |
| `CACHE_KV` | optional | yes | `CfnDashboard` `kvName` | automated (Pulumi) |
| `site/wrangler.toml` (D1 id, KV id, bucket, `STORE`, `MARK`, `STATIC_GEN`, `PATH_STORE`, …) | yes | yes | copy `site/wrangler.example.toml`, paste `pulumi stack output` ids | by hand |
| Store registry entry `site/src/stores/<key>.ts` | yes | yes | a TS module per store (`site/src/stores.ts` globs `./stores/*`); build-time `STORE` picks it | **code** per stack |
| Mark (favicon / badge) `public/icons/<MARK>.*`, share card `public/og.jpg` | yes | yes | `scripts/marks/build.mjs`, `scripts/render-og-fallback.mjs` | by hand (generic fallbacks exist) |
| Pages secrets `STORE_ENDPOINT` / `STORE_ACCESS_KEY_ID` / `STORE_SECRET_ACCESS_KEY` | yes | yes | `wrangler pages secret put` ×3 (×2 with a preview env); hf: `infra/cf/hf-secrets -P -d`, r2: `infra/cf/sync-secrets` | per-branch scripts or by hand |
| `SESSION_SECRET` | **no** (nobody signs in) | yes | `wrangler pages secret put` | by hand / per-branch script |
| Google OAuth client (origins + `/auth/google/callback` for prod, dev, `pages.dev`, localhost) | **no** | yes | console "Create client" + Download JSON, then the auth repo's `scripts/provision-oauth-client.mjs --from-json … --pages-project … --run` | **manual console**: no public Google API creates a standard web OAuth client ([done/oidc-cutover-cw.md] P0: "Not IaC-able"). The script automates storing it |
| Email-code sign-in (`RESEND_API_KEY`, `MAIL_FROM`, the sending domain verified at Resend) | **no** | yes | secrets via `hf-secrets`; the domain was already verified for OA | secrets scripted; a new sending domain is manual (Resend + DNS) |
| Admin console, staff domain, personal agent tokens | **no** (public reads; mutations stay closed) | yes | site config (`_lib/auth.ts`, `@open-athena/auth`) | config |
| Job → site grant (`SITE_TOKEN`) for warm-cache / probe | **no** (public site: anonymous reads) | yes | mint at `/admin`, `gcloud secrets versions add` | manual |
| Scan job: GHA workflow (`daily-ingest.yml`) | yes (demo shape) | — | a hand-copied workflow: `BUCKET`, the D1 id and source buckets are literals | by hand |
| Scan job: Nextflow on Batch (`job/nf/scan.nf`, `dt-cloud scan head`, `job/profiles/<stack>.yml`) | no (overkill for small buckets) | yes | profile on the branch; GCP stack (`infra/gcp/__main__.py`, per branch, **not** config-only) for SA, secrets, scheduler; image build `job/build.sh` | Pulumi + manual secret versions + image build |
| Nextflow **local** executor in GHA | yes (target) | — | exists (`executor: {kind: local}`, tested by `test_scan_nf.py`); the demo's adoption is step 5 of [pipelines-everywhere.md] §6, not done; R2-backed resume cache unprobed | not adopted |
| Cron | yes | yes | GHA `schedule:` (runs late), or `infra/cf/cron-dispatch` (one Worker, a `DISPATCH` JSON map, `wrangler deploy` + `GITHUB_TOKEN` secret); GCP Cloud Scheduler for Batch | config edit + wrangler |
| GHA secrets (CF token, account id, RW pair, source RO pair, endpoint) | yes (~7) | — | `gh secret set` | by hand (scriptable) |
| First index: static-names `build`, interval-store `build` | yes | yes | the demo's workflow does `build` when absent, `add` otherwise | automated by the workflow |
| `PATH_STORE = "intervals"` after `interval-store verify` | yes | yes | flip in `wrangler.toml`, redeploy | by hand |
| R2 access collector (read recency) | optional | optional | `infra/cf/r2-access` (Pulumi stack per account + `wrangler secret put CF_OBSERVABILITY_TOKEN` + a second apply to enable the cron); the source bucket's **Data Access Logs** enabled in the dashboard | Pulumi + 2 manual (Observability token, dashboard toggle) |
| Slack digest + alerts (`#<d>-usage`, `#<d>-admin`) | **yes** when the owner has a Slack (HCCS; Ryan 2026-10-17) | yes | Slack app from a manifest (api.slack.com, by hand), channels, bot token into Secret Manager, icons Pages project (Pulumi) + avatars deploy | manual |
| Run records (`scan-run supervise`, `/scans`, `DT_TRIGGER`) | optional | yes | needs D1 + the job's D1 token | config |
| Meta store ("data behind this site") | **no** for a one-bucket demo | yes | a second profile + `STORES_JSON` | config |

### 1.2 What a public stack legitimately skips from the parity baseline

`CLAUDE.md` "Deployments: parity by default" is the baseline. A public, no-auth, one-bucket demo can skip whatever exists only for signed-in users or an operator audience, and keep everything that serves readers:
- **Skip:**
  - auth (Google OIDC, email codes, staff domain, admin console, agent tokens);
  - the job's site grant (warm-cache and probe read anonymously);
  - (not skipped: the Slack digest, when the owner has a Slack; see §5)
  - the meta store (a one-bucket stack's own index bucket is tiny; the aggregator can show it if wanted);
  - staging / sweeps (read-only by definition).
- **Keep:**
  - the dev stack (a `branchAliases` entry costs nothing);
  - the interval store as the Map's path store;
  - search from the static name index only (`FILTER_STATIC`, `FILTER_STATIC_HEAVY`, `FILTER_INDEXED_ONLY`);
  - `index-gc`;
  - `warm-cache` + `probe -c` after each scan;
  - `DT_TRIGGER` and run records, if the D1 token is there anyway.

A demo stack's spec lists the kept items it lacks as gaps, as the baseline says.

### 1.3 Domains and accounts

A Pages custom domain needs a zone in the **same Cloudflare account** as the Pages project, for `CfnDashboard`'s `PagesDomain` + proxied CNAME.
- Deep names are fine for Pages custom domains. `dev.r2.rbw.sh` and `dev.hf.oa.dev` are live.
- The m3 caveat ("Universal SSL covers `*.rbw.sh`, not `*.disk.rbw.sh`") applies to its Tunnel hostname, not to Pages.
- So `disky.crashes.hccs.dev` and `disky.jct.rbw.sh` are OK as names. Each stack then lives in its zone's account:
  - `ctbk` and `crashes` presumably in HCCS's account, which also owns the buckets (zone placement unverified);
  - `jc-taxes` in RAC (`rbw.sh` is RAC's zone).

That's the point of the split: each stack's source token, index bucket and D1 are same-account, and no cross-account key exists.

### 1.4 Manual-step count: a public one-bucket stack today

Take `jc-taxes` → `disky.jct.rbw.sh`. It's the easy case: RAC already has an IaC token and the collector.

1. Branch (or dir) + `site/src/stores/jct.ts` + mark/OG (code).
2. `Pulumi.yaml` + `Pulumi.jct.yaml` (+ passphrase/state choice).
3. `pulumi up` (Pages, domain, CNAME, dev alias, D1, R2 index bucket, RW token).
4. Site RO token on the index bucket (dashboard, **console**).
5. Source RO token on `jc-taxes` (dashboard, **console**; or reuse "disky demo bkts RO", which re-couples the stack to the demo).
6. `site/wrangler.toml` from the example, ids pasted.
7. `wrangler d1 execute … --file schema.sql`.
8. Pages secrets ×3 (×2 envs).
9. A copy of `daily-ingest.yml` + `deploy-r2.yml` with the literals changed.
10. GHA secrets ×~7.
11. A cron-dispatch `DISPATCH` entry + `wrangler deploy` (or accept GHA's late `schedule:`).
12. First deploy.
13. First scan dispatch (builds static-names + interval store).
14. `interval-store verify` / probe, then confirm `PATH_STORE`.
15. Optional collector: already collecting `jc-taxes` on RAC; for an HCCS source, a new `r2-access` stack, an Observability token (**console**), Data Access Logs on (**console**), install the secret, a second apply.

**≈14 steps** without the collector, ~5 more with it on a new account. **3–5 are console-only.** A new account (HCCS) adds a sixth console step: its IaC token. An org stack adds GCP Pulumi, the image build, OAuth (**console**), Resend, the Slack app (**console**) and the site grant (**console**). The hf checklist is 11 numbered steps with ~35 sub-steps.

**Biggest blockers to turnkey:**
- the store registry is TS code plus build-time `VITE_*` vars, so a stack needs a branch and a build of its own;
- the read-only and D1 tokens aren't in the shared CF program (hf added them in per-branch `hf_stack.py`);
- secret plumbing is per-branch scripts (`hf-secrets`, `sync-secrets`);
- the D1 schema is outside Pulumi;
- the scan workflow is a hand-copied file with literals;
- source tokens in a different account are console-made.

## 2. Aggregator status

### 2.1 What exists

- **Union at ingest** (live): r2.rbw.sh lists `ctbk`, `crashes` and `jc-taxes` with two accounts' tokens into one `path-index`, one D1 and one index bucket (`.github/workflows/daily-ingest.yml`). [federated-scans.md] calls it "the migration bridge, not the target model".
- **Union views at read time, within one deployment** (done, slices 1–7 of [federated-scans.md] "Case 1"; live on r2.oa.dev as `primary` + `meta`). `STORES_JSON` `{"union": [...], "main": true}` (`site/functions/_lib/stores.ts`, `_lib/union.ts`).
  - **Works over a union:**
    - subtree + table (shared byte threshold `T=`);
    - series (carry-forward steps, `missing`);
    - diff (changed / unchanged / unknown per member);
    - age pyramid;
    - `?f=` (each member's own static index; a refusal makes the answer `partial`);
    - the `FederatedScanPicker` with `at=` cutoffs;
    - member marks.
  - **Refused or primary-only at the union root:**
    - owners / `lens` / `o` / `by` / `cl`;
    - `q=` on series and diff;
    - OG, `largest`, `prefixes`, `name-summary`, `/data/*` scan list;
    - the interval store, warm-cache, probe.
- **Catalog / resolver model** (done, not wired to live data): `site/src/federatedScans.ts`, `federatedViews.ts`, `federatedPickerModel.ts`, dev fixture `/dev/federated-scans` (`src/dev/FederatedScansPreview.tsx`). The producer contract is [federated-source-catalog.md]: a JSON shape and parser only. No producer publishes one. The Awair handoff (`~/c/awair/specs/disky-scan-export.md`, untracked) isn't implemented ([awair-public-r2-demo.md]).
- **Stacks phase 0** ([stacks-and-sources.md] §8, branch `stacks-p0`): one gcs scan rebuilt as six per-bucket stores plus the union. Per-bucket wall time is 2,245 s → 1,124 s, total `path-index` work is 24% lower, bytes are equal, and the per-bucket sums equal the union exactly. Serving latency is **not measured**: the browse key can't read the scratch bucket.
- **Not built:**
  - the **remote member kind** (another stack's site as a union member, [stacks-and-sources.md] §6.3, [federated-scans.md] "cw + HF");
  - producer-published catalogs;
  - per-source pipeline mode (`sources_as_stores`, §6.1 there).

### 2.2 Can a site read another deployment's artifacts today?

Partly, and not thinly.
- **Path store: yes, cross-account.** A secondary store in `STORES_JSON` sets its own `STORE_BUCKET` / `STORE_ENDPOINT` / keys (`secrets` maps them to Pages secret names). So an RAC site can range-read an HCCS index bucket with an HCCS-minted read-only key.
- **D1 footers: no.** Footers are read from the reading deployment's own D1 (`index_schema.store`). An aggregator would have to run `index-sync -s <member>` into its own D1 for every member scan. That's a job per member scan, and the aggregator then holds the members' publish timing: not thin.
- **Static name index: no.** It's read only through the deployment's single `INDEX_R2` binding (`staticFilter.ts`: `r2Blobs(env.INDEX_R2, …)`), so it's same account, same bucket. Making it remote needs an S3-backed blob source per store; the `r2Blobs` seam makes that small, but it isn't written.
- **Interval store: no.** `intervalsOn` needs `INTERVAL_STORE_GEN` and `INDEX_R2`, neither per store. Secondary stores read the per-scan path store. [lean-scans.md] plans to delete those per-scan sorts, so artifact-level aggregation would **conflict with lean scans**.
- **Public reads.** The demo's source buckets have public domains (`data.ctbk.dev`, `crashes-data.hccs.dev`; `objectBases` in `site/src/stores/r2.ts`). The index bucket `disk-tree-demo` is private: the site reads it with "disky demo RO".

So the thin path is **API-level fan-out**: the aggregator calls each member site's `/api/subtree`, `/api/series`, `/api/diff`, `/api/age-pyramid`, `/api/union-scans`, `/api/filter-*`.
- Public members need no grant; private ones take a grant token, as [federated-scans.md] specifies.
- Today a member request is in-process: `memberRequest` + `withStore` call the same handlers. A remote member replaces that call with a `fetch` to the member's origin, with the same query params, the scan id the union resolved for it, and `T=`.
- The merge functions (`unionRoot`, `unionDiffRows`, `carryForward`, `unionFiltered`) are transport-agnostic.

### 2.3 What an aggregator needs, and what works less well than one stack

Needed:
- a remote member kind in `stores.ts` / `union.ts`: `{"remote": "https://disky.ctbk.dev", "grant"?: <secret name>}`;
- `/api/union-scans` and the picker from each member's own scan list (the primary's `scans.json` today);
- response-version handshake: a member's `RESPONSE_V` or schema can differ from the aggregator's after one side deploys;
- a fetch timeout that marks a member `missing` / unavailable, not a 500;
- edge caching of member responses keyed by member + resolved scan;
- union marks/icons per member.

**Works as well:**
- treemap + table root (one shared threshold);
- a one-member drill, delegated to the member: only one hop more, and the member's own caches are warm;
- series and diff per member, carried forward;
- age;
- `?f=` merged across members' static indexes;
- read recency (each member's own collector and lens).

**Worse than one stack:**
- **Cold root latency:** the slowest member plus one hop; one stack reads one D1 and one bucket. Measure it with `probe -c` before committing ([stacks-and-sources.md] §5).
- **Union-root refusals persist:** owners/lenses (moot for the demo), `largest` / `prefixes` / `name-summary`, OG of the union root, and `q=` on root series/diff.
- **No cross-member "moved" diff,** and no single fleet scan instant: members are scanned on their own schedules (carry-forward makes that honest, not identical).
- **N× fixed costs:** N Pages projects, N D1, N cron entries, N workflow runs, N dashboards to keep on the baseline. Each is small; their upkeep is the real cost, which is why §4 matters.
- **Version skew:** each member deploys separately, and a breaking API change needs coordinated deploys or versioned endpoints.
- **Search ranking across members** is a concat (heaviest first), not one global index.

## 3. The aggregator (shelved)

Kept as a design note, not planned. What it would take, and where it costs more than one stack:

- **Not inherent, just not built:** owners, `largest`/`prefixes`, `name-summary`, the OG card and `q=` on series/diff at a union root. Sums and counts merge exactly.
- **Thresholds compose with one extra round trip.** The Map's cutoff today is computed from the viewport and the total bytes to render; an aggregator first asks each member for its total under the current query (cheap, cacheable), computes the global cutoff, then sends it to every member, which short-circuits below it exactly as a single stack does. Top-k is exact by over-fetch: the global top k is within the union of each member's top k. Page P of a largest-files table needs the top (P+1)·k from each member, so cost grows with page depth where a single stack does one index lookup. The zero-overhead answer is to materialize: a cron that merges members' indexes into one, which is a single stack again.
- **API drift** between members deploying separately: each response carries an API version; the aggregator requires a minimum per feature and degrades per member (that member shown stale or omitted for that view) instead of failing the page.
- **Latency:** a cold root is the slowest member plus a hop.
- Reading another stack's artifacts directly works only for the path store (cross-account keys, which §0 rules out); footers, the static name index and the interval store are read through the reader's own D1 / `INDEX_R2`. So an aggregator would be API fan-out (a "remote member" kind; the merge code is transport-agnostic).

Worth an experiment some day for the perf numbers and the design problems it surfaces; not before the turnkey work.

## 4. Work that makes a one-bucket stack turnkey

Target: `dt-cloud stack new hccs-r2 --source r2://ctbk --source r2://crashes --account hccs --domain r2.hccs.dev --public`, then `dt-cloud stack up hccs-r2`. What's left manual: the source's read-only token (and the IaC token on a first-time account), plus the Data Access Logs toggle if the collector is wanted.

1. **Store descriptor as data.** A JSON store descriptor (`stacks/<name>/store.json`, or a `STORE_JSON` var) covers `Store`'s plain fields: `label`, `title`, `desc`, `buckets`, `scheme`, `objectBases`, `rootLabel`, `objectsNote`. The SPA reads it at runtime (`/api/config`, or a static `/store.json`). The TS modules stay for function-valued fields (`externalLink`, `about`, `allowances`). `AUTH_MODE` / `MARK` / `PROD_HOST` / `DEV_HOST` move to runtime too, with a generic mark. Then one `dist` built on `cloud` (a GHA artifact per `cloud` SHA) serves every public stack, and **no branch or build per stack** is needed. It's also the precondition for "someone who isn't us" ([iac-templates.md] goal).
2. **Tokens in the shared CF program.** Upstream `hf_stack.py`'s site reader and job D1 tokens into `infra/cf/stack/` (`siteReaderToken`, `jobD1Token`), plus an optional `sourceReaderToken: {buckets: [...]}` when the source is in the stack's own account. All are exported as secrets. A source in another account stays a console step: the point of per-source stacks is to make that case rare.
3. **One secrets sync.** Generalize `hf:infra/cf/hf-secrets` / `r2:infra/cf/sync-secrets` into `dt-cloud stack secrets <name>`. It pipes the stack outputs into Pages secrets (prod + preview) and GHA secrets (`gh secret set`, names prefixed per stack) without printing.
4. **Schema in the apply.** Run `site/schema.sql` on a new D1 from `stack up` (`wrangler d1 execute`, then `d1-schema check`), or as a Pulumi `command.local.Command` keyed on the database id. It's idempotent because it runs only on create.
5. **One reusable scan workflow.** A `workflow_call` workflow (or one matrix workflow over `stacks/*/stack.yml`) that runs the profile's pipeline on the **local Nextflow executor** in GHA ([pipelines-everywhere.md] §6 step 5). That gives the demo the same step registry as gcs/cw/hf (list → index → static-names → interval-store → index-sync → index-gc → warm-cache → probe), not a 186-line bespoke file. The cron is one `DISPATCH` entry per stack in `infra/cf/cron-dispatch` (generated).
6. **`dt-cloud stack new` / `stack up`.** `new` generates the config only (`stacks/<name>/`: `stack.yml`, `Pulumi.<name>.yaml`, the `wrangler.toml` vars, the descriptor, the profile, the dispatch entry). `up` runs the sequence, idempotently, each step skipped when done:
   1. `pulumi preview`, shown;
   2. `up` on a confirm;
   3. schema;
   4. secrets;
   5. deploy;
   6. first scan dispatch;
   7. `probe -c`.

   `up` stops at the first missing manual input (the source token) and names it. Pulumi `up`, secret writes and deploys stay human-confirmed per run, as today.

Org stacks (gcs/cw/hf/r2) benefit from 2–4 and 6 too. They keep their branches for auth, Slack, the GCP program and owner policy. The GCP program as one config-only program is still [iac-templates.md]'s open item.

## 5. Answers and remaining choices

- **Cloudflare tokens by IaC:** yes. The Cloudflare provider creates API tokens (`cloudflare.ApiToken`, account-owned `AccountToken`), and an R2 S3 key pair is derived from a token (access key id = the token id, secret = SHA-256 of the token value), so the site's reader token, the job's D1 token and a same-account source reader can all be Pulumi resources (hf's `hf_stack.py` already makes two). What stays manual is one bootstrap token per account, with permission to create tokens; the access-log toggle may stay a console click (no API found yet; verify).
- **Google OAuth clients:** Google has no public API to create OAuth client IDs or edit their redirect URIs for ordinary web apps (the IAP OAuth admin API covered only IAP clients and is deprecated). That's deliberate (consent-screen review, abuse), and hostile to automation. Mitigations: public stacks need none; email-code sign-in needs none; an org's stacks can share one client (one redirect URI to add per stack, by hand), or a central auth host per org with one client that the stacks delegate to (removes the per-stack step entirely; a separate spec).
- **Hugging Face:** no Pulumi provider, and as far as I know no API to mint fine-grained tokens; HF stacks are rare, so the token stays a documented console step.
- **Workers + Assets instead of Pages:** yes, worth it (Cloudflare steers new projects to Workers; one deploy model for the site, the collector and cron-dispatch; Pulumi-managed Worker scripts and routes). It's a port of every `functions/` route to one Worker router: its own spec, for every deployment, after 10/19. r2.rbw.sh's hand-built Pages project, D1 and DNS get imported into Pulumi either way (`pulumi import`), or are recreated by the turnkey path when it moves to W+A.
- **Site config at boot vs deploy:** with W+A the Worker can inject the stack's descriptor into `index.html` as it serves it: no extra request, no build per stack, and the same hook stamps per-stack meta/OG tags. So: one prebuilt site, config injected at serve time. (On Pages, the equivalent is `stack up` rewriting `index.html` at deploy.)
- **Where stack config lives:** with one stack per account, each stack's config lives with its owner. RAC's (r2.rbw.sh) on `demo`; HCCS's in an HCCS repo (or a `hccs` branch here until it has one), consuming disky's reusable workflow and released site by SHA. Account ids and other identifiers come from Pulumi config/outputs, not tracked files (this repo is public).
- **Names:** `r2.hccs.dev` for HCCS's R2 disky; r2.rbw.sh stays RAC's. Verify `hccs.dev` is in the HCCS Cloudflare account.
- **Awair:** joins r2.rbw.sh (RAC), through the same descriptor + source mechanism.
- **Slack digest:** public stacks still post their digest; HCCS's goes to HCCS's Slack (its own bot token: one Slack-side install per workspace, then a secret).
- **Scan ids:** stacks use timestamped ids (`YYYY-MM-DDTHHMM`, as hf), not date ids.

[stacks-and-sources.md]: stacks-and-sources.md
[federated-scans.md]: federated-scans.md
[federated-source-catalog.md]: federated-source-catalog.md
[iac-templates.md]: iac-templates.md
[pipelines-everywhere.md]: pipelines-everywhere.md
[lean-scans.md]: lean-scans.md
[awair-public-r2-demo.md]: awair-public-r2-demo.md
[done/oidc-cutover-cw.md]: done/oidc-cutover-cw.md
