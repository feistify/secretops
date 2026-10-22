#!/bin/bash
# install.sh: install or upgrade a local secretops broker on Linux.
#
# Run as root from the human's own terminal. It uses the wrappers and
# broker-requirements.txt next to this script, so run it from an unpacked
# installer bundle of a release or from a repository checkout:
#
#   sudo ./install.sh --agent-user USER --infisical-domain https://vault.example.com
#   sudo ./install.sh --upgrade
#
# In a bundle, secretops-release.txt pins the release: the program comes
# from PyPI and pip checks it against the pinned SHA-256. In a checkout, pass
# a wheel built with `uv build` instead:
#
#   sudo bash scripts/install.sh --wheel dist/secretops-0.1.4-py3-none-any.whl \
#       --agent-user USER --infisical-domain https://vault.example.com
#
# Fresh install creates the secretops-broker account, /opt/secretops (root-
# owned application, broker-owned broker.env/plans/audit/recipes), the
# wrappers, a loopback-only SSH service for the agent, an agent key and
# pinned host key in the agent user's ~/.ssh, and the human's client config.
# It never asks for, reads, or writes a credential: the human runs
# secretops-configure afterwards. Upgrade replaces only the application and
# wrappers and keeps the previous ones for a manual rollback.
#
# See docs/INSTALL.md and docs/OPERATIONS.md.
set -Eeuo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

ROOT=/opt/secretops
BROKER_USER=secretops-broker
BROKER_HOME=/var/lib/secretops-broker
UNIT=secretops-local-sshd.service
UNIT_FILE=/etc/systemd/system/$UNIT
INFO=$ROOT/install-info
PYTHON=/usr/bin/python3
AGENT_KEY=secretops_local_agent_ed25519
AGENT_KNOWN_HOSTS=secretops_local_known_hosts
# Installed program -> source file in scripts/.
WRAPPERS="/usr/local/bin/secretops-broker:secretops-broker
/usr/local/bin/secretops-agent:secretops-agent
/usr/local/bin/secretops-local:secretops-local
/usr/local/sbin/secretops-uninstall:secretops-uninstall"

die() {
    echo "install: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  sudo ./install.sh (--agent-user USER | --create-agent-user NAME) --infisical-domain URL
                    [--wheel WHEEL] [--infisical-bin PATH] [--gh-bin PATH] [--port PORT] [--no-start]
  sudo ./install.sh --upgrade [--wheel WHEEL]

  --wheel PATH            secretops wheel: required in a repository checkout (uv build);
                          optional in a release bundle, where it must match the pinned hash
  --agent-user USER       existing account the coding agent runs as
  --create-agent-user NAME
                          create a new account without sudo for the coding agent
  --infisical-domain URL  vault URL, for example https://app.infisical.com
  --infisical-bin PATH    root-owned infisical CLI (default: /usr/bin/infisical)
  --gh-bin PATH           root-owned gh CLI (default: /usr/bin/gh if present)
  --port PORT             loopback SSH port for the agent (default: 22222)
  --no-start              write the SSH service but do not enable or start it
  --allow-untested-os     install on a distribution other than Ubuntu or Debian
  --upgrade               replace application and wrappers of an existing install
EOF
}

wheel="" agent_user="" create_agent="" domain="" infisical_bin=/usr/bin/infisical gh_bin="" port=22222
upgrade=0 start=1 allow_untested_os=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --wheel) wheel=${2:?}; shift 2 ;;
        --agent-user) agent_user=${2:?}; shift 2 ;;
        --create-agent-user) create_agent=${2:?}; shift 2 ;;
        --infisical-domain) domain=${2:?}; shift 2 ;;
        --infisical-bin) infisical_bin=${2:?}; shift 2 ;;
        --gh-bin) gh_bin=${2:?}; shift 2 ;;
        --port) port=${2:?}; shift 2 ;;
        --no-start) start=0; shift ;;
        --allow-untested-os) allow_untested_os=1; shift ;;
        --upgrade) upgrade=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

[[ $EUID == 0 ]] || die "run with sudo from your own terminal, not from an agent session"

# --- the system: stop here with a clear message rather than fail midway ---
[[ $(uname -s) == Linux ]] || die "secretops brokers run on Linux only"
os_id=$(. /etc/os-release 2>/dev/null && echo "${ID:-unknown}") || os_id=unknown
os_name=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-$os_id}") || os_name=$os_id
case $os_id in
    ubuntu | debian) ;;
    *)
        [[ $allow_untested_os == 1 ]] \
            || die "untested system: $os_name. Tested: Ubuntu 24.04 and Debian 12. To try anyway, add --allow-untested-os"
        echo "WARNING: untested system: $os_name."
        ;;
esac
missing=()
for command in sshd ssh ssh-keygen ss runuser useradd userdel pgrep stat sha256sum tar; do
    command -v "$command" >/dev/null || missing+=("$command")
done
[[ ${#missing[@]} == 0 ]] || die "missing commands: ${missing[*]} (Ubuntu and Debian: apt install openssh-server openssh-client iproute2 procps)"
if [[ $upgrade == 0 && $start == 1 && ! -d /run/systemd/system ]]; then
    die "systemd is not running; it starts the broker's SSH service. Use --no-start to only write the service"
fi
src=$(cd -- "$(dirname -- "$0")" && pwd)
for name in secretops-broker secretops-agent secretops-local secretops-uninstall broker-requirements.txt; do
    [[ -f $src/$name ]] || die "missing $src/$name; run from an unpacked installer bundle or a repository checkout"
done
if [[ -n $wheel ]]; then
    [[ -f $wheel && $(basename -- "$wheel") == secretops-*-py3-none-any.whl ]] \
        || die "--wheel must be a secretops-*-py3-none-any.whl file"
    wheel=$(readlink -f -- "$wheel")
    wheel_sha256=$(sha256sum -- "$wheel" | cut -d' ' -f1)
fi
# A release bundle pins one version and its wheel hash.
release_pin=$src/secretops-release.txt
if [[ -f $release_pin ]]; then
    pin=$(sed -n 's/^secretops==\([0-9][0-9a-z.]*\) --hash=sha256:\([0-9a-f]\{64\}\)$/\1 \2/p' "$release_pin")
    [[ -n $pin && $(grep -c . "$release_pin") == 1 ]] || die "malformed $release_pin"
    read -r pin_version pin_sha256 <<< "$pin"
    if [[ -n $wheel ]]; then
        [[ $(basename -- "$wheel") == "secretops-$pin_version-py3-none-any.whl" && $wheel_sha256 == "$pin_sha256" ]] \
            || die "--wheel does not match the release pinned in this bundle (secretops $pin_version)"
    fi
    wheel_sha256=$pin_sha256
elif [[ -z $wheel ]]; then
    usage >&2
    die "--wheel is required outside a release bundle"
fi

"$PYTHON" -c 'import sys, ensurepip, venv; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
    || die "$PYTHON 3.11 or newer with venv is required; found: $("$PYTHON" -V 2>&1 || echo none) (Ubuntu and Debian: apt install python3-venv)"

# An executable that runs with the broker's credentials must not be
# replaceable by an unprivileged user: it and every parent directory must be
# root-owned and not group- or other-writable. It takes only a canonical
# path: callers resolve a given path once, check that, and store that same
# string, so a symlink the agent controls cannot be repointed in between.
trusted() {
    local path=$1
    [[ $path == /* && -f $path && -x $path && $(readlink -f -- "$path") == "$path" ]] || return 1
    while :; do
        read -r uid mode < <(stat -c '%u %a' -- "$path")
        [[ $uid == 0 ]] && (( (8#$mode & 8#022) == 0 )) || return 1
        [[ $path == / ]] && return 0
        path=$(dirname -- "$path")
    done
}

# Build a venv from the hash-pinned dependencies and the wheel, owned by
# root and checked before it is used.
# Download every package before anything on the system changes, so a
# network or PyPI problem stops the installer with nothing to clean up. pip
# checks each file against the pinned hashes; the venv is then built from
# these files only, without the network.
fetch_packages() {
    fetch_dir=$(mktemp -d)
    trap 'rm -rf -- "$fetch_dir"' EXIT
    echo "Downloading packages from PyPI (checked against pinned hashes)..."
    "$PYTHON" -m venv "$fetch_dir/venv"
    "$fetch_dir/venv/bin/python" -I -m pip download --quiet --no-cache-dir --disable-pip-version-check \
        --require-hashes --only-binary=:all: -d "$fetch_dir/packages" -r "$src/broker-requirements.txt" \
        || die "could not download the dependencies with their pinned hashes; nothing was changed"
    if [[ -z $wheel ]]; then
        "$fetch_dir/venv/bin/python" -I -m pip download --quiet --no-cache-dir --disable-pip-version-check \
            --require-hashes --only-binary=:all: --no-deps -d "$fetch_dir/program" -r "$release_pin" \
            || die "could not download secretops $pin_version with the pinned hash; nothing was changed"
        wheel=$fetch_dir/program/secretops-$pin_version-py3-none-any.whl
        [[ -f $wheel && $(sha256sum -- "$wheel" | cut -d' ' -f1) == "$pin_sha256" ]] \
            || die "downloaded secretops does not match the pinned hash; nothing was changed"
    fi
}

build_venv() {
    local dest=$1
    [[ ! -e $dest && ! -L $dest ]] || die "$dest exists; inspect and remove it first"
    echo "Building application environment..."
    "$PYTHON" -m venv "$dest"
    "$dest/bin/python" -I -m pip install --quiet --no-cache-dir --disable-pip-version-check \
        --no-index --find-links "$fetch_dir/packages" \
        --require-hashes --only-binary=:all: -r "$src/broker-requirements.txt"
    "$dest/bin/python" -I -m pip install --quiet --no-cache-dir --disable-pip-version-check \
        --no-index --no-deps "$wheel"
    chown -R root:root "$dest"
    chmod -R go-w "$dest"
    smoke "$dest" || die "application check failed in $dest"
}

smoke() {
    (cd / && env -i PATH=/usr/bin:/bin "$1/bin/python" -I -m secretops.cli version >/dev/null)
}

write_launcher() { # path module
    printf '#!/bin/sh\nexec %s/venv/bin/python -I -m %s "$@"\n' "$ROOT" "$2" > "$1"
    chown root:root "$1"
    chmod 0755 "$1"
}

install_wrappers() {
    local target source
    while IFS=: read -r target source; do
        install -o root -g root -m 0755 "$src/$source" "$target"
    done <<< "$WRAPPERS"
    sed -i "s/^PORT=.*/PORT=$port/" /usr/local/bin/secretops-local
    write_launcher "$ROOT/bin/secretops" secretops.cli
    write_launcher /usr/local/bin/secretops secretops.cli
    write_launcher /usr/local/sbin/secretops-configure secretops.broker_config
}

write_info() {
    cat > "$INFO" <<EOF
installer=secretops-install-1
version=$("$ROOT/venv/bin/python" -I -c 'from importlib.metadata import version; print(version("secretops"))')
wheel_sha256=$wheel_sha256
agent_user=$agent_user
agent_created=$agent_created
port=$port
installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
    chmod 0644 "$INFO"
}

info_value() {
    sed -n "s/^$1=//p" "$INFO" | head -n 1
}

as_agent() {
    runuser -u "$agent_user" -- "$@"
}

if [[ $upgrade == 1 ]]; then
    [[ -f $INFO && ! -L $INFO && $(info_value installer) == secretops-install-1 ]] \
        || die "no installation by this installer at $ROOT; upgrade it with its own procedure"
    [[ -d $ROOT/venv && ! -L $ROOT/venv ]] || die "unexpected layout: $ROOT/venv"
    if pgrep -u "$BROKER_USER" >/dev/null; then
        die "broker processes are running; let them finish and pause the agent first"
    fi
    agent_user=$(info_value agent_user)
    port=$(info_value port)
    # Installations from before --create-agent-user did not create the account.
    agent_created=$(info_value agent_created)
    [[ $agent_created == yes ]] || agent_created=no
    fetch_packages
    build_venv "$ROOT/venv.new"
    rm -rf "$ROOT/venv.previous" "$ROOT/previous"
    install -d -o root -g root -m 0755 "$ROOT/previous"
    while IFS=: read -r target source; do
        [[ ! -e $target ]] || cp -p "$target" "$ROOT/previous/$(basename -- "$target")"
    done <<< "$WRAPPERS"
    mv "$ROOT/venv" "$ROOT/venv.previous"
    mv "$ROOT/venv.new" "$ROOT/venv"
    if ! smoke "$ROOT/venv"; then
        mv "$ROOT/venv" "$ROOT/venv.new"
        mv "$ROOT/venv.previous" "$ROOT/venv"
        die "new application failed its check; previous application restored"
    fi
    install_wrappers
    write_info
    runuser -u "$BROKER_USER" -- /usr/local/bin/secretops-broker version
    if ! "$ROOT/venv/bin/python" -I -c '
import sys
from importlib import resources
packaged = resources.files("secretops.policies").joinpath("default.yaml").read_text()
sys.exit(packaged != open(sys.argv[1]).read())' "$ROOT/policy.yaml"; then
        echo "NOTE: $ROOT/policy.yaml differs from this version's default policy. It was not"
        echo "      changed; review the release notes before editing it."
    fi
    echo "Upgraded. broker.env, policy, plans, audit, SSH service and keys were not changed."
    echo "Previous application: $ROOT/venv.previous; previous wrappers: $ROOT/previous/"
    echo "Rollback: see docs/OPERATIONS.md, 'Roll back an upgrade'."
    exit 0
fi

# --- fresh installation: check everything before changing anything ---
agent_created=no
if [[ -n $create_agent ]]; then
    [[ -z $agent_user ]] || die "use either --agent-user or --create-agent-user"
    [[ $create_agent =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "--create-agent-user: invalid account name"
    ! getent passwd "$create_agent" >/dev/null || die "account $create_agent already exists; use --agent-user"
    ! getent group "$create_agent" >/dev/null || die "group $create_agent already exists; choose another name"
    agent_user=$create_agent
fi
[[ -n $agent_user && -n $domain ]] || { usage >&2; exit 2; }
[[ $port =~ ^[0-9]+$ ]] && (( port >= 1024 && port <= 65535 )) || die "--port must be 1024-65535"
[[ $agent_user != root && $agent_user != "$BROKER_USER" ]] || die "the agent must not run as $agent_user"
human=${SUDO_USER:-}
[[ -n $human && $human != root ]] || die "run sudo from your normal login, not a root shell"
agent_files=()
if [[ -z $create_agent ]]; then
    agent_entry=$(getent passwd "$agent_user") || die "agent user $agent_user does not exist"
    agent_home=$(cut -d: -f6 <<< "$agent_entry")
    [[ -d $agent_home ]] || die "home of $agent_user does not exist"
    if [[ " $(id -nG "$agent_user") " =~ \ (sudo|admin|wheel)\  ]]; then
        echo "WARNING: $agent_user is in an administrator group; the approval boundary is not enforced."
    fi
    agent_files=("$agent_home/.ssh/$AGENT_KEY" "$agent_home/.ssh/$AGENT_KEY.pub" "$agent_home/.ssh/$AGENT_KNOWN_HOSTS")
fi
for path in "$ROOT" "$BROKER_HOME" /usr/local/bin/secretops /usr/local/bin/secretops-broker \
    /usr/local/bin/secretops-agent /usr/local/bin/secretops-local /usr/local/sbin/secretops-configure \
    /usr/local/sbin/secretops-uninstall \
    "$UNIT_FILE" ${agent_files[@]+"${agent_files[@]}"}; do
    [[ ! -e $path && ! -L $path ]] || die "refusing to overwrite existing $path"
done
! getent passwd "$BROKER_USER" >/dev/null || die "account $BROKER_USER already exists; inspect it first"
[[ -x /usr/sbin/sshd ]] || die "OpenSSH server is required (see docs/INSTALL.md, 'Prerequisites')"
[[ -z $(ss -ltnH "sport = :$port") ]] || die "TCP port $port is already in use"
infisical_bin=$(readlink -f -- "$infisical_bin") || die "$infisical_bin does not exist"
trusted "$infisical_bin" || die "$infisical_bin must be an executable whose path is root-owned and not writable by others"
if [[ -z $gh_bin && -e /usr/bin/gh ]]; then
    gh_bin=/usr/bin/gh
fi
if [[ -n $gh_bin ]]; then
    gh_bin=$(readlink -f -- "$gh_bin") || die "$gh_bin does not exist"
    trusted "$gh_bin" || die "$gh_bin must be an executable whose path is root-owned and not writable by others"
fi

fetch_packages
trap 'echo "install: failed partway; see docs/INSTALL.md, \"Remove a failed installation\"" >&2' ERR

if [[ -n $create_agent ]]; then
    # A normal login account, in no administrator group, password locked.
    useradd --create-home --shell /bin/bash "$agent_user"
    agent_created=yes
    agent_home=$(getent passwd "$agent_user" | cut -d: -f6)
    echo "Created the agent account $agent_user (no sudo, no password)."
fi

install -d -o root -g root -m 0755 "$ROOT" "$ROOT/bin" "$ROOT/ssh"
build_venv "$ROOT/venv.new"
mv "$ROOT/venv.new" "$ROOT/venv"
"$ROOT/venv/bin/python" -I -c '
from importlib import resources
print(resources.files("secretops.policies").joinpath("default.yaml").read_text(), end="")' \
    > "$ROOT/policy.yaml"
chmod 0644 "$ROOT/policy.yaml"

useradd --system --create-home --home-dir "$BROKER_HOME" --shell /bin/sh --password '*' "$BROKER_USER"
chmod 0700 "$BROKER_HOME"
for name in recipes plans audit; do
    install -d -o "$BROKER_USER" -g "$BROKER_USER" -m 0700 "$ROOT/$name"
done
"$ROOT/venv/bin/python" -I -m secretops.broker_config --init --domain "$domain" \
    --infisical-bin "$infisical_bin" ${gh_bin:+--gh-bin "$gh_bin"}
install_wrappers

# Agent key and pinned host key. Files in the agent's home are written as
# the agent, so root never follows a link the agent placed there.
as_agent mkdir -p -m 0700 "$agent_home/.ssh"
as_agent ssh-keygen -q -t ed25519 -N '' -C secretops-local-agent -f "$agent_home/.ssh/$AGENT_KEY"
agent_pub=$(as_agent cat "$agent_home/.ssh/$AGENT_KEY.pub")
[[ $agent_pub =~ ^(ssh-ed25519\ [A-Za-z0-9+/]+=*) ]] || die "unexpected agent public key format"
printf 'from="127.0.0.1",command="/usr/local/bin/secretops-agent",restrict %s\n' \
    "${BASH_REMATCH[1]}" > "$ROOT/ssh/authorized_keys"
chmod 0644 "$ROOT/ssh/authorized_keys"
ssh-keygen -q -t ed25519 -N '' -C secretops-local-host -f "$ROOT/ssh/ssh_host_ed25519_key"
awk -v port="$port" '{print "[127.0.0.1]:" port " " $1 " " $2}' "$ROOT/ssh/ssh_host_ed25519_key.pub" \
    | as_agent sh -c 'umask 077 && cat > "$1"' sh "$agent_home/.ssh/$AGENT_KNOWN_HOSTS"

cat > "$ROOT/ssh/sshd_config" <<EOF
# Loopback-only SSH boundary for the agent. Managed by scripts/install.sh.
Port $port
ListenAddress 127.0.0.1
HostKey $ROOT/ssh/ssh_host_ed25519_key
PidFile /run/secretops-local-sshd.pid
AuthorizedKeysFile $ROOT/ssh/authorized_keys
AllowUsers $BROKER_USER
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
UsePAM no
PermitRootLogin no
PermitUserEnvironment no
AcceptEnv none
AllowAgentForwarding no
AllowTcpForwarding no
AllowStreamLocalForwarding no
X11Forwarding no
PermitTunnel no
PermitTTY no
StrictModes yes
LogLevel VERBOSE
EOF
install -d -o root -g root -m 0755 /run/sshd
/usr/sbin/sshd -t -f "$ROOT/ssh/sshd_config"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=secretops local broker SSH boundary (loopback only)
After=network.target

[Service]
Type=simple
ExecStartPre=/usr/bin/install -d -o root -g root -m 0755 /run/sshd
ExecStart=/usr/sbin/sshd -D -e -f $ROOT/ssh/sshd_config
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# The human's client configuration for `secretops add`, unless one exists.
human_home=$(getent passwd "$human" | cut -d: -f6)
if [[ -d $human_home ]] && ! runuser -u "$human" -- test -e "$human_home/.config/secretops/client.yaml"; then
    runuser -u "$human" -- mkdir -p -m 0700 "$human_home/.config/secretops"
    printf 'brokers:\n  local:\n    sudo_user: %s\n    broker_command: /usr/local/bin/secretops-broker\n' \
        "$BROKER_USER" | runuser -u "$human" -- sh -c 'umask 077 && cat > "$1"' sh \
        "$human_home/.config/secretops/client.yaml"
fi

write_info
trap - ERR

runuser -u "$BROKER_USER" -- /usr/local/bin/secretops-broker version >/dev/null
if [[ $start == 1 ]]; then
    systemctl daemon-reload
    systemctl enable --now "$UNIT"
    # Type=simple: systemctl returns before sshd listens.
    for _ in {1..20}; do
        [[ -n $(ss -ltnH "sport = :$port") ]] && break
        sleep 0.5
    done
    as_agent env HOME="$agent_home" /usr/local/bin/secretops-local version >/dev/null \
        || die "the agent cannot reach the broker; check: systemctl status $UNIT"
    echo "PASS: $agent_user reaches the broker through the restricted SSH key."
else
    echo "SSH service written but not started (--no-start): systemctl enable --now $UNIT"
fi
if [[ $agent_user == "$human" ]]; then
    echo "WARNING: the agent runs as your own account. The approval boundary is then a"
    echo "         convention, not enforced; see docs/INSTALL.md, 'Accounts'."
fi
cat <<EOF
Installed secretops $(info_value version). Vault credentials are not configured yet.
Next (human, in this terminal):
  sudo secretops-configure
  sudo -u $BROKER_USER gh auth login --hostname github.com   (from cd /, for GitHub sync)
EOF
if [[ -n $create_agent ]]; then
    cat <<EOF
Then set up the agent account $agent_user (docs/INSTALL.md, 'Accounts'):
  sudo -iu $agent_user
  # there: install and log in to your coding agent following its vendor's
  # instructions, and clone your project; start the agent itself from a
  # separate login of $agent_user, not from your own terminal
EOF
fi
echo "Then follow docs/INSTALL.md, 'First workflow'."
