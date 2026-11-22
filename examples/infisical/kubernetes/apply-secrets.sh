#!/bin/sh
# Creates the namespace, the Kubernetes Secrets and a ConfigMap from secrets/ (written by
# make-secrets.sh, or restored from your offline copy). Run as the human,
# with a kubectl that can create the namespace, Secrets and ConfigMaps:
#
#   sh apply-secrets.sh [NAMESPACE]        # default: infisical
#
# Values are read from the files by kubectl, never passed as arguments, and
# not printed. Existing Secrets are not replaced.
set -eu

ns=${1:-infisical}
[ -d secrets ] || { echo "secrets/ not found; run make-secrets.sh first or restore your copy" >&2; exit 1; }
for f in backend.env db_password redis.conf settings.env ca.crt ca.key; do
    [ -f "secrets/$f" ] || { echo "secrets/$f is missing" >&2; exit 1; }
done
for name in infisical-secrets infisical-db infisical-redis infisical-ca infisical-settings; do
    if kubectl -n "$ns" get secret "$name" >/dev/null 2>&1 || kubectl -n "$ns" get configmap "$name" >/dev/null 2>&1; then
        echo "$name exists in namespace $ns; not replaced." >&2
        echo "A fresh install that stopped half-way: kubectl delete namespace $ns, then run this again." >&2
        exit 1
    fi
done
trap 'echo "Stopped half-way. For a fresh install: kubectl delete namespace $ns, then run this again." >&2' EXIT
kubectl get namespace "$ns" >/dev/null 2>&1 || kubectl create namespace "$ns"
kubectl -n "$ns" create secret generic infisical-secrets --from-env-file=secrets/backend.env
kubectl -n "$ns" create secret generic infisical-db --from-file=password=secrets/db_password
kubectl -n "$ns" create secret generic infisical-redis --from-file=redis.conf=secrets/redis.conf
kubectl -n "$ns" create secret generic infisical-ca --from-file=root.crt=secrets/ca.crt --from-file=root.key=secrets/ca.key
kubectl -n "$ns" create configmap infisical-settings --from-env-file=secrets/settings.env
trap - EXIT
echo "Next: kubectl apply -n $ns -f vault.yaml, then the Helm release (docs/VAULT_KUBERNETES.md)."
