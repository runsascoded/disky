# The r2.rbw.sh demo on its own branch (`demo`), scanned by the Nextflow pipeline's local executor

Status: 2026-10-17, design. Ryan's go on both parts (the branch split, the NF-local scan). `cloud` stays the default branch, with placeholder stubs for the demo's workflows.

## Why

`cloud` is the base every deployment merges, and it is also the r2.rbw.sh demo's deployment branch ([branch-layout.md]: "the public r2.rbw.sh demo, deployed from `cloud` itself"). So `cloud` carries demo-only files (stores, wrangler configs, the ingest workflow, the RAC collector stack's config and state), and the demo is the one deployment that doesn't run the shared scan pipeline: it is ~200 lines of GHA bash (`daily-ingest.yml`) where gcs/cw/hf/r2 run `job/nf/scan.nf`. Every other deployment is a branch carrying exactly its own code; the demo should be one too, and should exercise the same pipeline the others do.

The branch is `demo`: `r2` is OA's r2.oa.dev branch.

## Part 1: the branch split

### What moves to `demo`

| file | what it is |
|---|---|
| `site/src/stores/r2.ts` | the demo's stores (ctbk, crashes, jc-taxes) |
| `site/wrangler.r2.toml`, `site/wrangler.r2-dev.toml` | Pages config, prod + dev |
| `site/index.html` (the demo's title/description/OG lines) | `cloud` keeps a generic page; the demo overrides it (an intrinsic, small conflict point) |
| `.github/workflows/daily-ingest.yml` | the ingest (replaced by the NF-local job in part 2) |
| `.github/workflows/deploy-r2.yml` | site deploy after CI |
| `infra/cf/cron-dispatch/wrangler.r2.toml` | the demo's clock (07:15 UTC `workflow_dispatch`) |
| `infra/cf/r2-access/Pulumi.rac.yaml` + its `state/` | the RAC access collector stack (monitors jc-taxes, publishes to the demo bucket) |
| `site/bench/baselines/r2-*.json` | the demo's render baselines |

Generic code that mentions the demo only as an example or a test fixture (`union.test.ts`, `federatedScans.test.ts`, `test_r2_access.py`, the collector's source and README, `cron-dispatch`'s source) stays on `cloud`. Docs (README, AGENTS.md, `branch-layout.md`) say the demo lives on `demo`, and `branch-layout.md`'s placement rule ("`cloud` only if the r2 demo uses it or ≥ 2 of {oa, local} do") becomes "the demo is a deployment like the others: `cloud` when ≥ 2 deployments use it".

### GitHub mechanics (the part that can break the daily)

`runsascoded/disky`'s default branch is `cloud`, and stays `cloud` (Ryan 2026-10-17): it is the front door to the whole ecosystem on GitHub. GitHub reads `schedule` and `workflow_run` triggers only from the default branch's workflow files, and a `workflow_dispatch` needs the workflow registered there. So:

- **Placeholder stubs on `cloud`** (`~/.rc/git/github/github-placeholder-main.py`'s pattern): `cloud` keeps a `daily-ingest.yml` with the same `name:`, `on: workflow_dispatch` only, and one step that says where the real one lives, with the full URL of the workflow on `demo` (`https://github.com/runsascoded/disky/blob/demo/.github/workflows/daily-ingest.yml`). The stub registers the workflow; `cron-dispatch` dispatches it with `ref: demo`, which runs `demo`'s file. The stub doubles as documentation on `cloud`.
- **The deploy can't stay a `workflow_run` on CI** (that trigger reads only `cloud`'s files). On `demo`, `deploy-demo.yml` runs `on: push` (`[demo, dev]`), calls CI as a reusable workflow (`ci.yml` gains `on: workflow_call`), and deploys `needs:` it, so a red build never deploys. CI's own `push` trigger then also fires on `demo`; its already-passed-inputs skip (55a1a1f4) makes the second run cheap, or `ci.yml` ignores branches that carry a deploy wrapper. Decide when implementing.
- GitHub's own `schedule:` (today the ~4 h-late duplicate of the 07:15 dispatch, which acts as a catch-up) is lost with the move: `schedule` reads only the default branch. A second `cron-dispatch` entry later in the day replaces it if the catch-up is wanted.

`dev` (dev.r2.rbw.sh's Pages branch) becomes the demo's dev branch: fast-forwarded to `demo`, not to `cloud`.

### Docs per branch

Every branch's README says what that branch is and links the related branches; `cloud`'s is the ecosystem's front door:
- `cloud`: what disky is, the engine/overlay/site layout, how to stand up a deployment, and the live deployments with their branches: r2.rbw.sh (`demo`), OA's gcs/cw/hf/r2.oa.dev (`Open-Athena/disky`), disk.rbw.sh (`m3`), the macOS app (`app`, `local`).
- each deployment branch: its host(s), what it scans, its stack/infra, what it changes from `cloud`, and a link back to `cloud`.
- The placeholder stubs on `cloud` link to the real workflows on their branches.

### Keeping the demo current

Today a push to `cloud` deploys the demo; after the split, `cloud` must be merged into `demo`, like every deployment. To keep the demo a live canary for `cloud`, add a workflow on `demo` (`push` on `cloud` doesn't trigger it, so it runs on the cron dispatch and on `workflow_dispatch`, registered by a placeholder stub on `cloud` like the ingest's): merge `cloud` into `demo` when it merges cleanly and push (which then deploys after CI); on a conflict, fail with the conflicting paths so a session resolves it. The root session's push routine becomes: push `cloud`, then merge it into `demo`, push `demo` and fast-forward `dev`.

### Credentials

The collector stack's only tie to `wt/m3` was its Pulumi passphrase: the RAC Cloudflare account and API token are the same in the root `.envrc` and `wt/m3`'s. The passphrase is now in the root `.envrc` too (2026-10-17), so `pulumi -C infra/cf/r2-access` runs from root; after the split it runs from `wt/demo`, whose `.envrc` carries the RAC token, the passphrase and the demo's R2 keys. Open: the root and m3 share one RAC `CLOUDFLARE_API_TOKEN`; per the one-token-per-consumer rule, mint a demo-only token (Pages + D1 + Workers + R2 on the demo's resources) when `wt/demo` is created.

### Steps

1. Create `demo` at `cloud`'s tip. On `cloud`, one commit that removes the files above, adds the placeholder stub(s) and makes `index.html` generic; merge it into `demo` with `git merge -s ours` (records the merge without its changes, so later `cloud` merges don't delete the demo's files).
2. On `demo`: `deploy-demo.yml` (push → CI via `workflow_call` → deploy) in place of `deploy-r2.yml`; `ci.yml` gains `workflow_call` on `cloud` first. Change `cron-dispatch`'s `DISPATCH` `ref` to `demo` and redeploy that Worker (Ryan's go: it's a prod deploy).
3. `wt/demo` worktree + `.envrc`; move the collector stack's `state/` there. READMEs per "Docs per branch".
4. Verify right away: a `workflow_dispatch` of the ingest with `ref: demo` (it no-ops past the listing on an already-scanned date), and a push of `demo` deploying to dev.r2.rbw.sh, then prod.

Sequencing: the only thing that can break is the daily's trigger, and step 4 tests it by hand the same day, so the split doesn't need a particular time of day; doing it right after the 07:15 run just leaves the most slack before the next one. Before or after 10/19 either way.

## Part 2: the demo's scan on the Nextflow pipeline's local executor

### What it is

The same `job/nf/scan.nf` DAG every deployment runs, with `executor: {kind: local}`: Nextflow runs each process as a local child process on the one machine it's on, concurrently as far as `cpus` allow, with the DAG's ordering, retries, `-resume` cache and per-step `scan-run` phases. The machine is whatever the cron starts: for the demo, a GitHub-hosted runner (ephemeral, 4 vCPU / 16 GB for public repos, free).

So yes: architecturally it is the pre-Nextflow one-node design, minus the hand-rolled bash. What it buys over today's `daily-ingest.yml`:
- one pipeline definition for every deployment: steps added to `scan.nf` (warm-cache, probe, index-gc, digest, over-time) reach the demo by config, which is the parity rule;
- the three bucket listings run concurrently as `list` tasks, and independent serve steps (static names, interval store) overlap;
- `-resume`: a failed run restarts from the failed step instead of from the listing;
- the scan records (`/scans`, phases, `DT_TRIGGER`) the other deployments get.

### When a deployment should use it

The cost argument against one big node was idle reserved capacity: a highmem VM sized for the `index` step sits mostly idle through I/O-bound listing and single-threaded tails, for tens of minutes, at gcs/cw scale. That argument is about paying for the node. It gives the rule:

- **Local executor:** the whole scan fits one node in minutes, or the node is free or already running. The demo (3 buckets, minutes end to end today, free GHA runner) and small single-bucket stacks qualify; so does a laptop (the m3 deployment's agent).
- **Cloud executor (Batch):** any step needs a bigger machine than the rest (gcs `index` on highmem), or there are many sources to fan out, or the run is long enough that per-step sizing and spot pricing matter.

Batch has a fixed overhead per task (~1.5–2 min VM boot; [scan.nf] line 13), so a tiny pipeline on Batch is slower and not cheaper. Local is the right default for small deployments, not a degraded mode.

### What the demo needs from the pipeline

| need | today (bash) | on `scan.nf` |
|---|---|---|
| list 3 R2 buckets in 2 accounts | `bulk-list r2://…` per bucket, cross-account keys | `sources:` with per-source credentials. `sources[].secrets` are GCP Secret Manager names; for the demo, the workflow writes the keys as AWS profiles and `buckets.yml` names them, and the profile declares no `secrets` (`secrets exec` with no `-s` is a plain exec with the ambient env). To verify: `list` honors `buckets.yml` profiles |
| read recency | `access r2-agg` over the collector's public hour files | the `access-ingest` step (`options.access-ingest.args`); verify it covers `r2-agg` |
| union → path store | `path-index -d <date>` | `index`; verify it runs `path-index` (the union) for an `r2://` multi-source profile, not `import` |
| static names, interval store | `static-names add/build`, `interval-store add/build` | `static-names`, `interval-store` steps |
| footers → D1 | `index-sync` | `index-sync` |
| smoke | curl | `probe -c` |
| skip a date already scanned | an `aws s3 ls` guard | the scan-id resolver: verify a same-day re-run is refused or resumed |
| hand-off store (`data:`) | R2 directly | the profile requires `data:`, FUSE-mounted on Batch; for `local` it must accept an `r2://` store or a local dir. Verify, else add |

Runner setup: `actions/setup-java` (17+) and a pinned Nextflow, ~30 s. The job is `dt-cloud scan run -P <demo profile>` (the same head command the other deployments' crons run), so the workflow shrinks to install + credentials + one command.

### Steps

1. A demo scan profile on `demo` (`job/profiles/demo.yml` or wherever profiles live): `executor: {kind: local}`, a local `work:`, the three sources, the steps above.
2. Close the gaps the table marks "verify" (each its own `cloud` commit with a test; the local-executor test `cloud/tests/test_scan_nf.py` is the harness).
3. A parallel workflow (`scan-nf.yml`) on `demo` writing to a scratch prefix of the demo bucket, compared with the bash run's outputs for the same date (path store root sizes, scan rows, static-name catalog counts).
4. Switch the cron-dispatch target to `scan-nf.yml`; keep `daily-ingest.yml` a week as the manual fallback, then delete it.

After 10/19.

## Open questions

- Should `cloud` stay deployable on its own (a stack with no stores, as a smoke target), or is "every push to `cloud` reaches the demo via the auto-merge" enough canary?

[branch-layout.md]: branch-layout.md
[scan.nf]: ../job/nf/scan.nf
