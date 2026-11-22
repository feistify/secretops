#!/bin/sh
# Puts the keys from export-keys.sh back, before a restore. Run as the human,
# in a copy of this directory that has no .env and no secrets/ yet:
#
#   sudo COMPOSE="nerdctl compose" sh import-keys.sh FILE   # containerd
#   sudo COMPOSE="docker compose" sh import-keys.sh FILE    # Docker
#
# It restores .env and secrets/ (root-only) and, if the file has it, Caddy's
# CA into the caddy_data volume, so the vault keeps its certificate
# authority. Run it before the first start; then follow Restore in
# docs/VAULT_SELF_HOSTED.md. It prints no key.
set -eu

die() { echo "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
file=${1:?usage: sudo COMPOSE=... sh import-keys.sh FILE}
[ -f "$file" ] || die "$file not found"
[ -f compose.yaml ] || die "run it in the Infisical directory"
for path in .env secrets; do
    [ ! -e "$path" ] || die "$path exists; this is not a fresh directory. Nothing changed."
done
compose=${COMPOSE:-docker compose}
PATH=/usr/local/sbin:/usr/local/bin:$PATH

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar -C "$work" -xzf "$file"
k="$work/infisical-keys"
[ -f "$k/.env" ] && [ -f "$k/secrets/backend.env" ] || die "$file is not an export from export-keys.sh"

cp -p "$k/.env" .env && chmod 644 .env
cp -rp "$k/secrets" secrets
chown -R root:root .env secrets
chmod 700 secrets
chmod 600 secrets/*

if [ -f "$k/caddy-ca/root.key" ]; then
    # Into the volume before Caddy first starts; otherwise it makes a new CA.
    # `create` makes the containers and volumes without starting them, and
    # `cp` works on a created container (`run` needs a terminal with nerdctl).
    mkdir -p "$work/tree/caddy/pki/authorities"
    cp -rp "$k/caddy-ca" "$work/tree/caddy/pki/authorities/local"
    # shellcheck disable=SC2086  # COMPOSE holds several words
    if ! { $compose create caddy && $compose cp "$work/tree/caddy" caddy:/data/; } >"$work/log" 2>&1; then
        rm -rf .env secrets
        echo "Could not put Caddy's CA into its volume; nothing was restored. Output:" >&2
        sed 's/^/  /' "$work/log" >&2
        exit 1
    fi
    echo "Restored .env, secrets/ and Caddy's CA; the broker and browsers keep trusting the vault."
else
    echo "Restored .env and secrets/. The file has no Caddy CA; Caddy will create a new one at the first start."
fi
