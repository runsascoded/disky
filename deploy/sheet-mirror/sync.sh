#!/usr/bin/env bash
# The sheet mirror's Cloud Run job entrypoint: for each mirror in the
# deployment's config, export its source from the live site and push it into
# its named tab, key-aware.
#
#   1. `dt-cloud export <source>` → the source's CSV (fixed column contract),
#      read from the site's API with the read-only grant token.
#   2. `dt-cloud sheet-push -c -k <key> -w <tab> [-D <footer>]` → cell-level,
#      key-aware diff into the one named tab, so Version History shows one-row
#      changes; a no-op run writes nothing.
#
# Config: `$SHEET_MIRROR_CONFIG_B64` (the deployment's sheet-mirror.yml, set by
# deploy.sh) or a path as $1 (local runs). Runs AS the job's SA: ambient ADC
# covers the Sheets write (the SA is Editor on each sheet). GCS_USAGE_TOKEN is
# injected from Secret Manager. No `set -x`: the token must never reach Cloud
# Logging. One mirror failing doesn't stop the others; the job exits non-zero
# if any failed.
set -euo pipefail

: "${GCS_USAGE_TOKEN:?GCS_USAGE_TOKEN must be set (Secret Manager)}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if [[ $# -ge 1 ]]; then
  cfg=$1
else
  : "${SHEET_MIRROR_CONFIG_B64:?SHEET_MIRROR_CONFIG_B64 must be set (deploy.sh), or pass a config path}"
  cfg=$work/sheet-mirror.yml
  printf '%s' "$SHEET_MIRROR_CONFIG_B64" | base64 -d > "$cfg"
fi

# One line per mirror: shell-quoted `source= site= subdir= sheet= tab= key=
# footer= executor=` assignments (every value `shlex.quote`d by the helper).
dt-cloud sheet-mirror plan "$cfg" > "$work/plan"

n=0
failed=0
while IFS= read -r line <&3; do
  source='' site='' subdir='' sheet='' tab='' key='' footer='' executor='' unit=''
  eval "$line"
  n=$((n + 1))
  csv=$work/$n-$source.csv
  export_args=(-u "$site" -o "$csv")
  [[ -n $subdir ]] && export_args+=(-s "$subdir")
  [[ -n $executor ]] && export_args+=(-e "$executor")
  [[ -n $unit ]] && export_args+=(-U "$unit")
  push_args=(-c -k "$key" -w "$tab")
  [[ -n $footer ]] && push_args+=(-D "$footer")
  if dt-cloud export "$source" "${export_args[@]}" && dt-cloud sheet-push "${push_args[@]}" "$sheet" "$csv"; then
    echo "✓ $source → '$tab' @ $(date -u '+%Y-%m-%d %H:%M UTC')" >&2
  else
    echo "✗ $source → '$tab' failed" >&2
    failed=$((failed + 1))
  fi
done 3< "$work/plan"

if ((failed)); then
  echo "$failed of $n mirror(s) failed" >&2
  exit 1
fi
echo "$n mirror(s) synced" >&2
