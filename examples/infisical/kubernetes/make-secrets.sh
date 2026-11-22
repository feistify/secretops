#!/bin/sh
# Writes the keys, passwords and TLS root CA for the Kubernetes variant into
# secrets/ (mode 700, files 600, owned by you). Run once, as the human, in
# this directory, without sudo:
#
#   sh make-secrets.sh HOST [NODE_PORT]
#
# HOST is the DNS name or IPv4 address clients use to reach the vault (a
# node's address). NODE_PORT defaults to 30843; another one must also be set
# as nodePort in vault.yaml. Keep HOST: it is in the certificate and SITE_URL.
# It prints no key or password and never overwrites. Keep a copy of secrets/
# offline: without ENCRYPTION_KEY no database backup can be decrypted, and
# with ca.key anyone can make certificates the broker trusts.
# apply-secrets.sh puts them into the cluster.
set -eu

host=${1:?usage: sh make-secrets.sh HOST [NODE_PORT]}
port=${2:-30843}
case $host in
    ''|*[!A-Za-z0-9.-]*) echo "invalid HOST: use a DNS name or an IPv4 address" >&2; exit 1 ;;
esac
case $port in
    ''|*[!0-9]*|??????*) echo "invalid NODE_PORT" >&2; exit 1 ;;
esac
if [ "$port" -lt 30000 ] || [ "$port" -gt 32767 ]; then
    echo "NODE_PORT must be in 30000-32767 (Kubernetes' NodePort range)" >&2; exit 1
fi
for path in secrets secrets.tmp; do
    [ -e "$path" ] && { echo "$path exists; not overwritten" >&2; exit 1; }
done
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

db_password=$(openssl rand -hex 24)
redis_password=$(openssl rand -hex 24)
encryption_key=$(openssl rand -hex 16)
auth_secret=$(openssl rand -base64 32)

umask 077
mkdir secrets.tmp
trap 'rm -rf secrets.tmp' EXIT
trap 'exit 1' HUP INT TERM
printf '%s' "$db_password" > secrets.tmp/db_password
printf 'requirepass %s\n' "$redis_password" > secrets.tmp/redis.conf
cat > secrets.tmp/backend.env <<ENV
ENCRYPTION_KEY=$encryption_key
AUTH_SECRET=$auth_secret
SITE_URL=https://$host:$port
DB_CONNECTION_URI=postgres://infisical:$db_password@infisical-db:5432/infisical
REDIS_URL=redis://:$redis_password@infisical-redis:6379
ENV
printf 'INFISICAL_HOST=%s\n' "$host" > secrets.tmp/settings.env
# The TLS root CA, ten years. Caddy signs short-lived certificates with an
# intermediate it makes from this root; the broker and browsers trust ca.crt.
openssl req -x509 -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
    -subj "/CN=Infisical local CA ($host)" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout secrets.tmp/ca.key -out secrets.tmp/ca.crt
mv secrets.tmp secrets
trap - EXIT
umask 022
cp secrets/ca.crt infisical-root.crt
chmod 644 infisical-root.crt
echo "Wrote secrets/ (only you can read it) and infisical-root.crt (public)."
echo "Keep a copy of secrets/ offline, in your password manager, not only here:"
echo "  backend.env   ENCRYPTION_KEY: without it no database backup can be decrypted"
echo "  ca.key        the TLS root key: with it anyone can make certificates the broker trusts"
echo "Next: sh apply-secrets.sh"
