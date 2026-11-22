# Orchestrator abstraction: one scan DAG, Nextflow or Marin's executor (Iris) underneath

Status: proposed 2026-11-21. Ryan approved "do 1–3, prep 4, then re-evaluate". §1–3 implemented on `orch` (below, "As built"); §4 prepped (`FrayBackend`, not wired), awaiting the re-evaluation.

## Why

Today the scan DAG lives in `job/nf/scan.nf`, and `dt-cloud scan head` (`scan_head.py`) is a Nextflow driver: it derives the session, handles resume through `.nextflow/history` + `NXF_CLOUDCACHE_PATH`, cancels on TERM, sums task cost and names the first failed task. We want to be able to run the same DAG under Marin's executor (`marin.execution` `StepSpec`/`StepRunner` over fray's `Client`), with fray's Iris backend on CoreWeave. CoreWeave CPU is about free at the margin for roughly a year (`specs/oa-hosting.md`), and cw's data sits next to it on CAIOS.

The step bodies are already engine-neutral (`dt-cloud scan step <name>`, `scan_steps.py`), steps hand off through the data bucket, and the run's DAG is reduced to `{nodes, edges}` for the site (`run_dag.py`). The Nextflow-only parts are the DAG's definition and the head.

Background: `specs/done/workflow-engine.md` (10-16) compared Marin's executor (option A) with Nextflow (B) and chose B. A was dropped because `StepRunner` lives in the `marin` package (jax, transformers, …), and running on Iris uses Marin's capacity.

## 1. The DAG as data

Move the DAG out of `scan.nf` into Python data in `cloud/src/dt_cloud/` (e.g. `scan_dag.py`):
- each step: name, `needs`, resources (cpu, memory, disk, machine type / spot), image, which profile options enable it, and its fan-out (`index_part` over `options.index.parts`, `list_source` over sources);
- built from the scan profile (`scan_profile.py`), so a deployment's options decide which steps exist;
- shaped like Marin's `StepSpec` where that costs nothing, so a later adapter is thin.

## 2. `scan.nf` rendered from it

Generate `scan.nf` (and its `base.config` resource lines) from the data, or keep it hand-written with a test that the rendered graph equals the data's graph. Prefer generation if the 241 lines template cleanly. There is no official Python DSL for Nextflow: it's Groovy, so a renderer is the way.

Acceptance: a scan run under the rendered `scan.nf` is identical to today's: same processes, same `run_dag` output, same resume behaviour (verified with an injected failure, as in workflow-engine.md §7).

## 3. A driver interface

`scan head` talks to a small backend interface. It submits the DAG, waits, reports each step's status and logs, cancels, resumes (skipping steps whose output is marked done), and supplies cost and the first failed step. Its first backend is Nextflow, exactly today's behaviour. The run record, phases, alerts and `run_dag` keep their current shapes; only the engine name varies (`run_dag.ENGINES`).

### As built (§1–3)

- **`scan_dag.py`**: `build(profile, reproc=)` → `ScanDag(nodes)`, each `Node(name, deps, tasks, label, fanout, time, retries)`; a task is `Task(calls, tag)`, a call `Call(step, arg)` (one `dt-cloud scan step <step> [arg]`). Node names are today's process names (`list_source`, `index_part`, `index`, `index_rows`, `publish_r2`, `static_names`, `interval_store`, `index_sync`, `post`, `over_time`, `bucket_check`, `pre`, `side`), so phases' `task` and `run_dag` ids don't change. Everything `scan.nf`'s workflow used to decide (`fold`, `side`, `pre`, `index.parts`, REPROC's skipped lists, a local data dir's static-after-publish, labels by `machines` key) is decided here. `graph()` is the run's `run_dag.Dag`; `resources(profile, node, task)` is a task's `Machine` (a list task's from its source). Shaped like `StepSpec` (`name`, `deps`, `resources`); a fan-out node's tasks would be one StepSpec each.
- **`scan.nf` is rendered per run** (`scan_dag_nf.render`) into the launch dir: `job/nf/prelude.nf` (what was `scan.nf`'s params and helpers, `git mv`'d) + one process per node + the workflow. A process's script is its calls as literals (a fan-out's by tag); deps arrive `gathered`. `params.json` keeps only the run's facts (identity, sources, secrets, env, labels, cache); the DAG-shaped keys (`steps`, `pre`, `side`, `post`, `fold`, `*_label`, `index_parts`, `skip_list`, `static_after_publish`, `over_time`) are gone.
- **Checked**: `test_scan_dag.py` (each profile shape's nodes, golden `cw-s3.scan.nf`), `test_scan_nf.py::test_rendered_scan_nf_is_the_dag` (Nextflow's `-preview` of the rendered file reduces to exactly `graph()`, over folded / unfolded / REPROC / pre+side / sharded), and the existing end-to-end `test_scan_nf.py` (order, injected failure, `-resume` reruns only the post task). A one-off (on `mgu`, 2026-11-21) compared the old hand-written `scan.nf`'s `-preview` (its own head writing its params, at `f73f20990`) with the rendered one's over eight shapes: folded, folded with a `gs://` data dir, unfolded, REPROC, pre + side, sharded `index_part`, lean (static-names / publish-r2 / interval-store / over-time skipped) and no publish-r2. `run_dag` reduced each pair to the same graph.
- **A failed dep holds its dependents** (found by `test_scan_fallback` flaking under load, 2026-11-21): a failed task emits nothing but its channel still closes, so `toList` emitted a short list and, under `errorStrategy 'finish'`, a dependent could still start: `post` (cw: `index-sync`) after a failed `interval-store`. The hand-written `scan.nf` had the same race (1 in 28 concurrent runs at `f73f20990`, 2 in 28 rendered). The renderer knows each dep's task count, so `gathered(ch, n)` emits only a complete set: 0 in 42 since.
- **Resume across the change**: a task's Nextflow hash includes its script's source text and input values, which the rendering changes, so a scan that failed under the old `scan.nf` and is resumed under the new image reruns every task (each step still skips what its own outputs mark done). Same-image resumes are unaffected.
- **`scan_backend.py`**: `Run` (the scan's identity, session, DAG, launch dir, env) and `Backend` (`prepare`, `store_preview`, `start`, `wait`, `progress`, `interrupt`, `cancel`, `error`, `finish`, `cost`, `dag_file`, `engine`). `head()` is engine-neutral: identity, recording, heartbeat, TERM; `NextflowBackend` (in `scan_head.py`, beside the Nextflow helpers) is today's path unchanged: history line, rendered `scan.nf`, params/executor/run configs, `-preview` DAG, `-resume <session>`, the `nf-session` sweep, first failed task from the trace, reports, ownership receipt. `backend_for(profile, nf_dir)` picks it for every executor kind.

## 4. Prep for an Iris backend (no CoreWeave jobs until Ryan has Marin's go)

- **The `marin.execution` split**, in a fork at `Open-Athena/marin` (clone `~/c/oa/marin`, its own session):
  - Move `lib/marin/src/marin/execution/` (9 files, ~100 KB; 13 upstream commits in the last 3 months) into a new workspace member, e.g. `lib/marin-execution`. `marin.execution` stays as a re-export shim, so none of the ~350 files that reference it change.
  - Untangle three imports:
    - `jax` + `numpy` in `fingerprint.py`: make these lazy or optional;
    - `marin.training.run_environment.dependency_groups_for_resources` in `remote.py` and `step_runner.py`: inject it, or move it into the new package;
    - `marin.utilities.json_encoder`: move it or copy it.
  - The new package depends only on `marin-fray`, `marin-rigging` and `marin-iris` (`step_runner` / `step_status` read `iris…job_info`). Check how heavy `marin-iris` is without its extras.
  - Merge upstream periodically. Upstream edits to the moved files should follow git's rename detection. If they stay mechanical, PR the split to Marin.
- **A fray backend for GCP Batch**, from `append_runner.BatchRunner`, so the same DAG could run under `StepRunner` on Batch too. Nextflow would then be optional everywhere. That's a re-evaluation point, not a goal.
- **The Iris backend**: it submits each step as an Iris job through fray's `iris_backend`. Test it with fray's `LocalClient` first. The first CoreWeave submission waits for Ryan's go, after Marin's ok (Russell for `infra`, Isaac for grant capacity).
- **Iris's namespace isn't a blocker** for this route. Iris runs its task pods in its own namespace `iris`, and we'd be submitting Iris jobs, not creating pods. A namespace of our own was needed only for Nextflow's own Kubernetes executor. The real ask is permission to use Iris and its capacity.
- **Data locality**: gcs's data is in GCS us-east1, so gcs scans on CoreWeave would read across clouds (egress). Iris fits cw first (CAIOS); gcs stays on Batch. So the abstraction serves two backends at once.

### Prep as built (§4): `FrayBackend`, without `marin.execution`

Reading fray (`lib/fray`): its `Client` (`submit(JobRequest) → JobHandle`, `status`, `terminate`, `logs`) is all a backend needs, and `marin-fray` depends only on cloudpickle, humanfriendly and `marin-rigging` (+ `marin-iris` for its `iris` extra). Since the DAG is already data and resume is already per-task done markers, the head can schedule the DAG itself over a fray `Client`; `StepRunner`'s own machinery (hash-addressed output paths, artifact records, locks) doesn't map onto our steps, which write fixed data-store paths and skip what's done themselves. So **the Iris backend doesn't need the `marin.execution` split**; the split is only worth doing if we want `StepRunner` for its own sake. Re-evaluation point.

- **`scan_backend_fray.FrayBackend(client)`** (engine `fray`): a task is submitted once its deps' tasks all succeeded (≤ `limit` in flight); a failure finishes the running tasks and starts none (`errorStrategy 'finish'`); a node's `retries` → `max_retries_failure`. Each success writes `<work>/fray/<session>/<node>/<tag>.done`; a later head for the scan skips those (`cached_tasks` in the run's DAG), `fresh` ignores them. A task is `bash -c` over its env (identity, `NF_PROCESS`/`NF_TAG`/`NF_MACHINE`, profile env, run id, profile JSON) and its calls through `dt-cloud secrets exec`, as `stepScript` writes them (`LocalClient` runs the command as is, so the env rides in the script). Resources: `scan_dag.resources` → `ResourceConfig(cpu, ram, disk, image)`, a GCE type read as its shape (`machine_shape`). `interrupt`/`cancel` terminate the running jobs. Its DAG file is `run_dag`'s JSON (`run_dag.ENGINES` gains `fray`; `scan-run finish -E`).
- **Tested** with a scripted fake client (waves, failure-finish, resume, `fresh`, interrupt, the task script, shapes); `test_local_client` runs it through fray's real `LocalClient` where fray is installed (passed on `mgu` with `uv run --with marin-fray==0.2.329.dev44910398524`, Python 3.13; CI skips it).
- **`marin-fray` is the `fray` extra** (`cloud[fray]`, `==0.2.329.dev44910398524; python_version < '3.14'`; Ryan's go 2026-11-21), so CI (`--all-extras`) runs `test_local_client`. No fork: upstream publishes `marin-fray` to PyPI on every merge to main (`0.2.N.devM`, each pinning `marin-rigging` / `marin-iris` to itself), so the pin is one exact version. The marker is for the lock: Marin's packages declare `>=3.12,<3.14`, and the workspace lock resolves `cloud`'s `>=3.12` for later Pythons too (disky runs 3.12 in the image, 3.12/3.13 in CI). The lock only gained packages; no existing pin moved.
- **fray's `iris` extra doesn't resolve with disky's lock**: `marin-iris` pins `marin-finestore`, which needs pyarrow >= 23, and pyrmts' core declares `pyarrow>=21,<23` (disky is on 22.0.0). An Iris client needs one of: pyrmts' range widened to 23 and disky moved to pyarrow 23 (a re-check of byte-identical outputs), or the head's Iris client in an environment of its own (the head only submits; the tasks run disky's image).
- **Not wired**: `backend_for` still picks Nextflow for every profile; `executor.kind: iris` (a client from `fray.iris_backend` over Iris's config) waits for the two points here and above. The `marin.execution` split (`~/c/oa/marin`, `wt/execution-pkg`, no commits) is dropped (Ryan, 2026-11-21).

#### Secrets and identity on Iris (research, 2026-11-21)

- **Today, on Batch**, everything rides the job's GCP service account through the metadata server: `secrets exec` reads each binding from Secret Manager (cw: 9, the CAIOS keys, Slack, CF Pages and Analytics tokens, the site grant, R2 endpoint and keys), Nextflow's `work` and the data dir are `gs://oa-cw-s3-usage-data`, and the image is in Artifact Registry.
- **Marin's own Iris jobs** get secrets as files: `EnvironmentConfig.secrets` (name → value) is delivered as a read-only Kubernetes Secret volume at `/run/rigging/secrets/<NAME>`, and rigging's `get_secret(NAME)` reads the file, then the env. By default (`SecretBehavior.INHERIT`) a submission also forwards the submitter's mounted bundle plus `HF_TOKEN`, `WANDB_API_KEY` and `CW_KEY_ID`/`CW_KEY_SECRET` (the last two also written as `AWS_SHARED_CREDENTIALS_FILE`); `ISOLATE` sends only the explicit map. Their CoreWeave jobs have no GCP identity: storage is CAIOS (`config/coreweave.yaml`: `s3://marin-us-east-02a/marin`), and GCP credentials appear only in CI (`IRIS_CI_GCP_SA_KEY`, to resolve the controller's signing key). Marin's auth notes prefer platform-injected secrets to a runtime Secret Manager client, and keep `gcp-secret://` to GCP hosts.
- **So a scan on Iris needs two things**: its step secrets, and a GCP identity for `gs://` (work, data) and the image pull, unless those move off GCP. Options:
  - (a) **The head resolves the bindings** (it runs where it can read Secret Manager) and sends them as `EnvironmentConfig.secrets` with `ISOLATE`; a task's `secrets exec` reads `/run/rigging/secrets/<NAME>` instead of Secret Manager. Marin's convention, no key; the values pass through the Iris controller and sit in a Kubernetes Secret in Iris's namespace.
  - (b) **A key for a dedicated, minimal service account** (the data bucket, Artifact Registry read, the deployment's secrets), itself delivered as in (a) and named by `GOOGLE_APPLICATION_CREDENTIALS`. Everything else unchanged; a long-lived key, which Marin's notes say to retire in favour of Workload Identity Federation.
  - (c) **Workload Identity Federation** from the CoreWeave cluster's OIDC issuer: keyless, but Iris's task pods would have to project a service-account token with GCP's audience, which I didn't find in Iris.
  - (d) **Move an Iris deployment's `work` (and, later, data) to CAIOS**: no GCP identity for storage, CAIOS keys inherited as Marin's jobs get them; the published store (R2, D1) is reached with secrets either way.
  - Recommendation: (a) for step secrets, plus (d) for `work` (fray markers + scratch) and (b) only for the data dir while it stays in GCS; ask Marin whether (c) is on Iris's roadmap. Unverified: whether the CoreWeave cluster can pull from our Artifact Registry.

## Sessions

- disky side (1–3, the Iris backend): a worktree `wt/orch` on a branch off `cloud`, its own session; `[cloud]` commits sent to root as usual.
- marin side (the split): a session in `~/c/oa/marin`, branch `rw/execution-pkg` pushed to `o`.
