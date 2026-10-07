# Ingest new captures from the cloud side

**From:** the `tauri-native-app` session (`wt/app`), 2026-10-01. Part of `rust-engine.md` phase 3; Ryan chose this option over keeping the submit on the laptop ("latter sg").

## Why

disky now captures in-process: the scan job's `to` writes the layer-1 capture (`r2://disk-tree/captures/<host>/<root slug>/<stamp>/` + `_SUCCESS.json`) from the app binary, with no Python. What's left on the laptop is `aws/submit`: it runs as disky's `then` step, with `AWS_PROFILE=r` (RAC AWS keys on the laptop). For anyone but Ryan, the laptop should hold an R2 write credential and nothing else. The ingest should start because a capture landed.

## Shape (recommended)

1. **R2 event notification** on bucket `disk-tree`: object-create, prefix `captures/`, suffix `_SUCCESS.json` → a Cloudflare Queue (`disk-tree-captures`). The manifest is written last, so its arrival means the capture is complete.
2. **Consumer Worker** (small, its own `wrangler.toml`; or Pulumi under `cf/` if that's where m3's CF IaC lives):
   - Per message, derive the capture dir (the key minus `/_SUCCESS.json`).
   - Skip if `<capture dir>/_INGEST.json` exists, through the R2 binding. Queues deliver at least once.
   - Call Batch `SubmitJob` (`disk-tree-m3` / `disk-tree-m3-ingest`, `containerOverrides.command = ["r2://disk-tree/<capture dir>"]`), signed with `aws4fetch`.
   - Write `_INGEST.json` `{job_id, submitted}`.
   - On error, retry the message.
3. **IAM:** a dedicated user/key for the Worker, allowed only `batch:SubmitJob` on that queue + job definition ARNs. Store it as Worker secrets. Add it to `aws/__main__.py`.

Alternative, if a CF→AWS key is unwelcome: an EventBridge-scheduled Lambda (every 15 min) that lists `captures/**/_SUCCESS.json` without an `_INGEST.json` and submits. That needs R2 read creds in AWS instead, and adds up to 15 min of latency.

## Also

- `ingest.sh` hard-codes `STORE=laptop`. With more than one host, derive the store from the capture's `<host>` segment, or keep a host → store map. Today only `m3` captures.
- Make the ingest idempotent per capture, not just per submit. A re-sent message must not double-write an index gen.

## Cutover

1. Once the trigger is live and has ingested one disky capture end to end, drop `then` from the scan job in `~/.config/disk-tree/disky.json`.
2. Keep `aws/submit` for manual re-ingests.
3. Until then, disky's `then` keeps submitting, and both paths can't run at once (no trigger yet).

## disky's scan job config (for reference)

```json
"scan": {"to": "r2://disk-tree/captures", "host": "m3", "log": "index",
         "env": {"AWS_PROFILE": "m3", "DISK_TREE_R2_ENDPOINT_URL": "https://…r2.cloudflarestorage.com"},
         "then": {"command": ["/Users/ryan/c/disky/wt/m3/.venv/bin/python", "/Users/ryan/c/disky/wt/m3/aws/submit"],
                  "env": {"AWS_PROFILE": "r"}}}
```

`aws/laptop-scan` was retired on 2026-10-05 after verifying the in-process capture and R2 trigger. The drainer's post-delete refresh now invokes `disky scan now`, using the same scan job and scope settings as scheduled scans.

## Status (m3): live since 2026-10-02

Built in `3e8e6c3`, applied 2026-10-02:
- `cf/__main__.py`: queue `disk-tree-captures`, and the `disk-tree` bucket's event notification (`PutObject` / `CompleteMultipartUpload` / `CopyObject`, prefix `captures/`, suffix `_SUCCESS.json`). The `cf/` token (`CLOUDFLARE_API_TOKEN`, account token "disk-tree-wrangler") gained account-wide Queues, Workers Scripts and Workers R2 Storage Write for it.
- `aws/__main__.py`: IAM user `disk-tree-m3-capture-trigger`, allowed only `batch:SubmitJob` on the queue and the job definition. No access key in Pulumi: `cf/capture-trigger/put-secrets` mints one straight into the Worker secrets (and rotates out older keys on a re-run).
- `cf/capture-trigger/`: the Worker. It is a serial consumer (`max_concurrency = 1`). The `_INGEST.json` marker is written `submitting` before the call and `submitted` with the job id after. A failed submit deletes the marker and retries the message.

Apply order, for reference: `pulumi up` (aws, then cf) → `pnpm -C infra/cf/capture-trigger run deploy` (`pnpm deploy` is pnpm's own command) → `infra/cf/capture-trigger/put-secrets` (`AWS_PROFILE=r`, under direnv). Then drop `then` from disky.json's scan job at the same time: `aws/submit` writes no marker, so while both run, a capture is ingested twice.

Cutover done 2026-10-02: `then` dropped from `disky.json`; a Scan now captured `captures/m3/root/2026-10-02T14-59-17Z` (`_SUCCESS.json` 15:05:24Z), the Worker submitted Batch job `7e6297c8…` at 15:05:30Z, and it succeeded at 11:08 EDT with index gen `listing/laptop/2026-10-02/index/202610021506` (R2 + D1 `index_schema`; `du -p` reads it).

Follow-ups (not blocking): the `STORE` per `<host>` derivation in `ingest.sh` (only `m3` captures today), and ingest idempotency per capture (the marker covers redelivery, not a manual `aws/submit` re-run).
