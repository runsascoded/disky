# Nextflow work ownership observations

`scan head` keeps the existing mutable `runs/<scan>/` report links and additionally publishes an immutable receipt beneath its configured work root:

```
ownership/v1/<deployment>/<store>/<actual-run-id>/<head-attempt-uuid>/receipt.json
ownership/v1/<deployment>/<store>/<actual-run-id>/<head-attempt-uuid>/trace.tsv
```

The attempt UUID distinguishes re-entry under the same actual run ID. Primary/meta heads and retries never share a receipt key merely because they share a scan minute. When neither `DT_RUN_ID` nor `BATCH_JOB_UID` was supplied, the actual ID remains null and the path uses `_unrecorded`; any existing scan-derived local recording ID is recorded separately. These receipts are audit observations, not an index unique by actual run ID alone.

The receipt captures deployment/store/scan/generation/session identity, head start/finish/exit or signal, configured source URIs and data/work roots, the loaded profile's SHA-256, the image's source stamp and task image. A configured tag is marked unpinned; absent source stamps remain null. It does not resolve mutable tags or source objects into fictitious immutable revisions.

The shared Nextflow config retains its default trace metrics and adds `workdir`, the [documented full task-directory field][Nextflow trace fields]. The parser uses the exact `workDir` configured for that run: an absolute local path or a `gs://` root, followed by a full task hash directory. Abbreviated paths, relative/unknown roots, mounted aliases and foreign paths stay unresolved; the parser never expands a shortened display hash with a glob. `CACHED` is recorded as another reference to that directory, including references shared between tasks or runs.

Trace copying, hashing and upload use bounded buffers, and the raw snapshot stops at 16 MiB plus a one-byte oversize probe. An oversized source is explicitly marked truncated: its byte count and SHA-256 describe only the retained prefix, never the full source; the separately recorded source size is the file's size when opened. JSON parsing is limited to the first 256 KiB and 1,024 observed task rows; exceeding either limit also produces an explicit incomplete observation. Missing, unreadable, empty, malformed, unterminated and nonterminal traces are explicit. The bounded raw snapshot is retained as an immutable sibling when readable. Raw trace metrics remain in that artifact; the JSON contains only the curated task identity/status/workdir fields. Even a well-formed trace describes observed rows only, not a complete census of every workdir the session could resume.

Publication is create-only on local and GCS work roots, with full digest/size read-back. Identical retries are accepted; an existing mismatch is refused. Other storage backends are reported as unsupported rather than using a non-atomic check-then-overwrite. The raw trace publishes before its receipt. A publication failure is logged as an ownership shortfall and does not prevent the original scan-run finish from being recorded.

No cleanup, expiry, output-parity check, successful-publication proof, resume fence or deletion authorization is introduced. A killed head that cannot finalize may have no receipt. A future retention planner must preserve incomplete or absent ownership and account for active/failed/resumed references independently; receipt presence alone is insufficient.

[Nextflow trace fields]: https://docs.seqera.io/nextflow/reference/config/trace/
