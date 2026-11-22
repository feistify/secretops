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
# backend.env with ENCRYPTION_KEY, AUTH_SECRET and the database URI, and
# db_password. It prints no key or password and never overwrites.
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
for path in .env secrets; do
    if [ -e "$path" ]; then
        echo "$path exists; not overwritten" >&2
        exit 1
    fi
done
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

umask 077
mkdir secrets.tmp
password=$(openssl rand -hex 24)
printf '%s\n' "$password" > secrets.tmp/db_password
cat > secrets.tmp/backend.env <<ENV
# Written by make-env.sh. Keep a copy of secrets/ offline: without
# ENCRYPTION_KEY a database backup cannot be decrypted. Never commit it.
ENCRYPTION_KEY=$(openssl rand -hex 16)
AUTH_SECRET=$(openssl rand -base64 32)
DB_CONNECTION_URI=postgres://infisical:$password@db:5432/infisical
ENV
mv secrets.tmp secrets

umask 022
printf 'INFISICAL_HOST=%s\nHTTPS_PORT=%s\n' "$host" "$port" > .env
echo "Wrote .env and secrets/ (root-only). Copy secrets/ to offline storage now."
