#!/bin/sh
# Dumps the Infisical database from the cluster into DIR, readable by you
# only. Run as the human, for example from cron (with KUBECONFIG set there):
#
#   sh backup.sh DIR [NAMESPACE]       # default namespace: infisical
#
# The dump is encrypted data: restoring it needs ENCRYPTION_KEY from your
# offline copy of secrets/. Keep backups apart from that copy.
set -eu

dir=${1:?usage: sh backup.sh DIR [NAMESPACE]}
ns=${2:-infisical}
PATH=$PATH:/usr/local/bin
[ -d "$dir" ] || { echo "$dir is not a directory" >&2; exit 1; }
umask 077
out="$dir/infisical-$(date +%Y%m%d-%H%M%S).sql.gz"
trap 'rm -f "$out.tmp"' EXIT
trap 'exit 1' HUP INT TERM
# Compressed inside the pod, so no plain dump lands on this disk.
kubectl -n "$ns" exec infisical-db-0 -- sh -c \
    'set -o pipefail; pg_dump --no-owner --no-privileges -U infisical -d infisical | gzip' > "$out.tmp"
mv "$out.tmp" "$out"
echo "Wrote $out"
