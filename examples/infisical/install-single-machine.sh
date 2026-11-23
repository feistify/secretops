#!/bin/bash
# install-single-machine.sh: the human's part of a single-machine install.
#
# Self-hosted Infisical (this directory's compose.yaml) and the secretops
# broker on the same Linux host, in one run instead of many pasted blocks:
#
#   sudo bash install-single-machine.sh --host HOST --create-agent-user agent
#
# Run it from a copy of examples/infisical/ (a repository checkout or the
# release's source archive), in your own terminal, with sudo. It does, and
# skips what is already done, so it can be run again after a stop:
#
#   1. checks the host: Ubuntu 22.04/24.04 or Debian 12, a container tool
#      (nerdctl or docker compose) already installed, free ports
#   2. Python 3.11+ for the broker; on Ubuntu 22.04 python3.12 from the
#      deadsnakes PPA
#   3. the vault in --dir (default /opt/infisical): make-env.sh, compose up,
#      waits for the API, copies Caddy's root certificate to
#      /etc/secretops-ca/infisical-root.pem
#   4. the Infisical CLI from Infisical's apt repository (signing key checked
#      against a pinned fingerprint), version pinned and held
#   5. secretops from the release bundle (SHA-256 checked against the
#      release's SHA256SUMS), with the agent account
#   6. bootstrap-broker.py: asks for the first admin's email and password,
#      creates project, machine identity and token, ends with the vault check
#
# It never installs or changes a container runtime: on a Kubernetes node the
# nerdctl full bundle would replace the node's containerd (docs/TRIAL.md).
# It prints no key, password or token, and asks before it changes anything.
# Afterwards it prints the next steps: export the keys, the first check,
# backups. Guide: docs/VAULT_SELF_HOSTED.md.
set -Eeuo pipefail
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SECRETOPS_VERSION=0.1.15
INFISICAL_CLI_VERSION=0.45.14
# Primary key of "Infisical code artifacts", which signs artifacts-cli.infisical.com.
INFISICAL_KEY_FINGERPRINT=698CE22DCBD9C67562F3C4749CC397EDC268078E
INFISICAL_REPO=https://artifacts-cli.infisical.com
KEYRING=/usr/share/keyrings/infisical-archive-keyring.gpg
CA_DIR=/etc/secretops-ca
CA_FILE=$CA_DIR/infisical-root.pem
BROKER_ROOT=/opt/secretops
VAULT_FILES="compose.yaml Caddyfile make-env.sh backup.sh export-keys.sh import-keys.sh"
API_WAIT_SECONDS=600

host='' port=8443 dir=/opt/infisical agent_user='' create_agent='' assume_yes=0
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage() {
    cat <<'EOF'
usage:
  sudo bash install-single-machine.sh --host HOST (--create-agent-user NAME | --agent-user USER)
                                      [--port PORT] [--dir DIR] [--yes]

  --host HOST              DNS name or IPv4 address the broker and your browser use for the vault
  --port PORT              HTTPS port of the vault (default 8443)
  --dir DIR                where the vault's files go (default /opt/infisical); its name
                           names the Compose project and its volumes
  --create-agent-user NAME create a separate account without sudo for the coding agent
  --agent-user USER        an existing account the coding agent runs as
  --yes                    do not ask before changing the system
EOF
}

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
ok() { printf 'OK   %s\n' "$*"; }
skip() { printf 'SKIP %s\n' "$*"; }
die() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --host) host=${2:?--host needs a value}; shift 2 ;;
        --port) port=${2:?--port needs a value}; shift 2 ;;
        --dir) dir=${2:?--dir needs a value}; shift 2 ;;
        --agent-user) agent_user=${2:?--agent-user needs a value}; shift 2 ;;
        --create-agent-user) create_agent=${2:?--create-agent-user needs a value}; shift 2 ;;
        --yes) assume_yes=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

# --- 1. checks: nothing changes before the confirmation -------------------

[[ -n $host ]] || { usage >&2; die "--host is required"; }
[[ $host =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "--host must be a DNS name or an IPv4 address"
if ! [[ $port =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    die "--port must be a number from 1 to 65535"
fi
[[ $dir == /* && $dir != / && $dir != *[[:space:]]* ]] || die "--dir must be an absolute path without spaces"
if [[ -n $agent_user && -n $create_agent ]] || [[ -z $agent_user && -z $create_agent ]]; then
    die "give exactly one of --create-agent-user NAME or --agent-user USER"
fi
[[ $(id -u) == 0 ]] || die "run with sudo from your own terminal"
human=${SUDO_USER:-}
[[ -n $human && $human != root ]] || die "run with sudo from your own (non-root) account, not as root directly"
for file in $VAULT_FILES bootstrap-broker.py; do
    [[ -f $here/$file ]] || die "$file is missing next to this script; run it from a copy of examples/infisical/"
done
for tool in curl tar sha256sum gpg awk ss openssl cmp; do
    command -v "$tool" >/dev/null || die "$tool is required (apt install curl tar coreutils gnupg gawk iproute2 openssl diffutils)"
done

step "1. Host"
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04|ubuntu:24.04|debian:12) ok "$PRETTY_NAME" ;;
    *) die "${PRETTY_NAME:-this system} is not supported here; see docs/INSTALL.md" ;;
esac
command -v apt-get >/dev/null || die "apt-get is required"

if command -v nerdctl >/dev/null; then
    compose="nerdctl compose"
elif command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    compose="docker compose"
else
    die "no container tool: install nerdctl (containerd) or Docker with the Compose plugin first; see 'Choose the container tool' in docs/VAULT_SELF_HOSTED.md. This script never installs one."
fi
ok "container tool: $compose"
if systemctl is-active --quiet kubelet 2>/dev/null; then
    say "NOTE this is a Kubernetes node: the vault runs beside the cluster in containerd's default namespace, and whoever has root here can approve plans and read the vault's keys (docs/SECURITY_MODEL.md, 'Who can act as the broker')."
fi

env_matches() { [[ $(cat "$dir/.env") == "INFISICAL_HOST=$host"$'\n'"HTTPS_PORT=$port" ]]; }
if [[ -f $dir/.env ]] && ! env_matches; then
    die "$dir/.env was written for another host or port; use those, or another --dir"
fi
url=https://$host:$port
vault_running() { curl -fsk -m 5 "$url/api/status" 2>/dev/null | grep -q '"message":"Ok"'; }
if ! vault_running && ss -Htln "sport = :$port" | grep -q .; then
    die "port $port is in use; choose another --port"
fi

python=''
for candidate in /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3; do
    if [[ -x $candidate ]] && "$candidate" -c 'import sys, ensurepip, venv; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        python=$candidate
        break
    fi
done

# What is still to do, for the confirmation.
todo=()  # what is still to do
[[ -n $python ]] || todo+=("install Python 3.12 (Ubuntu 22.04: deadsnakes PPA)")
[[ -f $dir/compose.yaml ]] || todo+=("copy the vault files to $dir (root-owned)")
[[ -f $dir/.env ]] || todo+=("write the vault's keys and passwords to $dir/secrets (root only; make-env.sh)")
vault_running || todo+=("start the vault: $compose up -d in $dir (downloads images)")
[[ -f $CA_FILE ]] || todo+=("copy the vault's root certificate to $CA_FILE")
[[ -x /usr/bin/infisical ]] || todo+=("install the Infisical CLI $INFISICAL_CLI_VERSION from $INFISICAL_REPO (apt; held)")
[[ -x $BROKER_ROOT/bin/secretops ]] || todo+=("install secretops $SECRETOPS_VERSION from the release bundle, agent account ${create_agent:-$agent_user}")
has_token() { grep -q '^INFISICAL_TOKEN=' "$BROKER_ROOT/broker.env" 2>/dev/null; }
has_token || todo+=("connect the broker to a new vault: bootstrap-broker.py asks for the first admin's email and password")

step "Plan"
if (( ${#todo[@]} == 0 )); then say " - nothing to change; only checks"; fi
for item in "${todo[@]}"; do say " - $item"; done
if (( ${#todo[@]} > 0 && ! assume_yes )); then
    printf 'Continue? [y/N] ' > /dev/tty
    read -r answer < /dev/tty || answer=''
    [[ $answer == [yY] || $answer == [yY][eE][sS] ]] || die "stopped; nothing was changed"
fi

# --- 2. Python ------------------------------------------------------------

step "2. Python for the broker"
if [[ -n $python ]]; then
    ok "$python ($("$python" -V 2>&1))"
else
    [[ $ID == ubuntu ]] || die "no Python 3.11 or newer with venv; install one and run again"
    apt-get update -q >/dev/null
    apt-get install -y -q software-properties-common >/dev/null
    add-apt-repository -y ppa:deadsnakes/ppa >/dev/null || die "could not add the deadsnakes PPA (network or proxy?); install Python 3.12 and run again"
    apt-get install -y -q python3.12 python3.12-venv >/dev/null || die "could not install python3.12"
    python=/usr/bin/python3.12
    ok "$python ($("$python" -V 2>&1))"
fi

# --- 3. vault -------------------------------------------------------------

step "3. Vault in $dir"
if [[ -f $dir/compose.yaml ]]; then
    skip "$dir already has the vault files"
else
    install -d -o root -g root -m 755 "$dir"
    for file in $VAULT_FILES; do install -o root -g root -m 644 "$here/$file" "$dir/$file"; done
    ok "files copied to $dir"
fi
[[ $(stat -c '%u %a' "$dir") == "0 755" ]] || die "$dir must be root's and writable by nobody else (sudo chown root:root $dir && sudo chmod 755 $dir)"
cd -- "$dir"
if [[ -f .env ]]; then
    skip ".env and secrets/ exist"
else
    sh make-env.sh "$host" "$port"
fi
env_matches || die "$dir/.env was written for another host or port; use those, or another --dir"
if vault_running; then
    skip "the vault runs"
else
    $compose up -d
fi
say "waiting for $url/api/status (the first start downloads images) ..."
deadline=$(( SECONDS + API_WAIT_SECONDS ))
until vault_running; do
    (( SECONDS < deadline )) || die "the vault did not answer within $API_WAIT_SECONDS seconds; see: sudo $compose logs (in $dir)"
    sleep 5
done
ok "the vault answers at $url"

if [[ ! -f infisical-root.crt ]]; then
    $compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./infisical-root.crt
    chmod 644 infisical-root.crt
fi
curl -fs -m 10 --cacert infisical-root.crt "$url/api/status" >/dev/null \
    || die "the vault's certificate does not verify with $dir/infisical-root.crt for $host"
if [[ -f $CA_FILE ]]; then
    cmp -s infisical-root.crt "$CA_FILE" \
        || die "$CA_FILE exists with another certificate; compare it with $dir/infisical-root.crt and replace it by hand if this vault is the right one"
    skip "$CA_FILE is this vault's root certificate"
else
    install -d -o root -g root -m 755 "$CA_DIR"
    install -o root -g root -m 644 infisical-root.crt "$CA_FILE"
    ok "root certificate in $CA_FILE"
fi
say "     its SHA-256 fingerprint, for your browser: $(openssl x509 -in "$CA_FILE" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"

# --- 4. Infisical CLI -----------------------------------------------------

step "4. Infisical CLI"
if [[ -x /usr/bin/infisical ]]; then
    skip "installed: $(/usr/bin/infisical --version 2>/dev/null | head -1)"
else
    keydir=$(mktemp -d)
    trap 'rm -rf -- "$keydir"' EXIT
    curl -fsSL -m 60 "$INFISICAL_REPO/infisical.gpg" -o "$keydir/key.asc" || die "could not download Infisical's signing key"
    fingerprint=$(gpg --show-keys --with-colons "$keydir/key.asc" 2>/dev/null | awk -F: '$1 == "fpr" { print $10; exit }')
    [[ $fingerprint == "$INFISICAL_KEY_FINGERPRINT" ]] \
        || die "Infisical's signing key has an unexpected fingerprint; nothing was installed"
    gpg --dearmor < "$keydir/key.asc" > "$keydir/key.gpg"
    install -o root -g root -m 644 "$keydir/key.gpg" "$KEYRING"
    printf 'deb [arch=%s signed-by=%s] %s/deb stable main\n' "$(dpkg --print-architecture)" "$KEYRING" "$INFISICAL_REPO" \
        > /etc/apt/sources.list.d/infisical.list
    apt-get update -q >/dev/null
    apt-get install -y -q "infisical=$INFISICAL_CLI_VERSION" >/dev/null || die "could not install infisical=$INFISICAL_CLI_VERSION"
    apt-mark hold infisical >/dev/null
    ok "infisical $INFISICAL_CLI_VERSION installed and held (apt-mark unhold infisical before a deliberate upgrade)"
fi

# --- 5. secretops ---------------------------------------------------------

step "5. secretops broker"
if [[ -x $BROKER_ROOT/bin/secretops ]]; then
    skip "installed: $("$BROKER_ROOT/bin/secretops" version 2>/dev/null || echo unknown); upgrade with the release bundle's install.sh --upgrade"
else
    work=$(mktemp -d)
    trap 'rm -rf -- "$work" "${keydir:-}"' EXIT
    base=https://github.com/feistify/secretops/releases/download/v$SECRETOPS_VERSION
    bundle=secretops-$SECRETOPS_VERSION-installer.tar.gz
    curl -fsSL -m 300 "$base/$bundle" -o "$work/$bundle" || die "could not download $bundle"
    curl -fsSL -m 60 "$base/SHA256SUMS" -o "$work/SHA256SUMS" || die "could not download SHA256SUMS"
    (cd "$work" && grep " $bundle\$" SHA256SUMS | sha256sum -c --quiet -) || die "$bundle does not match the release's SHA256SUMS"
    tar -xzf "$work/$bundle" -C "$work"
    if [[ -n $create_agent ]]; then agent=(--create-agent-user "$create_agent"); else agent=(--agent-user "$agent_user"); fi
    (cd "$work/secretops-$SECRETOPS_VERSION-installer" && ./install.sh "${agent[@]}" --infisical-domain "$url" --python "$python")
fi

# --- 6. connect the broker to the vault ----------------------------------

step "6. Connect the broker"
if has_token; then
    skip "the broker already has a token; check it with: sudo secretops-configure --check"
else
    say "bootstrap-broker.py creates the first admin, the project secretops (dev, staging, prod) and the broker's"
    say "machine identity and token. It asks only for the admin's email and a password of at least 14 characters."
    status=0
    "$BROKER_ROOT/venv/bin/python" -I "$here/bootstrap-broker.py" --ca-file "$CA_FILE" || status=$?
    if (( status != 0 )); then
        say "The vault may already have an admin: then create the project, identity and token in the web page"
        say "(docs/PREPARE.md) and run: sudo secretops-configure --ca-file $CA_FILE"
        exit "$status"
    fi
fi

# --- next steps -----------------------------------------------------------

step "Next"
cat <<EOF
1. Keep the vault's keys offline (password manager), then delete the archive:
     cd $dir && sudo COMPOSE="$compose" sh export-keys.sh
2. First check, in your own terminal (made-up name and value):
     secretops add FIRST_CHECK_KEY --env dev
   then as the agent account: see "First check: vault only" in docs/INSTALL.md.
3. Back ups: docs/VAULT_SELF_HOSTED.md, "Back up" (a root cron job with backup.sh).
4. Browser access to $url: trust $CA_FILE ("Trust the CA in your browser").
5. Optional: a message when a plan waits: docs/OPERATIONS.md, "Notify the human when a plan waits".
EOF
