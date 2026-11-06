# cw's scan on CoreWeave compute

Run the parts of cw's scan that are bound to CoreWeave storage on CoreWeave's own CPU nodes, where compute is prepaid, instead of GCP Batch. Status: nothing is run or asked for yet. Research: 2026-10-28, read from Marin at `623134de90`; recheck it before relying on it.

## Why

- **Cost:** cw's labeled GCP spend, Nov 1–4, before the us-east1 move and `publish-r2 -S`: compute about $11.50/day (listing about $3, jobs with no step label about $6), network about $7.50/day. Moving everything saves at most about $11/day. Listing alone saves about $3/day.
- **Speed, unmeasured:** `cw-us-east-02a` is in the same zone as `marin-us-east-02a`. In-cluster reads go through `http://cwlota.com` (LOTA, a node-local cache) at about twice the rate of `https://cwobject.com` in Marin's benchmark. That was GETs: our listing is LISTs, which LOTA may not help.
- **Abstraction:** it would prove out a step running somewhere other than the head's executor.

## What's there

- **Nodes:** `cw-us-east-02a` has 4 reserved `cd-gp-a192-genoa` CPU nodes (192 vCPU, 1.5 TB RAM, 7.68 TB NVMe each), under Marin's grant. They also host the Iris controller and Marin's CPU tasks. Batch-band CPU work can be preempted for GPU work, and Marin saw CPU lockups from September on.
- **Iris:** runs a Pod per task on CoreWeave, with any image (`--task-image --no-sync`), resources, retries, secrets and priorities. It's a job scheduler, not a DAG engine.
- **Zephyr:** a dataset library. Our steps are CLIs, so it would be a rewrite: not proposed.
- **Marin's DAG layer (`marin.execution`):** rejected in `specs/done/workflow-engine.md`, which keeps Nextflow.

## Phase 1: one experiment, no pipeline change

List `marin-us-east-02a` from an Iris job, beside the same hour's cron listing, and compare.

1. Publish the scan image somewhere Iris can pull: public GHCR (the repo is public; check the image holds no secrets), or a pull secret.
2. Run, once each with `-E http://cwlota.com` and `-E https://cwobject.com`:
   ```bash
   iris --cluster=marin job run --target-cluster cw-us-east-02a --task-image IMAGE --no-sync \
     --cpu 32 --memory 64GB --disk 200GB --priority batch --max-retries 1 \
     -- disk-tree bulk-list -a s3://marin-us-east-02a -E ENDPOINT -P 32 -o OUT
   ```
3. Compare wall time, object count and bytes with the GCP listing (33–41 min recently, largest source). Passing means the same counts, allowing for what changed between the two listings' times.

## Phase 2, only if phase 1 is worth it and Marin agrees

- A profile's source (or step) can name another executor. Today `executor` is one per profile (`google-batch`, `awsbatch`, `local`), and `specs/pipelines-everywhere.md` §4 keeps it there.
- The head submits to Iris and polls (`iris job run --no-wait`), since Nextflow has no Iris executor. Nextflow's `k8s` executor would need its own namespace and RBAC on Marin's cluster, outside Iris and Kueue.
- Listing shards go to the data bucket as today. Steps that read GCS stay on GCP unless the data bucket moves too, which is a much larger change, and Marin's rule is no reading GCS data from CoreWeave without approval.

## Access needed

From Rafal Wojdyla or Russell Power, via #marin-eng:

1. **Marin Dev role** in GCP project `hai-gcp-models`, and **Iris IAP access** to `iris.oa.dev`, for Ryan's OA identity. `cw-us-east-02a` already admits OA addresses as submitters.
2. **OK to run a recurring job:** low priority, CPU only, about 4 times a day, each under an hour on 32 CPUs, to be sized by phase 1.
3. **A service identity that can submit,** for the cron. `allowed_submitters` has service-account entries.
4. **Which CAIOS key:** the scan's existing one, forwarded into the task, or a separate one.
5. **Anything already known** about LIST through LOTA, and egress from CoreWeave to GCS.

Phase 1 by hand needs only 1 and 4.

## Open

- Whether Marin wants this running on its nodes at all.
- The CPU lockups: are they still happening?
