#!/bin/sh
# Writes the configuration for compose.yaml. Run once, as the human, in this
# directory, with sudo so that the key files belong to root:
#
#   sudo sh make-env.sh HOST [HTTPS_PORT]
#
# HOST is the DNS name or IPv4 address clients use to reach Infisical, for
# example vault.example.lan or 192.168.1.20. HTTPS_PORT defaults to 8443.
#
# It writes .env (HOST and port only) and secrets/ (mode 700, root-only):
# backend.env with ENCRYPTION_KEY, AUTH_SECRET and the database and Redis
# URIs, db_password, and redis.conf. It prints no key or password and never
# overwrites.
set -eu

host=${1:?usage: sudo sh make-env.sh HOST [HTTPS_PORT]}
port=${2:-8443}
case $host in
    ''|*[!A-Za-z0-9.-]*) echo "invalid HOST: use a DNS name or an IPv4 address" >&2; exit 1 ;;
esac
case $port in
    ''|*[!0-9]*) echo "invalid HTTPS_PORT" >&2; exit 1 ;;
esac
[ "$(id -u)" = 0 ] || { echo "run with sudo, so that secrets/ belongs to root" >&2; exit 1; }
for path in .env secrets secrets.tmp; do
    if [ -e "$path" ]; then
        echo "$path exists; not overwritten" >&2
        exit 1
    fi
done
# Container uids: Infisical 61001, Redis 61002 (compose.yaml), Postgres 70
# (the image's). A host account with the same uid could read their keys.
has_account() {
    # Synology DSM has no getent; its local accounts are in /etc/passwd
    # (directory-service accounts are not checked there).
    if command -v getent >/dev/null; then
        getent passwd "$1" >/dev/null
    else
        awk -F: -v uid="$1" '$3 == uid { found = 1 } END { exit !found }' /etc/passwd
    fi
}
for uid in 61001 61002 70; do
    status=0
    has_account "$uid" || status=$?
    if [ "$status" -gt 2 ] || { [ "$status" = 2 ] && ! command -v getent >/dev/null; }; then
        echo "could not check whether uid $uid belongs to a host account" >&2
        exit 1
    fi
    if [ "$status" = 0 ]; then
        echo "uid $uid belongs to a host account, which could read the vault's keys or passwords;" >&2
        echo "use another uid in compose.yaml (61001, 61002) or a machine without that account (70)" >&2
        exit 1
    fi
done
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

# Generate first, so that a failure stops before anything is written.
db_password=$(openssl rand -hex 24)
redis_password=$(openssl rand -hex 24)
encryption_key=$(openssl rand -hex 16)
auth_secret=$(openssl rand -base64 32)

umask 077
mkdir secrets.tmp
printf '%s\n' "$db_password" > secrets.tmp/db_password
printf 'requirepass %s\n' "$redis_password" > secrets.tmp/redis.conf
cat > secrets.tmp/backend.env <<ENV
# Written by make-env.sh. Keep a copy of secrets/ offline: without
# ENCRYPTION_KEY a database backup cannot be decrypted. Never commit it.
ENCRYPTION_KEY=$encryption_key
AUTH_SECRET=$auth_secret
DB_CONNECTION_URI=postgres://infisical:$db_password@db:5432/infisical
REDIS_URL=redis://:$redis_password@redis:6379
ENV
mv secrets.tmp secrets

umask 022
printf 'INFISICAL_HOST=%s\nHTTPS_PORT=%s\n' "$host" "$port" > .env
echo "Wrote .env and secrets/ (root-only). Copy secrets/ to offline storage now."
