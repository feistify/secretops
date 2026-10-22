#!/bin/sh
# Runs scripts/install.sh in a throwaway Ubuntu 24.04 container and checks
# the installed boundary. Needs Docker and network access (apt, PyPI).
# Not part of pytest; run before changing the installer:
#
#   uv build && sh tests/test_install_container.sh
#
# The container has no systemd, so the installer runs with --no-start and
# the test starts the dedicated sshd itself. A fake infisical CLI stands in
# for the vault; no real provider or credential is used.
set -eu
REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WHEEL=$(ls "$REPO"/dist/secretops-*-py3-none-any.whl | head -n 1)
exec docker run --rm -i -v "$REPO:/src:ro" -e WHEEL="/src/dist/$(basename "$WHEEL")" \
    ubuntu:24.04 bash -s <<'EOF'
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends python3-venv openssh-server openssh-client \
    sudo procps iproute2 ca-certificates >/dev/null
useradd -m -s /bin/bash human
useradd -m -s /bin/bash agent
# Records whether the configured token reached the vault CLI, then fails.
printf '#!/bin/sh\n[ "$INFISICAL_TOKEN" = synthetic-token-2 ] && : > /tmp/token-seen\necho fake infisical >&2\nexit 1\n' > /usr/bin/infisical
chmod 0755 /usr/bin/infisical

fail=0
check() { # label, expected (pass|deny), command...
    label=$1 expected=$2
    shift 2
    # stdin closed: ssh would otherwise read the rest of this script.
    if out=$("$@" 2>&1 </dev/null); then got=pass; else got=deny; fi
    if [ "$got" = "$expected" ]; then
        echo "PASS: $label"
    else
        echo "FAIL: $label (got $got)"; echo "$out" | head -n 5; fail=1
    fi
}
as_agent() { runuser -u agent -- env HOME=/home/agent "$@"; }

cd /src
check "install refuses without the agent user flag" deny env SUDO_USER=human bash scripts/install.sh --wheel "$WHEEL"
runuser -u agent -- sh -c 'cp /usr/bin/infisical /home/agent/own-infisical && ln -s own-infisical /home/agent/evil-link'
check "install refuses a link to an agent-owned binary" deny env SUDO_USER=human bash scripts/install.sh \
    --wheel "$WHEEL" --agent-user agent --infisical-domain http://127.0.0.1:8086 \
    --infisical-bin /home/agent/evil-link --no-start
# The agent controls this link; the installer must store its target.
runuser -u agent -- ln -s /usr/bin/infisical /home/agent/infisical
env SUDO_USER=human bash scripts/install.sh --wheel "$WHEEL" --agent-user agent \
    --infisical-domain http://127.0.0.1:8086 --infisical-bin /home/agent/infisical --no-start
/usr/sbin/sshd -f /opt/secretops/ssh/sshd_config

check "second install refuses to overwrite" deny env SUDO_USER=human bash scripts/install.sh \
    --wheel "$WHEEL" --agent-user agent --infisical-domain http://127.0.0.1:8086 --no-start
check "broker.env is broker-owned 0600" pass test "$(stat -c '%U %a' /opt/secretops/broker.env)" = "secretops-broker 600"
check "application is root-owned and not writable" pass sh -c \
    '! find /opt/secretops/venv /opt/secretops/bin /opt/secretops/policy.yaml ! -type l \( ! -user root -o -perm /022 \) | grep -q .'
check "state directories are private" pass test "$(stat -c '%U %a' /opt/secretops/plans /opt/secretops/audit /opt/secretops/recipes | sort -u)" = "secretops-broker 700"
check "trusted binary stored by its resolved path" pass grep -qx "SECRETOPS_INFISICAL_BIN=/usr/bin/infisical" /opt/secretops/broker.env
check "broker.env has no token yet" pass sh -c '! grep -q INFISICAL_TOKEN /opt/secretops/broker.env'
check "human client config written" pass test -f /home/human/.config/secretops/client.yaml

check "agent: version" pass as_agent secretops-local version
check "agent: actions" pass as_agent secretops-local actions
cp examples/recipes/github-env-secret-verify-many.yaml /tmp/verify.yaml
chmod 0644 /tmp/verify.yaml
check "agent: write-recipe" pass sh -c 'runuser -u agent -- env HOME=/home/agent secretops-local write-recipe verify.yaml < /tmp/verify.yaml'
check "agent: plan --save" pass as_agent secretops-local plan --file verify.yaml --save
check "agent: approve-plan denied" deny as_agent secretops-local approve-plan --id abc
check "agent: reject-plan denied" deny as_agent secretops-local reject-plan --id abc
check "agent: add endpoints denied" deny as_agent secretops-local prepare-add
check "agent: shell denied" deny as_agent secretops-local sh -c id
check "agent: interactive shell denied" deny as_agent sh -c 'ssh -F /dev/null -i ~/.ssh/secretops_local_agent_ed25519 -o BatchMode=yes -o UserKnownHostsFile=~/.ssh/secretops_local_known_hosts -tt -p 22222 secretops-broker@127.0.0.1 </dev/null'
# A remote forward is refused by the server at once; ExitOnForwardFailure
# then exits 255 (a timeout would be 124).
check "agent: port forwarding denied" pass as_agent sh -c 'timeout 10 ssh -F /dev/null -i ~/.ssh/secretops_local_agent_ed25519 -o BatchMode=yes -o UserKnownHostsFile=~/.ssh/secretops_local_known_hosts -o ExitOnForwardFailure=yes -R 18080:127.0.0.1:22 -N -p 22222 secretops-broker@127.0.0.1; test $? = 255'
check "agent: cannot read broker.env" deny as_agent cat /opt/secretops/broker.env
check "agent: cannot list plans" deny as_agent ls /opt/secretops/plans
check "agent: cannot sudo" deny as_agent sudo -n -u secretops-broker true
sed -i 's/^\(\[127.0.0.1\]:22222 ssh-ed25519 \).*/\1AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl/' /home/agent/.ssh/secretops_local_known_hosts
check "agent: changed host key refused" deny as_agent secretops-local version

check "configure refuses without a terminal" deny sh -c 'echo x | secretops-configure --token-only'
check "vault read fails safely without a token" deny runuser -u secretops-broker -- /usr/local/bin/secretops-broker check-vault-read --name DEMO --env dev
# Answers each prompt only after it appears, as a person would, so the
# hidden token prompt has turned echo off before the token is typed.
cat > /tmp/answer.py <<'PY'
import os, pty, select, sys
answers, command = sys.argv[1].split(","), sys.argv[2:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(command[0], command)
out = b""
def read():
    global out
    if not select.select([fd], [], [], 15)[0]:
        sys.exit("timeout")
    try:
        chunk = os.read(fd, 1024)
    except OSError:
        chunk = b""
    out += chunk
    return chunk
for number, answer in enumerate(answers, start=1):
    while out.count(b": ") < number:
        if not read():
            break
    os.write(fd, answer.encode() + b"\r")
while read():
    pass
sys.stdout.write(out.decode(errors="replace"))
sys.exit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
PY
check "configure through a terminal" pass sh -c 'python3 /tmp/answer.py http://127.0.0.1:8086,project-1,/,synthetic-token-1 secretops-configure > /tmp/configure.out'
check "token renewal through a terminal" pass sh -c 'python3 /tmp/answer.py synthetic-token-2 secretops-configure --token-only >> /tmp/configure.out'
check "configure output shows no token" pass sh -c '! grep -q synthetic-token /tmp/configure.out'
check "configured file stays broker-owned 0600" pass test "$(stat -c '%U %a' /opt/secretops/broker.env)" = "secretops-broker 600"
runuser -u secretops-broker -- /usr/local/bin/secretops-broker check-vault-read --name DEMO --env dev >/dev/null 2>&1 </dev/null || true
check "renewed token reaches the vault CLI" pass test -e /tmp/token-seen

# Backup and the restore archive checks from docs/OPERATIONS.md.
tar -C /opt/secretops -czf /root/good.tar.gz plans audit policy.yaml
restore_checks() {
    tar -tvzf "$1" | awk '$1 !~ /^[-d]/'
    tar -tzf "$1" | grep -Ev '^(plans|audit)/([^/]+)?$|^policy\.yaml$'
}
check "restore checks accept a normal backup" pass sh -c "$(declare -f restore_checks); test -z \"\$(restore_checks /root/good.tar.gz)\""
mkdir -p /root/bad/audit && ln -s /etc/shadow /root/bad/audit/secretops.jsonl && cp /opt/secretops/policy.yaml /root/bad/
tar -C /root/bad -czf /root/bad.tar.gz audit policy.yaml
check "restore checks catch a linked audit file" pass sh -c "$(declare -f restore_checks); test -n \"\$(restore_checks /root/bad.tar.gz)\""
mkdir -p /root/odd && cp /opt/secretops/policy.yaml /root/odd/broker.env
tar -C /root/odd -czf /root/odd.tar.gz broker.env
check "restore checks catch unexpected paths" pass sh -c "$(declare -f restore_checks); test -n \"\$(restore_checks /root/odd.tar.gz)\""

pkill -f 'sshd -f /opt/secretops' || true
check "upgrade replaces the application" pass bash scripts/install.sh --upgrade --wheel "$WHEEL"
check "upgrade kept the previous application" pass test -x /opt/secretops/venv.previous/bin/python
check "upgrade kept broker.env" pass test "$(stat -c '%U %a' /opt/secretops/broker.env)" = "secretops-broker 600"
check "upgraded broker runs" pass runuser -u secretops-broker -- /usr/local/bin/secretops-broker version

# Installer bundle: the same installer, pinned to one wheel by its hash.
bundle=$(sh scripts/make-installer-bundle.sh "$WHEEL" /tmp/bundle)
tar -C /tmp -xzf "$bundle"
bdir=/tmp/$(basename "$bundle" .tar.gz)
check "bundle upgrade with the pinned wheel" pass bash "$bdir/install.sh" --upgrade --wheel "$WHEEL"
mkdir /tmp/other && cp "$WHEEL" /tmp/other/ && printf x >> "/tmp/other/$(basename "$WHEEL")"
check "bundle refuses a wheel with another hash" deny bash "$bdir/install.sh" --upgrade --wheel "/tmp/other/$(basename "$WHEEL")"
# The published release from PyPI, pinned like a real bundle.
python3 - > /tmp/pypi-pin.txt <<'PY'
import json, urllib.request
info = json.load(urllib.request.urlopen("https://pypi.org/pypi/secretops/json", timeout=30))
version = info["info"]["version"]
wheel = next(f for f in info["urls"] if f["filename"].endswith("-py3-none-any.whl"))
print(f"secretops=={version} --hash=sha256:{wheel['digests']['sha256']}")
PY
pypi_version=$(sed 's/^secretops==\([^ ]*\) .*/\1/' /tmp/pypi-pin.txt)
cp -r "$bdir" /tmp/pypi-bundle && cp /tmp/pypi-pin.txt /tmp/pypi-bundle/secretops-release.txt
check "bundle upgrade from PyPI with the pinned hash" pass bash /tmp/pypi-bundle/install.sh --upgrade
check "installed version is the pinned release" pass grep -qx "version=$pypi_version" /opt/secretops/install-info
cp -r "$bdir" /tmp/bad-bundle
sed "s/sha256:.*/sha256:$(printf '0%.0s' $(seq 64))/" /tmp/pypi-pin.txt > /tmp/bad-bundle/secretops-release.txt
check "PyPI download with a wrong pinned hash refused" deny bash /tmp/bad-bundle/install.sh --upgrade
check "broker still runs after the refused upgrade" pass runuser -u secretops-broker -- /usr/local/bin/secretops-broker version
check "refused download left no partial environment" pass test ! -e /opt/secretops/venv.new

[ "$fail" = 0 ] && echo "All installer checks passed." || { echo "Installer checks failed."; exit 1; }
EOF
