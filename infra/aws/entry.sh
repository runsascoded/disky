#!/usr/bin/env bash
# The ingest image's entrypoint: `INGEST_PIPELINE=nf` runs the capture on the scan pipeline (`scan.sh`: its head
# records the run, specs/m3-scan-nf.md); otherwise the bash ingest (`ingest.sh`) under `dt-cloud scan-run supervise`,
# which records a termination no trap sees. The bash path stays until the pipeline's first clean week.
case ${INGEST_PIPELINE:-} in
  nf) exec bash /scan.sh "$@" ;;
  # one-off: the age pyramids of scans indexed before the pipeline built them (its argument is ignored)
  age-backfill) exec bash /age-backfill.sh ;;
esac
exec /app/.venv/bin/dt-cloud scan-run supervise -- bash /ingest.sh "$@"
