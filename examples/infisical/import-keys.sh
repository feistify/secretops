#!/bin/sh
# Puts the keys from export-keys.sh back, before a restore. Run as the human,
# in a copy of this directory that has no .env and no secrets/ yet:
#
#   sudo COMPOSE="nerdctl compose" sh import-keys.sh FILE   # containerd
#   sudo COMPOSE="docker compose" sh import-keys.sh FILE    # Docker
#
# It restores .env and secrets/ (root-only) and, if the file has it, Caddy's
# root CA into the caddy_data volume, so the vault keeps its certificate
# authority. Run it before the first start; then follow Restore in
# docs/VAULT_SELF_HOSTED.md. It prints no key. On any failure it removes what
# it restored, so running it again is safe.
set -eu

die() { echo "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
file=${1:?usage: sudo COMPOSE=... sh import-keys.sh FILE}
[ -f "$file" ] || die "$file not found"
[ -f compose.yaml ] || die "run it in the Infisical directory"
for path in .env secrets; do
    [ ! -e "$path" ] && [ ! -L "$path" ] || die "$path exists; this is not a fresh directory. Nothing changed."
done
compose=${COMPOSE:-docker compose}
PATH=/usr/local/sbin:/usr/local/bin:$PATH

umask 077
work=$(mktemp -d)
finished=no
cleanup() {
    rm -rf "$work"
    if [ "$finished" != yes ]; then
        rm -rf .env secrets
    fi
}
trap cleanup EXIT
trap 'exit 1' INT TERM
tar --no-same-owner -C "$work" -xzf "$file" || die "$file is not a readable export from export-keys.sh"
# Only plain files and directories: no links or devices from the archive.
[ -z "$(find "$work" ! -type f ! -type d)" ] || die "$file holds links or special files; refusing it"
k="$work/infisical-keys"
[ -f "$k/.env" ] && [ -f "$k/secrets/backend.env" ] || die "$file is not an export from export-keys.sh"

cp "$k/.env" .env
chmod 644 .env
mkdir -m 700 secrets
cp "$k/secrets/"* secrets/
chmod 600 secrets/*
chown -R root:root .env secrets

if [ -f "$k/caddy-ca/root.crt" ] && [ -f "$k/caddy-ca/root.key" ]; then
    # Into the volume before Caddy first starts; otherwise it makes a new CA.
    # Only the root goes back: Caddy makes a new intermediate from it.
    # `create` makes the containers and volumes without starting them;
    # `cp` works on a created container (`run` needs a terminal with nerdctl).
    mkdir -p "$work/tree/caddy/pki/authorities/local"
    cp "$k/caddy-ca/root.crt" "$k/caddy-ca/root.key" "$work/tree/caddy/pki/authorities/local/"
    # shellcheck disable=SC2086  # COMPOSE holds several words
    if ! { $compose create caddy && $compose cp "$work/tree/caddy" caddy:/data/; } >"$work/log" 2>&1; then
        echo "Could not put Caddy's CA into its volume; nothing was restored. Output:" >&2
        sed 's/^/  /' "$work/log" >&2
        exit 1
    fi
    msg="Restored .env, secrets/ and Caddy's root CA; the broker and browsers keep trusting the vault."
else
    msg="Restored .env and secrets/. The file has no Caddy CA; Caddy will create a new one at the first start."
fi
finished=yes
echo "$msg"
