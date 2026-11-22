#!/bin/sh
# Exports what you must keep offline into one file for your password manager.
# Run as the human, in this directory, once Infisical has started:
#
#   sudo COMPOSE="nerdctl compose" sh export-keys.sh [DIR]   # containerd
#   sudo COMPOSE="docker compose" sh export-keys.sh [DIR]    # Docker
#
# It writes infisical-keys-DATE.tar.gz into DIR (default: your home),
# readable only by you. It holds .env, secrets/ (ENCRYPTION_KEY, AUTH_SECRET,
# passwords) and Caddy's local CA (root certificate and key), so that a
# restored vault keeps its TLS certificate authority. Attach it to an entry
# in your password manager, then delete it. It prints no key.
set -eu

die() { echo "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
owner=${SUDO_USER:-}
[ -n "$owner" ] && [ "$owner" != root ] || die "run with sudo from your own account, not as root"
[ -d secrets ] && [ -f .env ] || die "run it in the Infisical directory, after make-env.sh"
home=$(awk -F: -v u="$owner" '$1 == u { print $6 }' /etc/passwd)
[ -n "$home" ] || home=$(getent passwd "$owner" | cut -d: -f6)
dest=${1:-$home}
[ -d "$dest" ] || die "$dest is not a directory"
compose=${COMPOSE:-docker compose}
PATH=/usr/local/sbin:/usr/local/bin:$PATH

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/infisical-keys"
cp -p .env "$work/infisical-keys/"
cp -rp secrets "$work/infisical-keys/"
# shellcheck disable=SC2086  # COMPOSE holds several words
if $compose cp caddy:/data/caddy/pki/authorities/local "$work/infisical-keys/caddy-ca" >/dev/null 2>&1 \
    && [ -f "$work/infisical-keys/caddy-ca/root.key" ]; then
    ca="and Caddy's CA"
else
    rm -rf "$work/infisical-keys/caddy-ca"
    ca="but not Caddy's CA (is the caddy service running?); a restore will create a new CA"
fi

file="$dest/infisical-keys-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -C "$work" -czf "$file.tmp" infisical-keys
chown "$owner" "$file.tmp"
mv "$file.tmp" "$file"
echo "Wrote $file (only $owner can read it): .env, secrets/ $ca."
echo "Attach it to an entry in your password manager, then delete it: rm $file"
