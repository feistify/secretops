#!/bin/bash
# install-single-machine.sh: the human's part of a single-machine install.
#
# Self-hosted Infisical (this directory's compose.yaml) and the secretops
# broker on the same Linux host, in one run instead of many pasted blocks:
#
#   sudo bash install-single-machine.sh --host HOST --create-agent-user agent
#
# Run it from a copy of examples/infisical/ that only you and root can
# change, in your own terminal, with sudo. It does, and skips what is already
# done, so it can be run again after a stop:
#
#   1. checks the host: Ubuntu 22.04/24.04 or Debian 12, a container tool
#      (nerdctl or docker compose) already installed, HOST is this machine,
#      the port is free, the broker's prerequisites
#   2. Python 3.11+ for the broker: python3.12 from the deadsnakes PPA on
#      Ubuntu 22.04, otherwise python3-venv
#   3. the vault in --dir (default /opt/infisical): make-env.sh, compose up,
#      waits for the API, copies Caddy's root certificate to
#      /etc/secretops-ca/infisical-root.pem
#   4. the Infisical CLI from Infisical's apt repository: only the signing
#      key with the pinned fingerprint is trusted; version pinned and held
#   5. secretops from the GitHub release bundle, with the agent account. The
#      bundle is compared with the release's SHA256SUMS, which comes from the
#      same place, so that catches a broken download, not a forged release;
#      the bundle pins the wheel's hash, and pip checks it against PyPI
#   6. bootstrap-broker.py: asks for the first admin's email and password,
#      creates project, machine identity and token, ends with the vault check
#
# It never installs or changes a container runtime: on a Kubernetes node the
# nerdctl full bundle would replace the node's containerd (docs/TRIAL.md).
# It shows what it will change and asks first, and prints no key, password or
# token. Downloads go direct; for a proxy, pass it: sudo https_proxy=URL bash
# install-single-machine.sh ... (the vault itself is always reached
# directly). Guide: docs/VAULT_SELF_HOSTED.md.
set -Eeuo pipefail
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

SECRETOPS_VERSION=0.1.15
BOOTSTRAP_MIN_VERSION=0.1.14
INFISICAL_CLI_VERSION=0.45.14
# Primary key of "Infisical code artifacts", which signs artifacts-cli.infisical.com.
INFISICAL_KEY_FINGERPRINT=698CE22DCBD9C67562F3C4749CC397EDC268078E
INFISICAL_REPO=https://artifacts-cli.infisical.com
KEYRING=/usr/share/keyrings/infisical-archive-keyring.gpg
APT_SOURCE=/etc/apt/sources.list.d/infisical.list
CA_DIR=/etc/secretops-ca
CA_FILE=$CA_DIR/infisical-root.pem
BROKER_ROOT=/opt/secretops
BROKER_USER=secretops-broker
BROKER_SSH_PORT=22222
VAULT_FILES=(compose.yaml Caddyfile make-env.sh backup.sh export-keys.sh import-keys.sh bootstrap-broker.py)
API_WAIT_SECONDS=600

host='' port=8443 dir=/opt/infisical agent_user='' create_agent='' assume_yes=0
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cleanup=()
trap 'rm -rf -- ${cleanup[@]+"${cleanup[@]}"}' EXIT
trap 'printf "FAIL at line %s (output above); fix the cause and run again: finished steps are skipped\n" "$LINENO" >&2' ERR

usage() {
    cat <<'EOF'
usage:
  sudo bash install-single-machine.sh --host HOST (--create-agent-user NAME | --agent-user USER)
                                      [--port PORT] [--dir DIR] [--yes]

  --host HOST              DNS name or IPv4 address of this machine, which the broker and your
                           browser use for the vault
  --create-agent-user NAME create a separate account without sudo for the coding agent
  --agent-user USER        or: an existing account the coding agent runs as
  --port PORT              HTTPS port of the vault (default 8443)
  --dir DIR                where the vault's files go (default /opt/infisical); its name
                           names the Compose project and its volumes
  --yes                    do not ask before changing the system
EOF
}

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
ok() { printf 'OK   %s\n' "$*"; }
skip() { printf 'SKIP %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
die() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

# apt without questions, without removing packages and without restarting
# services (needrestart only lists them).
apt_get() { DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -q -y -o Dpkg::Options::=--force-confold "$@"; }

# Write only the key with the pinned fingerprint from the downloaded file
# $1 to the keyring $2; any other key in the download is left out.
# published PORT LOCAL_ADDRESSES: a NAT rule (in $nat) that sends PORT on
# this host elsewhere: a NodePort or a container's published port. A rule
# whose -d is another address (a Kubernetes ClusterIP) does not count.
# Read from a string, not a pipe: with pipefail, awk's early exit could
# fail the writer and hide the match.
published() {
    awk -v p="$1" -v locals=" $2 " '
        { dport = 0; dst = ""; neg = 0
          for (i = 1; i < NF; i++) {
              if ($i == "--dport" && $(i + 1) == p) dport = 1
              if ($i == "-d") { dst = $(i + 1); neg = ($(i - 1) == "!") }
          }
          if (!dport) next
          sub(/\/32$/, "", dst)
          if (dst == "" || neg || index(locals, " " dst " ")) { found = 1; exit } }
        END { exit !found }' <<<"$nat"
}

pinned_key() {
    local home status=0
    home=$(mktemp -d)
    if gpg --batch --quiet --homedir "$home" --import "$1" 2>/dev/null \
        && gpg --batch --homedir "$home" --export "$INFISICAL_KEY_FINGERPRINT" > "$2" 2>/dev/null \
        && [[ -s $2 ]] \
        && [[ $(gpg --batch --show-keys --with-colons "$2" 2>/dev/null | awk -F: '$1 == "pub"' | wc -l) == 1 ]] \
        && [[ $(gpg --batch --show-keys --with-colons "$2" 2>/dev/null | awk -F: '$1 == "fpr" { print $10; exit }') == "$INFISICAL_KEY_FINGERPRINT" ]]; then
        status=0
    else
        status=1
    fi
    rm -rf -- "$home"
    return "$status"
}

version_at_least() { [[ $(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1) == "$2" ]]; }

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
[[ -n ${SUDO_USER:-} && $SUDO_USER != root && -n ${SUDO_UID:-} ]] \
    || die "run with sudo from your own (non-root) account, not as root directly"

# Root runs these files; nobody else may be able to change them.
for path in "$here" "${VAULT_FILES[@]/#/$here/}"; do
    [[ -e $path ]] || die "$path is missing; run this from a copy of examples/infisical/"
    read -r owner group mode < <(stat -c '%u %g %a' -- "$path")
    # Group write is fine for root's group and your own primary group (on
    # Ubuntu and Debian a group of your own; a checkout gets mode 664 there).
    if [[ -L $path ]] || [[ $owner != 0 && $owner != "$SUDO_UID" ]] || (( (8#$mode & 8#002) != 0 )) \
        || { (( (8#$mode & 8#020) != 0 )) && [[ $group != 0 && $group != "${SUDO_GID:-}" ]]; }; then
        die "$path must belong to you or root and be writable by nobody else (chmod go-w), because root runs it"
    fi
done
for tool in curl tar sha256sum gpg awk ss openssl cmp getent ip sort; do
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
    # The default namespace, also where nerdctl.toml names another (such as
    # Kubernetes' k8s.io, whose images the kubelet may remove).
    compose="nerdctl --namespace default compose"
elif command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    compose="docker compose"
else
    die "no container tool: install nerdctl (containerd) or Docker with the Compose plugin first; see 'Choose the container tool' in docs/VAULT_SELF_HOSTED.md. This script never installs one."
fi
ok "container tool: $compose"
node=0
if systemctl is-active --quiet kubelet 2>/dev/null; then
    node=1
    warn "this is a Kubernetes node: the vault runs beside the cluster (about 1-2 GB of memory, outside the kubelet's accounting), and whoever has root here can approve plans and read the vault's keys (docs/SECURITY_MODEL.md, 'Who can act as the broker')"
fi

# HOST must be this machine: the vault is reached at its address, directly.
if [[ $host =~ ^[0-9.]+$ ]]; then
    address=$host
else
    address=$(getent ahostsv4 "$host" | awk 'NR == 1 { print $1 }') || address=''
    [[ -n $address ]] || die "$host does not resolve here; add it to DNS or /etc/hosts, or give this machine's IPv4 address"
fi
ip -o -4 addr show | awk '{ split($4, a, "/"); print a[1] }' | grep -qxF "$address" \
    || die "$host is $address, which is not an address of this machine; use its LAN IPv4 address or a DNS name that resolves to it"
url=https://$host:$port
vault_curl() { curl --noproxy '*' --resolve "$host:$port:$address" -fs -m 10 "$@"; }
vault_answers() { vault_curl -k "$url/api/status" 2>/dev/null | grep -q '"message":"Ok"'; }

fresh_vault=1
if [[ -f $dir/.env ]]; then
    fresh_vault=0
    # shellcheck disable=SC2015  # both failing tests stop the run
    grep -qx "INFISICAL_HOST=$host" "$dir/.env" && grep -qx "HTTPS_PORT=$port" "$dir/.env" \
        || die "$dir/.env was written for another host or port; use those, or another --dir"
fi
# One vault per broker: a new vault on a host whose broker already trusts or
# uses another one would be started and then left unconnected.
if (( fresh_vault )); then
    [[ ! -f $CA_FILE ]] \
        || die "$CA_FILE already holds another vault's certificate; give that vault's --dir and --port, or set up a new vault by hand (docs/VAULT_SELF_HOSTED.md)"
    if grep -q '^INFISICAL_TOKEN=' "$BROKER_ROOT/broker.env" 2>/dev/null; then
        die "the broker is already connected to a vault; give that vault's --dir and --port"
    fi
fi
[[ ! -e $dir/secrets.tmp ]] || die "a stopped make-env.sh left $dir/secrets.tmp; remove it (sudo rm -rf $dir/secrets.tmp) and run again"
if (( fresh_vault )); then
    if ss -Htln "sport = :$port" | grep -q .; then die "port $port is in use; choose another --port"; fi
    # Captured first: grep -q would stop the pipe early, and with pipefail a
    # large rule set (kube-proxy) would then hide the match.
    nat=$(iptables-save -t nat 2>/dev/null || true)
    if published "$port" "$(ip -o -4 addr show | awk '{ split($4, a, "/"); printf "%s ", a[1] }')"; then
        die "port $port is published by a NodePort, a pod or a container (iptables); choose another --port"
    fi
    if [[ $compose == nerdctl* && ! -e /etc/cni/net.d/nerdctl-bridge.conflist ]]; then
        # nerdctl may write nerdctl-bridge.conflist into /etc/cni/net.d (the
        # compose network goes to a subdirectory). containerd's CRI uses the
        # first file there in name order: it must not come before the cluster's.
        first=$( { find /etc/cni/net.d -maxdepth 1 -type f \( -name '*.conf' -o -name '*.conflist' -o -name '*.json' \) \
            -printf '%f\n' 2>/dev/null || true; } | sort | head -1)
        if [[ -n $first && ! $first < nerdctl-bridge.conflist ]]; then
            die "/etc/cni/net.d/$first would come after nerdctl's nerdctl-bridge.conflist, and the cluster could switch to it; run the vault elsewhere"
        fi
    fi
fi

python='' python_todo=''
for candidate in /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3; do
    if [[ -x $candidate ]] && "$candidate" -c 'import sys, ensurepip, venv; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        python=$candidate
        break
    fi
done
if [[ -z $python ]]; then
    if [[ $ID:$VERSION_ID == ubuntu:22.04 ]]; then
        python_todo="add the deadsnakes PPA (stays as an apt source) and install python3.12 and python3.12-venv"
    else
        python_todo="install python3-venv"
    fi
fi

broker_installed=0
if [[ -x $BROKER_ROOT/bin/secretops ]]; then
    broker_installed=1
    broker_version=$("$BROKER_ROOT/bin/secretops" version 2>/dev/null | awk '{ print $2 }') || broker_version=''
    version_at_least "${broker_version:-0}" "$BOOTSTRAP_MIN_VERSION" \
        || die "the installed secretops ${broker_version:-?} is older than $BOOTSTRAP_MIN_VERSION; upgrade it with the release bundle's install.sh --upgrade first"
else
    [[ ! -e $BROKER_ROOT ]] \
        || die "$BROKER_ROOT exists without a working install; remove it as in docs/INSTALL.md, 'Remove a failed installation', then run again"
    ! getent passwd "$BROKER_USER" >/dev/null || die "account $BROKER_USER exists without a broker install; remove it as in docs/INSTALL.md, 'Remove a failed installation'"
    [[ -x /usr/sbin/sshd ]] || die "OpenSSH server is required: sudo apt install openssh-server (docs/INSTALL.md, 'Prerequisites')"
    if ss -Htln "sport = :$BROKER_SSH_PORT" | grep -q .; then die "TCP port $BROKER_SSH_PORT is in use; the broker's SSH service needs it"; fi
    if [[ -n $create_agent ]]; then
        ! getent passwd "$create_agent" >/dev/null || die "account $create_agent already exists; use --agent-user $create_agent"
    else
        getent passwd "$agent_user" >/dev/null || die "account $agent_user does not exist; use --create-agent-user $agent_user"
    fi
fi
has_token() { grep -q '^INFISICAL_TOKEN=' "$BROKER_ROOT/broker.env" 2>/dev/null; }

todo=()
[[ -z $python_todo ]] || todo+=("$python_todo")
missing_files=()
for file in "${VAULT_FILES[@]}"; do [[ -f $dir/$file ]] || missing_files+=("$file"); done
(( ${#missing_files[@]} == 0 )) || todo+=("copy the vault files to $dir (root-owned): ${missing_files[*]}")
(( ! fresh_vault )) || todo+=("write the vault's keys and passwords to $dir/secrets (root only; make-env.sh)")
vault_answers || todo+=("start the vault: $compose up -d in $dir (downloads images; restarts with the machine)")
[[ -f $CA_FILE ]] || todo+=("copy the vault's root certificate to $CA_FILE")
[[ -x /usr/bin/infisical ]] || todo+=("add Infisical's apt source (stays) and install the Infisical CLI $INFISICAL_CLI_VERSION (held)")
(( broker_installed )) || todo+=("install secretops $SECRETOPS_VERSION: account $BROKER_USER, $BROKER_ROOT, programs in /usr/local, an SSH service on 127.0.0.1:$BROKER_SSH_PORT, agent account ${create_agent:-$agent_user}${create_agent:+ (new)}")
has_token || todo+=("connect the broker to the new vault: bootstrap-broker.py asks for the first admin's email and password")

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
if [[ -z $python ]]; then
    apt_get update >/dev/null
    if [[ $ID:$VERSION_ID == ubuntu:22.04 ]]; then
        apt_get install --no-remove software-properties-common >/dev/null
        add-apt-repository -y ppa:deadsnakes/ppa >/dev/null \
            || die "could not add the deadsnakes PPA (network or proxy?); install Python 3.12 and run again"
        apt_get install --no-remove python3.12 python3.12-venv >/dev/null || die "could not install python3.12"
        python=/usr/bin/python3.12
    else
        apt_get install --no-remove python3-venv >/dev/null || die "could not install python3-venv"
        python=/usr/bin/python3
    fi
fi
ok "$python ($("$python" -V 2>&1))"

# --- 3. vault -------------------------------------------------------------

step "3. Vault in $dir"
install -d -o root -g root -m 755 "$dir"
if (( ${#missing_files[@]} == 0 )); then
    skip "$dir has the vault files"
else
    for file in "${missing_files[@]}"; do install -o root -g root -m 644 "$here/$file" "$dir/$file"; done
    ok "copied: ${missing_files[*]}"
fi
for path in "$dir" "${VAULT_FILES[@]/#/$dir/}"; do
    read -r owner mode < <(stat -c '%u %a' -- "$path")
    # shellcheck disable=SC2015  # both failing tests stop the run
    [[ $owner == 0 ]] && (( (8#$mode & 8#022) == 0 )) \
        || die "$path must be root's and writable by nobody else: sudo chown -R root:root $dir && sudo chmod -R go-w $dir"
done
cd -- "$dir"
if (( fresh_vault )); then
    sh make-env.sh "$host" "$port"
else
    skip ".env and secrets/ exist"
fi
if vault_answers; then
    skip "the vault runs"
else
    $compose up -d
fi
say "waiting for $url/api/status (the first start downloads images) ..."
deadline=$(( SECONDS + API_WAIT_SECONDS ))
until vault_answers; do
    (( SECONDS < deadline )) || die "the vault did not answer within $API_WAIT_SECONDS seconds; see: cd $dir && sudo $compose logs"
    sleep 5
done
if [[ ! -f infisical-root.crt ]]; then
    $compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./infisical-root.crt \
        || die "could not copy the root certificate from this directory's caddy container"
    chmod 644 infisical-root.crt
fi
vault_curl --cacert infisical-root.crt "$url/api/status" >/dev/null \
    || die "the vault at $url does not present this directory's certificate ($dir/infisical-root.crt); is another service on port $port?"
ok "the vault answers at $url"
if [[ -f $CA_FILE ]]; then
    cmp -s infisical-root.crt "$CA_FILE" \
        || die "$CA_FILE exists with another certificate; compare it with $dir/infisical-root.crt and replace it by hand if this vault is the right one"
    skip "$CA_FILE is this vault's root certificate"
else
    install -d -o root -g root -m 755 "$CA_DIR"
    install -o root -g root -m 644 infisical-root.crt "$CA_FILE"
    ok "root certificate in $CA_FILE"
fi
say "     SHA-256 fingerprint, to compare in your browser: $(openssl x509 -in "$CA_FILE" -noout -fingerprint -sha256 | cut -d= -f2)"

# --- 4. Infisical CLI -----------------------------------------------------

step "4. Infisical CLI"
if [[ -x /usr/bin/infisical ]]; then
    installed=$(/usr/bin/infisical --version 2>/dev/null | awk '{ print $NF; exit }') || installed=''
    if [[ $installed == "$INFISICAL_CLI_VERSION" ]]; then
        skip "infisical $installed installed"
    else
        warn "infisical ${installed:-?} is installed; tested: $INFISICAL_CLI_VERSION (docs/LIMITATIONS.md). Kept as it is."
    fi
else
    keydir=$(mktemp -d)
    cleanup+=("$keydir")
    curl --proto '=https' -fsSL -m 60 "$INFISICAL_REPO/infisical.gpg" -o "$keydir/key.asc" \
        || die "could not download Infisical's signing key"
    pinned_key "$keydir/key.asc" "$keydir/key.gpg" \
        || die "Infisical's signing key with fingerprint $INFISICAL_KEY_FINGERPRINT was not in the download; nothing was installed"
    install -o root -g root -m 644 "$keydir/key.gpg" "$KEYRING"
    printf 'deb [arch=%s signed-by=%s] %s/deb stable main\n' "$(dpkg --print-architecture)" "$KEYRING" "$INFISICAL_REPO" > "$APT_SOURCE"
    if ! apt_get update >/dev/null || ! apt_get install --no-remove "infisical=$INFISICAL_CLI_VERSION" >/dev/null; then
        rm -f -- "$APT_SOURCE"
        die "could not install infisical=$INFISICAL_CLI_VERSION; Infisical's apt source was removed again"
    fi
    apt-mark hold infisical >/dev/null
    ok "infisical $INFISICAL_CLI_VERSION installed and held (apt-mark unhold infisical before a deliberate upgrade)"
fi

# --- 5. secretops ---------------------------------------------------------

step "5. secretops broker"
if (( broker_installed )); then
    skip "secretops $broker_version installed; the agent account is the one it was installed with"
else
    work=$(mktemp -d)
    cleanup+=("$work")
    base=https://github.com/feistify/secretops/releases/download/v$SECRETOPS_VERSION
    bundle=secretops-$SECRETOPS_VERSION-installer.tar.gz
    curl --proto '=https' -fsSL -m 300 "$base/$bundle" -o "$work/$bundle" || die "could not download $bundle"
    curl --proto '=https' -fsSL -m 60 "$base/SHA256SUMS" -o "$work/SHA256SUMS" || die "could not download SHA256SUMS"
    expected=$(awk -v file="$bundle" '$2 == file { print $1 }' "$work/SHA256SUMS")
    [[ -n $expected && $(sha256sum -- "$work/$bundle" | awk '{ print $1 }') == "$expected" ]] \
        || die "$bundle does not match the release's SHA256SUMS"
    tar -xzf "$work/$bundle" -C "$work"
    if [[ -n $create_agent ]]; then agent=(--create-agent-user "$create_agent"); else agent=(--agent-user "$agent_user"); fi
    (cd "$work/secretops-$SECRETOPS_VERSION-installer" && ./install.sh "${agent[@]}" --infisical-domain "$url" --python "$python") \
        || die "the secretops installer stopped (see above); fix the cause, then run this again"
fi

# --- 6. connect the broker to the vault ----------------------------------

step "6. Connect the broker"
if has_token; then
    skip "the broker has a token; check it with: sudo secretops-configure --check"
else
    say "bootstrap-broker.py creates the first admin, the project secretops (dev, staging, prod) and the broker's"
    say "machine identity and token. It asks only for the admin's email and a password of at least 14 characters."
    status=0
    env no_proxy="$host" NO_PROXY="$host" "$BROKER_ROOT/venv/bin/python" -I "$dir/bootstrap-broker.py" \
        --url "$url" --ca-file "$CA_FILE" || status=$?
    if (( status != 0 )); then
        say "If the vault already has an admin, create the project, identity and token in the web page"
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
3. Backups: docs/VAULT_SELF_HOSTED.md, "Back up" (a root cron job with backup.sh).
4. Browser access to $url: trust $CA_FILE ("Trust the CA in your browser").
5. Optional: a message when a plan waits: docs/OPERATIONS.md, "Notify the human when a plan waits".
EOF
(( ! node )) || say "On this Kubernetes node, use COMPOSE=\"$compose\" for every compose command in the guide."
