#!/usr/bin/env bash
# The VM pilot box's startup script (specs/vm-pilot.md "Runbook"). infra/gcp/shared puts this file in the instance's
# `startup-script` metadata; google-guest-agent runs it as root at every boot, and
# `sudo google_metadata_script_runner startup` runs it again (e.g. after a `vmPilot.ref` change). Idempotent.
#
# Its config is the instance's non-secret metadata (`disky-*`, set by the stack). Secrets stay in Secret Manager: the
# services read them at start (`with-secrets`, into their own environment), never into a file, argv or metadata.
#
#   /var/lib/disky (the data disk)   src/ (the checkout), bin/<tree>/ (vmd + fsb per bench/full-scan tree), venv/<sha>/
#                                    (dt-cloud), packs/<tenant>/current.fsb, jobs/ + work/ (the local job backend),
#                                    docker/ (Docker's data root), cargo/ + uv/ (caches)
#   /etc/disky                       tenants.json, vmd.json (pack-sync writes it), *.secrets (VAR=secret-name lines:
#                                    names only), job.env (the local job backend's)
set -euo pipefail
{ set +x; } 2>/dev/null
export HOME=${HOME:-/root}

CLOUDFLARED_VERSION=2026.11.0
CLOUDFLARED_SHA256=5305dbaf62984e92e7daa29cdd6a35a4797edc23385d4c5b8422ff97eb96a19b
UV_VERSION=0.13.8
UV_SHA256=5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91
# What job/Dockerfile's `fsb-build` stage builds on
RUST_IMAGE=rust:1.91-slim-bookworm
PYTHON=3.12

DATA=/var/lib/disky
SRC=$DATA/src
ETC=/etc/disky
LIB=/usr/local/lib/disky
UNITS=/etc/systemd/system

log() { echo "disky-bootstrap: $*"; }
md() { curl -fsS -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
# Install $1 at $2 (mode $3) when it differs; status 0 when it changed.
put() {
  if [ -e "$2" ] && cmp -s "$1" "$2"; then return 1; fi
  install -D -m "$3" "$1" "$2.new" && mv -f "$2.new" "$2"
}
# Write stdin to $1 (mode $2) when it differs; status 0 when it changed.
put_text() {
  local tmp
  tmp=$(mktemp)
  cat > "$tmp"
  if put "$tmp" "$1" "$2"; then rm -f "$tmp"; return 0; fi
  rm -f "$tmp"
  return 1
}
fetch() {  # URL SHA256 DEST
  curl -fsSL --retry 5 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c --quiet -
}

REPO=$(md disky-repo)
REF=$(md disky-ref)
TUNNEL_SECRET=$(md disky-tunnel-secret)
REGISTRIES=$(md disky-registries)
SCAN_MEMORY_MAX=$(md disky-scan-memory-max)
SERVE_MEMORY_MIN=$(md disky-serve-memory-min)
TENANTS=$(md disky-tenants)

# 1. The data disk: formatted once, mounted at every boot.
DEV=/dev/disk/by-id/google-disky-data
if ! blkid "$DEV" >/dev/null 2>&1; then
  log "formatting $DEV"
  mkfs.ext4 -q -L disky-data -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard "$DEV"
fi
mkdir -p "$DATA"
grep -q " $DATA " /etc/fstab || echo "LABEL=disky-data $DATA ext4 discard,defaults,nofail 0 2" >> /etc/fstab
mountpoint -q "$DATA" || mount "$DATA"
mkdir -p "$DATA"/{bin,packs,jobs,work,cargo,uv,venv,build} "$ETC" "$LIB"

# 2. Packages: Docker (the local job backend's engine), git, jq; cloudflared and uv pinned and checksummed; the Ops Agent
# ships the journal (every unit's output) to Cloud Logging.
export DEBIAN_FRONTEND=noninteractive
PKGS=(docker.io git jq ca-certificates)
if ! dpkg -s "${PKGS[@]}" >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q "${PKGS[@]}"
fi
if [ "$(cloudflared --version 2>/dev/null | awk '{print $3}')" != "$CLOUDFLARED_VERSION" ]; then
  log "installing cloudflared $CLOUDFLARED_VERSION"
  fetch "https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-linux-amd64.deb" \
    "$CLOUDFLARED_SHA256" /tmp/cloudflared.deb
  dpkg -i /tmp/cloudflared.deb
  rm -f /tmp/cloudflared.deb
fi
if [ "$(uv --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]; then
  log "installing uv $UV_VERSION"
  fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" "$UV_SHA256" /tmp/uv.tgz
  tar -xzf /tmp/uv.tgz -C /usr/local/bin --strip-components=1 uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx
  rm -f /tmp/uv.tgz
fi
if ! dpkg -s google-cloud-ops-agent >/dev/null 2>&1; then
  log "installing the Ops Agent"
  curl -fsSL -o /tmp/add-ops-agent-repo.sh https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
  bash /tmp/add-ops-agent-repo.sh --also-install
  rm -f /tmp/add-ops-agent-repo.sh
fi

# 3. The checkout: `disky-ref` (a sha, or a branch: whatever it names at this boot).
[ -d "$SRC/.git" ] || git init -q "$SRC"
git -C "$SRC" fetch -q --depth 1 "$REPO" "$REF"
SHA=$(git -C "$SRC" rev-parse FETCH_HEAD)
git -C "$SRC" checkout -q --force --detach "$SHA"
HERE=$SRC/deploy/vm-pilot
log "checkout $SHA ($REF)"

# 4. The slices first (the build below runs in scan.slice), with this box's budgets as drop-ins over the tracked
# placeholders.
changed=0
for u in serve.slice scan.slice; do put "$HERE/$u" "$UNITS/$u" 0644 && changed=1; done
printf '[Slice]\nMemoryMin=%s\n' "$SERVE_MEMORY_MIN" | put_text "$UNITS/serve.slice.d/budget.conf" 0644 && changed=1
printf '[Slice]\nMemoryMax=%s\n' "$SCAN_MEMORY_MAX" | put_text "$UNITS/scan.slice.d/budget.conf" 0644 && changed=1
[ "$changed" = 0 ] || systemctl daemon-reload

# 5. Docker: its data root on the data disk; job containers kept off the metadata server (`no-metadata.conf`); the
# tenants' Artifact Registry hosts pulled as the job's own account (`docker-credential-disky`: the token the local job
# backend minted, from the environment), never as the box's.
docker_changed=0
printf '{"data-root": "%s/docker", "log-opts": {"max-size": "50m", "max-file": "3"}}\n' "$DATA" \
  | put_text /etc/docker/daemon.json 0644 && docker_changed=1
put "$HERE/docker-no-metadata.conf" /etc/systemd/system/docker.service.d/no-metadata.conf 0644 && { systemctl daemon-reload; docker_changed=1; }
put "$HERE/docker-credential-disky" /usr/local/bin/docker-credential-disky 0755 || true
printf '%s\n' "$REGISTRIES" | jq -R 'split(",") | map(select(length > 0)) | {credHelpers: (map({(.): "disky"}) | add // {})}' \
  | put_text /root/.docker/config.json 0600 || true
systemctl enable -q docker
if [ "$docker_changed" = 1 ]; then systemctl restart docker; else systemctl start docker; fi
while iptables -D DOCKER-USER -d 169.254.169.254/32 -j REJECT 2>/dev/null; do :; done  # the old all-traffic rule, if a boot added it
iptables -C DOCKER-USER -d 169.254.169.254/32 -p tcp ! --dport 53 -j REJECT --reject-with tcp-reset  # the drop-in's rule is in place, or this run stops here

# 6. vmd + fsb, built once per bench/full-scan tree in job/Dockerfile's Rust image (cargo's registry cached on the disk).
TREE=$(git -C "$SRC" rev-parse "$SHA:bench/full-scan")
BIN=$DATA/bin/$TREE
if [ ! -x "$BIN/vmd" ] || [ ! -x "$BIN/fsb" ]; then
  log "building vmd + fsb (bench/full-scan $TREE)"
  docker run --rm --cgroup-parent=scan.slice \
    -v "$SRC/bench/full-scan:/src:ro" -v "$DATA/cargo:/usr/local/cargo/registry" -v "$DATA/build:/target" \
    -e CARGO_TARGET_DIR=/target -e CARGO_PROFILE_RELEASE_DEBUG=0 -w /src \
    "$RUST_IMAGE" cargo build --release --locked --bin vmd --bin fsb
  install -D -m 0755 "$DATA/build/release/vmd" "$BIN/vmd"
  install -D -m 0755 "$DATA/build/release/fsb" "$BIN/fsb"
fi
vmd_changed=1
put "$BIN/vmd" /usr/local/bin/vmd 0755 || vmd_changed=0
put "$BIN/fsb" /usr/local/bin/fsb 0755 || true

# 7. dt-cloud (the local job backend's supervisor, `vm-pack pull`), one venv per checkout: a running job keeps its own.
VENV=$DATA/venv/$SHA
if [ ! -x "$VENV/bin/dt-cloud" ]; then
  log "dt-cloud venv for $SHA"
  (cd "$SRC" && UV_PROJECT_ENVIRONMENT="$VENV" UV_CACHE_DIR="$DATA/uv" UV_PYTHON_INSTALL_DIR="$DATA/uv/python" \
    uv sync -q --frozen --no-dev --no-editable --package dt-cloud --python "$PYTHON")
fi
ln -sfn "$VENV" "$DATA/venv/current"
ln -sfn "$DATA/venv/current/bin/dt-cloud" /usr/local/bin/dt-cloud
# Older venvs past the newest two (by mtime), never the current one
n=0
while IFS= read -r v; do
  case "$v" in */current/|*/"$SHA"/) continue ;; esac
  n=$((n + 1))
  [ "$n" -lt 2 ] || rm -rf "$v"
done < <(find "$DATA/venv" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p/\n' | sort -rn | cut -d' ' -f2-)

# 8. Config: the tenants, and which secret each service's variables come from (names only).
printf '%s\n' "$TENANTS" | jq . | put_text "$ETC/tenants.json" 0644 || true
jq -r '.[] | "VMD_KEY_\(.name | ascii_upcase | gsub("-"; "_"))=\(.vmd_key_secret)"' "$ETC/tenants.json" \
  | put_text "$ETC/vmd.secrets" 0644 && vmd_changed=1
printf 'TUNNEL_TOKEN=%s\n' "$TUNNEL_SECRET" | put_text "$ETC/tunnel.secrets" 0644 || true
[ -e "$ETC/vmd.json" ] || printf '{"listen": "127.0.0.1:7340", "tenants": []}\n' > "$ETC/vmd.json"
cat <<EOF | put_text "$ETC/job.env" 0644 || true
DISKY_JOB_BACKEND=local
DISKY_LOCAL_JOBS=$DATA/jobs
DISKY_LOCAL_WORK=$DATA/work
DISKY_LOCAL_ENGINE=docker
DISKY_LOCAL_SLICE=scan.slice
DISKY_LOCAL_SPAWN=systemd-run
EOF
printf 'set -a\n. %s/job.env\nset +a\n' "$ETC" | put_text /etc/profile.d/disky.sh 0644 || true

# 9. The scripts and units.
for f in with-secrets pack-sync vmd-status; do put "$HERE/$f" "$LIB/$f" 0755 || true; done
put "$HERE/ops-agent.yaml" /etc/google-cloud-ops-agent/config.yaml 0644 && systemctl restart google-cloud-ops-agent
units_changed=0
for u in vmd.service disky-tunnel.service disky-pack-sync.service disky-pack-sync.timer; do
  put "$HERE/$u" "$UNITS/$u" 0644 && units_changed=1
done
[ "$units_changed" = 0 ] || systemctl daemon-reload
systemctl enable -q vmd.service disky-tunnel.service disky-pack-sync.timer
# The packs before vmd's first start (vmd maps what vmd.json lists; a failed sync is retried by the timer)
systemctl start disky-pack-sync.service || log "pack sync failed (the timer retries): journalctl -u disky-pack-sync"
if [ "$vmd_changed" = 1 ] || [ "$units_changed" = 1 ]; then systemctl restart vmd.service; else systemctl start vmd.service; fi
systemctl start disky-tunnel.service disky-pack-sync.timer
log "done: $SHA; vmd $(systemctl is-active vmd), tunnel $(systemctl is-active disky-tunnel)"
