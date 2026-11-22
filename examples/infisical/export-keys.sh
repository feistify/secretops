#!/bin/sh
# Exports what you must keep offline into one file for your password manager.
# Run as the human, in this directory, once Infisical has started:
#
#   sudo COMPOSE="nerdctl compose" sh export-keys.sh [DIR]   # containerd
#   sudo COMPOSE="docker compose" sh export-keys.sh [DIR]    # Docker
#
# It writes infisical-keys-DATE.tar.gz into DIR (default: your home),
# readable only by you. It holds .env, secrets/ (ENCRYPTION_KEY, AUTH_SECRET,
# passwords) and Caddy's root CA (certificate and key), so that a restored
# vault keeps its TLS certificate authority. Attach it to an entry in your
# password manager, then delete it. It prints no key.
set -eu

die() { echo "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
owner=${SUDO_USER:-}
[ -n "$owner" ] && [ "$owner" != root ] || die "run with sudo from your own account, not as root"
[ -d secrets ] && [ -f .env ] || die "run it in the Infisical directory, after make-env.sh"
home=$(awk -F: -v u="$owner" '$1 == u { print $6 }' /etc/passwd)
[ -n "$home" ] || home=$(getent passwd "$owner" 2>/dev/null | cut -d: -f6) || true
dest=${1:-$home}
[ -n "$dest" ] || die "no home directory found for $owner; give DIR"
[ -d "$dest" ] && [ ! -L "$dest" ] || die "$dest is not a directory"
# Root writes the archive there: refuse a directory others can change.
dest_owner=$(stat -c %U "$dest")
[ "$dest_owner" = "$owner" ] || [ "$dest_owner" = root ] || die "$dest belongs to $dest_owner; give a directory of yours"
[ -z "$(find "$dest" -maxdepth 0 -perm /022)" ] || die "$dest is writable by others; give a private directory"
compose=${COMPOSE:-docker compose}
PATH=/usr/local/sbin:/usr/local/bin:$PATH

umask 077
work=$(mktemp -d)
tmp=$(mktemp "$dest/.infisical-keys.XXXXXX")
trap 'rm -rf "$work"; rm -f "$tmp"' EXIT
mkdir "$work/infisical-keys" "$work/ca"
cp -p .env "$work/infisical-keys/"
cp -rp secrets "$work/infisical-keys/"
# Only the root: Caddy makes a new 7-day intermediate from it after a restore.
ca_ok=no
# shellcheck disable=SC2086  # COMPOSE holds several words
if $compose cp caddy:/data/caddy/pki/authorities/local "$work/ca/local" >/dev/null 2>&1 \
    && [ -f "$work/ca/local/root.crt" ] && [ -f "$work/ca/local/root.key" ]; then
    mkdir "$work/infisical-keys/caddy-ca"
    cp -p "$work/ca/local/root.crt" "$work/ca/local/root.key" "$work/infisical-keys/caddy-ca/"
    ca_ok=yes
fi

file="$dest/infisical-keys-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -C "$work" -czf "$tmp" infisical-keys
chown "$owner" "$tmp"
mv "$tmp" "$file"
if [ "$ca_ok" = yes ]; then
    echo "Wrote $file (only $owner can read it): .env, secrets/ and Caddy's root CA."
    echo "Attach it to an entry in your password manager, then delete it: rm $file"
else
    echo "WARNING: wrote $file with .env and secrets/ but WITHOUT Caddy's CA" >&2
    echo "(is the caddy service running?). A restore from it will create a new CA." >&2
    exit 1
fi
