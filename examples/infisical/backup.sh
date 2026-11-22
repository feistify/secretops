#!/bin/bash
# Dumps the Infisical database into DIR as a compressed SQL file (mode 600).
# Run as root, from anywhere, also from a root cron job:
#
#   sudo COMPOSE="nerdctl compose" bash /opt/infisical/backup.sh DIR   # containerd
#   sudo COMPOSE="docker compose" bash /opt/infisical/backup.sh DIR    # Docker
#
# The dump holds encrypted data; it is useful only together with
# secrets/backend.env (ENCRYPTION_KEY), which you keep separately and offline.
set -euo pipefail

dir=${1:?usage: sudo COMPOSE=... bash backup.sh DIR}
compose=${COMPOSE:-docker compose}
[[ -d $dir ]] || { echo "$dir is not a directory" >&2; exit 1; }
dir=$(cd -- "$dir" && pwd)
# cron runs with a short PATH and in root's home: find nerdctl and compose.yaml.
PATH=/usr/local/sbin:/usr/local/bin:$PATH
cd -- "$(dirname -- "$0")"

umask 077
file="$dir/infisical-$(date +%Y%m%d-%H%M%S).sql.gz"
trap 'rm -f -- "$file.tmp"' EXIT
# shellcheck disable=SC2086  # COMPOSE holds several words
$compose exec -T db pg_dump --no-owner --no-privileges -U infisical -d infisical | gzip > "$file.tmp"
mv -- "$file.tmp" "$file"
echo "$file"
