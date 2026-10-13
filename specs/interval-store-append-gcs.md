# Interval store: the per-scan append in the gcs job

**For the gcs session.** `cloud` has `dt-cloud interval-store append` (specs/interval-store.md §2.7). This adds it to `job/run.sh`, after the static-names step and before index-sync, off unless `INTERVAL_STORE_APPEND=1`, so it can be enabled on its own. The site keeps reading per-scan stores until the read switch (`PATH_STORE`), which is separate and needs Ryan's go.

- **Non-fatal and bounded:** `timeout ${INTERVAL_STORE_TIMEOUT:-60m}`; a failure or timeout warns, posts to Slack with the resume command, records `phase interval-store "failed (exit N)"`, and the scan publishes as usual.
- **Catch-up:** `-c` appends any earlier published scan still pending, in scan-id order. Every stage skips what is done, so a rerun resumes.
- **Scan runs:** `phase interval-store` (the scan-runs record).
- **Image:** the profile pins the 2026-10-08 job image, which predates `interval_append`; the step passes `INTERVAL_STORE_IMAGE=$JOB_IMAGE` so the append's Batch tasks run this job's own code.
- **Labels:** the jobs carry `component=interval-store` on top of `$DISKY_LABELS` (defaulted to `app=disky,deployment=gcs` if the job's env lacks it).
- **Measured** (§6.3 of the spec): a few minutes and well under $1 per scan.

Apply on `gcs` (after it merges a `cloud` with `interval_append`): `git apply` the diff below (also at `wt/iv-append/tmp/gcs-interval-store-append.patch`; checked with `git apply --check` against `gcs` at `d503928a`).

```diff
--- a/job/run.sh
+++ b/job/run.sh
@@ -413,6 +413,29 @@
   fi
 fi

+# Interval store (specs/interval-store.md §2.7): append this scan to the change-
+# interval path store as a run beside its base generation — per key range on
+# Batch, the run's served sorts, the binary counter's merges, R2, then the
+# immutable `manifests/<scan>.json`. `-c` first appends any earlier published
+# scan still pending, in scan-id order. Off unless INTERVAL_STORE_APPEND=1 (the
+# site reads the store only under `PATH_STORE=opt-in` + `ps=iv` until the read
+# switch). Bounded and never fatal: past INTERVAL_STORE_TIMEOUT or on a failure
+# the scan still publishes and reads per-scan; every stage skips what's done,
+# so a rerun resumes. Its Batch tasks run this job's own image (the profile
+# pins an older one, without `interval_append`).
+if [ "${INTERVAL_STORE_APPEND:-0}" = "1" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
+  if R2_ENDPOINT=${R2_ENDPOINT:-https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com} INTERVAL_STORE_PROFILE=gcs \
+      INTERVAL_STORE_IMAGE=${INTERVAL_STORE_IMAGE:-$JOB_IMAGE} DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=gcs} \
+      timeout "${INTERVAL_STORE_TIMEOUT:-60m}" dt-cloud interval-store append -c "$SNAP_ID"; then
+    phase interval-store
+  else
+    rc=$?
+    echo "WARN: interval-store append failed for $SNAP_ID (exit $rc$([ $rc = 124 ] && echo ', timed out'))" >&2
+    slack_post "⚠️ \`dt-cloud\` $SNAP_ID: the interval store append failed (exit $rc$([ $rc = 124 ] && echo ', timed out after '"${INTERVAL_STORE_TIMEOUT:-60m}")) — the scan publishes and reads per-scan. Resume: \`INTERVAL_STORE_PROFILE=gcs dt-cloud interval-store append -c $SNAP_ID\`."
+    phase interval-store "failed (exit $rc)"
+  fi
+fi
+
 # Footer-in-D1: sync the path-index parquet footer into the site's D1 so the
 # reader skips the cold-isolate footer parse (specs/done/path-agnostic-serving.md
 # §2.1). Needs CLOUDFLARE_API_TOKEN (D1 write) + CLOUDFLARE_ACCOUNT_ID — set as
```
