#!/usr/bin/env bash
# The recorded gcs bench as one Batch task (`submit` renders it into the job spec): install the toolchain, unpack the
# bench's source, copy the store's current generation (`fetch-gen`), load it, run the query mix, check every answer
# equals the VM run's (the oracle-checked answers in `$OUT/inputs/`), upload results. Each phase is timed: copy + load
# is the restart cost a serving box would pay.
set -euo pipefail
: "${OUT:?}" "${SRC:?}" "${STORE:?}"
work=/mnt/disks/work
mkdir -p "$work" && cd "$work"
t() { date +%s.%N; }
phase() { echo "{\"phase\": \"$1\", \"s\": $(echo "$3 - $2" | bc)}" >> "$work/phases.jsonl"; }
t0=$(t)
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq build-essential bc python3 >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal >/dev/null 2>&1
export PATH=$HOME/.cargo/bin:$PATH
gcloud storage cp "$SRC" src.tar.gz && mkdir -p src && tar -xzf src.tar.gz -C src
(cd src && cargo build -q --release)
t1=$(t); phase setup "$t0" "$t1"
gen=$(src/fetch-gen "$STORE" "$work/store" | tail -1)
t2=$(t); phase copy "$t1" "$t2"
gcloud storage cp "$OUT/inputs/queries.jsonl" "$OUT/inputs/answers.jsonl" .
src/target/release/fsb bench "$gen" queries.jsonl -t "1,$(nproc)" -r 5 -l "gcs-batch-${BATCH_JOB_ID:-local}" \
  -o results.jsonl -a answers.batch.jsonl 2> bench.log
t3=$(t); phase bench "$t2" "$t3"
python3 - <<'PY'
import json
a = [json.loads(l) for l in open("answers.jsonl")]
b = [json.loads(l) for l in open("answers.batch.jsonl")]
assert a == b, f"{sum(x != y for x, y in zip(a, b))} answers differ from the oracle-checked run"
print(f"{len(a)} answers equal the oracle-checked run's")
PY
dest="$OUT/runs/${BATCH_JOB_ID:-local}-$(date -u +%Y%m%dT%H%M%SZ)"
gcloud storage cp results.jsonl answers.batch.jsonl bench.log phases.jsonl "$dest/"
echo "results: $dest"
