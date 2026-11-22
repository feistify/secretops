# Self-hosted Infisical on Kubernetes

The Kubernetes variant of [VAULT_SELF_HOSTED.md](VAULT_SELF_HOSTED.md), for a cluster you administer, including a single node. TLS, connecting the broker and `bootstrap-broker.py` work the same way; this page covers only what differs. Files: [`examples/infisical/kubernetes/`](../examples/infisical/kubernetes/).

Tested: k3s v1.37.2 on one Ubuntu 22.04 node with Helm 3.19, Infisical's chart `infisical-standalone` 1.12.4 (Infisical v0.167.14), secretops 0.1.14 on the same node ([record](validation/self-hosted-infisical.md#kubernetes-k3s)).

## What you get

- Infisical from Infisical's [Helm chart](https://infisical.com/docs/self-hosting/deployment-options/kubernetes-helm), with only Infisical enabled. The chart's own Postgres and Redis would put their passwords into the Deployment in plain text and use unmaintained images, and its bundled ingress controller is old; so they are off.
- Postgres, Redis and Caddy from [`vault.yaml`](../examples/infisical/kubernetes/vault.yaml), with the same pinned images as the compose variant. Passwords and keys are in Kubernetes Secrets, read from files.
- TLS from Caddy with a root CA that `make-secrets.sh` makes and you keep offline, so a restore into a new cluster keeps the certificate the broker trusts. Reached at `https://HOST:30843` (a NodePort).
- Processes run as uids 61001 (Infisical), 61002 (Redis), 61003 (Caddy) and 70 (Postgres). No host account on the nodes should have them.

Anyone who can read Secrets in the namespace, exec into or create pods there, read the cluster's datastore or its snapshots (Secrets are not encrypted there unless you turned that on), or is root on a node, can read the vault's keys. A NetworkPolicy in `vault.yaml` lets only Infisical reach Postgres and Redis where the network plugin enforces it.

## Before you start

On the node, run [`check-node.sh`](../examples/infisical/check-node.sh) (read-only). You need `kubectl` with rights to create a namespace and Secrets, Helm 3, a default storage class, and no host account with the uids above. The chart repository is on `dl.cloudsmith.io`.

## Set up

Work in a copy of `examples/infisical/kubernetes/` on a machine with `kubectl` for the cluster, in a directory only you can read.

```sh
# Human
sh make-secrets.sh HOST                 # HOST: a node's DNS name or address
sh apply-secrets.sh                     # namespace infisical, Secrets, settings
kubectl apply -n infisical -f vault.yaml
helm repo add infisical-helm-charts https://dl.cloudsmith.io/public/infisical/helm-charts/helm/charts/
helm upgrade --install infisical infisical-helm-charts/infisical-standalone --version 1.12.4 -n infisical -f values.yaml
kubectl -n infisical rollout status deploy/infisical-backend
curl -s --cacert infisical-root.crt https://HOST:30843/api/status    # "message":"Ok"
```

`make-secrets.sh` writes `secrets/` (keys, passwords, the root CA; only you can read it) and the public `infisical-root.crt`, prints no key and never overwrites. **Copy `secrets/` to your password manager now**: it is everything [Keep offline](VAULT_SELF_HOSTED.md#keep-offline) lists, including the CA key. Another port: pass it as the second argument and change `nodePort` in `vault.yaml`.

Then [connect the broker](VAULT_SELF_HOSTED.md#connect-the-broker) with `infisical-root.crt` and `--infisical-domain https://HOST:30843`; for a new vault, `bootstrap-broker.py` creates the admin, project, identity and token.

## Back up

```sh
# Human
sh backup.sh DIR            # a pg_dump, readable by you only
```

Schedule it with cron, with `KUBECONFIG` set in the crontab (on k3s, `/etc/rancher/k3s/k3s.yaml` is root's). The dump is compressed inside the pod, so no plain copy lands on disk. A dump is useless without `ENCRYPTION_KEY` from `secrets/`; keep the two apart.

## Restore

Into an empty namespace, from your copy of `secrets/` and a dump. To start over in the same cluster, first `helm -n infisical uninstall infisical` and `kubectl delete namespace infisical` (this deletes the data volumes). Keep HOST: it is in the certificate and in `SITE_URL` in `secrets/backend.env`.

```sh
# Human, in the directory with secrets/
sh apply-secrets.sh
kubectl apply -n infisical -f vault.yaml
kubectl -n infisical rollout status statefulset/infisical-db
gzip -dc DUMP.sql.gz | kubectl -n infisical exec -i infisical-db-0 -- psql -q -v ON_ERROR_STOP=1 -U infisical -d infisical
helm repo add infisical-helm-charts https://dl.cloudsmith.io/public/infisical/helm-charts/helm/charts/   # on a new machine
helm upgrade --install infisical infisical-helm-charts/infisical-standalone --version 1.12.4 -n infisical -f values.yaml
kubectl -n infisical rollout status deploy/infisical-backend
curl -s --cacert infisical-root.crt https://HOST:30843/api/status
```

Restore the database before Infisical starts. The CA comes from `secrets/`, so the broker and browsers need no change.

## Upgrade

Back up first. Run `helm repo update`, pick the chart version whose app version you want (`helm search repo infisical-helm-charts/infisical-standalone --versions`) and read the chart's CHANGELOG, since value names can change. Set the same tag in `values.yaml` (`infisical.image.tag` and `infisical.extraInitContainers[0].image`) and run the `helm upgrade` line with the new `--version`. Change Postgres, Redis or Caddy images in `vault.yaml` one at a time; a new Postgres major version needs a dump and restore.
