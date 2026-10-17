# Per-source stacks: a small disky stack per public bucket, and r2.rbw.sh as a thin aggregator

**Status:** design only, 2026-10-17. Research from the root checkout (`cloud`) and the deployment branches (`gcs`, `cw`, `hf`, `r2`, `m3`). Nothing here has been built, applied or deployed. Ryan decides §5's open questions.

**Question (Ryan):** should each public demo bucket get its own small disky stack (e.g. `disky.ctbk.dev` for `ctbk`, `disky.crashes.hccs.dev` for `crashes`, `disky.jct.rbw.sh` for `jc-taxes`), with r2.rbw.sh either retired or turned into a "thin" multi-source aggregator over them? The design goal: standing up a stack should be turnkey, especially a one-bucket one, so per-project tailoring stays inside that project's stack, and scaling doesn't concentrate in an aggregator.

**Short answer:**
- **Both, in order:** per-source stacks first, then the aggregator. A per-source stack is the unit [stacks-and-sources.md] and [federated-scans.md] already point at. Its build-side case is measured (gcs phase 0: wall time halved, compute −24%, same bytes). For the demo the stronger case is ownership: the HCCS buckets get scanned by an HCCS-account stack, and no HCCS key is held in disky's repo secrets.
- **Keep r2.rbw.sh, as an aggregator.** Don't retire it. It becomes thin only once the union reader has a **remote member** kind (API fan-out to member sites), which isn't built. Until then it stays the central scanner. It gets retired as a scanner, not as a site, after the members have published for a week.
- **Not turnkey today.** A public one-bucket stack is ~15 manual steps, about 5 of them console-only, and needs a code file and a branch (§1.4). Six pieces of work (§4) would make it `dt-cloud stack new` + `stack up` plus ~2 console steps, both Cloudflare-side.

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
| Slack digest + alerts (`#<d>-usage`, `#<d>-admin`) | **no** (no audience) | yes | Slack app from a manifest (api.slack.com, by hand), channels, bot token into Secret Manager, icons Pages project (Pulumi) + avatars deploy | manual |
| Run records (`scan-run supervise`, `/scans`, `DT_TRIGGER`) | optional | yes | needs D1 + the job's D1 token | config |
| Meta store ("data behind this site") | **no** for a one-bucket demo | yes | a second profile + `STORES_JSON` | config |

### 1.2 What a public stack legitimately skips from the parity baseline

`CLAUDE.md` "Deployments: parity by default" is the baseline. A public, no-auth, one-bucket demo can skip whatever exists only for signed-in users or an operator audience, and keep everything that serves readers:
- **Skip:**
  - auth (Google OIDC, email codes, staff domain, admin console, agent tokens);
  - the job's site grant (warm-cache and probe read anonymously);
  - the Slack digest thread and alert channels (GHA's own failure email is the alert);
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

## 3. Recommendation

**Both: per-source stacks for the work, and r2.rbw.sh as a thin aggregator over them.** Retiring r2.rbw.sh loses the cross-project view, which is the demo's pitch ("union of roots"). Aggregator-only, the status quo, keeps every project's tailoring and every account's keys in one stack, the opposite of the goal.

Order:
1. **Make a one-bucket public stack turnkey** (§4), and stand up **jc-taxes first**. It's same-account on RAC, its collector already runs, and `jct.rbw.sh` is its project. Then crashes and ctbk in HCCS's account, which needs HCCS's IaC token: one console step, once.
2. **Remote member kind** in the union reader, measured with `probe -c` against the current single store on the same three buckets. The legacy union history stays a separate legacy source, never summed with the new members ([federated-scans.md] slice 4).
3. **Cut r2.rbw.sh over** to `{"union": [jct, crashes, ctbk(, awair)], "main": true}` over remote members. Retire `daily-ingest.yml`'s central listing, and the HCCS key in disky's repo secrets, after a clean week. Keep the old index read-only for history.

For the demo's sizes (~1M objects; GHA lists all three in minutes), the scaling argument is weak and the ownership argument carries it. For gcs, phase 0 says the build side favors per-source regardless.

## 4. Work that makes a one-bucket stack turnkey

Target: `dt-cloud stack new jct --source r2://jc-taxes --account rac --domain disky.jct.rbw.sh --public`, then `dt-cloud stack up jct`. What's left manual: the source's read-only token (and the IaC token on a first-time account), plus the Data Access Logs toggle if the collector is wanted.

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

**Where stack config lives (proposed):** demo stacks' config goes on `cloud`, as `stacks/<name>/`, beside r2.rbw.sh's own `wrangler.r2.toml` (the precedent: `cloud` deploys the demo). It holds no code, so "each branch carries exactly its deployments' code" holds. Later, the same generator can run in the owning project's repo (`ctbk`'s `disky/` dir, consuming disky's reusable workflow, released `dist` and `infra` package by SHA). That's [federated-scans.md] phase 4's ownership boundary: "the aggregate deployment does not orchestrate source scans or hold source listing/write credentials".

Org stacks (gcs/cw/hf/r2) benefit from 2–4 and 6 too. They keep their branches for auth, Slack, the GCP program and owner policy. The GCP program as one config-only program is still [iac-templates.md]'s open item.

## 5. Open

- **Runtime vs build-time site config (§4.1):** does the SPA fetch its descriptor at boot (one more request on first paint, cacheable), or does `stack up` inject it into a prebuilt `index.html` at deploy (no request, one tiny per-stack asset rewrite)? The latter keeps first paint unchanged.
- **Stack config home:** `cloud`'s `stacks/<name>/`, or each project's repo from day one?
- **Accounts:** does HCCS get its own Cloudflare IaC token and Pulumi state (passphrase + local `state/` as RAC does), and are `ctbk.dev` / `hccs.dev` zones in the HCCS account (unverified)?
- **Names:** `disky.ctbk.dev`, `disky.crashes.hccs.dev`, `disky.jct.rbw.sh` as proposed, or `<project>.disky.dev`-style under one zone? One zone would put every stack in one account, which undercuts the ownership split.
- **Awair:** does it join as a member through the same per-source stack template, or through its own producer export ([awair-public-r2-demo.md])? The template makes the former cheap.
- **Scan ids:** member stacks should use timestamped ids (`YYYY-MM-DDTHHMM`, as hf), not date ids. The demo's date-id same-day re-run hazard is noted in `daily-ingest.yml`.

[stacks-and-sources.md]: stacks-and-sources.md
[federated-scans.md]: federated-scans.md
[federated-source-catalog.md]: federated-source-catalog.md
[iac-templates.md]: iac-templates.md
[pipelines-everywhere.md]: pipelines-everywhere.md
[lean-scans.md]: lean-scans.md
[awair-public-r2-demo.md]: awair-public-r2-demo.md
[done/oidc-cutover-cw.md]: done/oidc-cutover-cw.md
