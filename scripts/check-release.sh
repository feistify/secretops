#!/bin/sh
# check-release.sh: check a published release end to end, as a maintainer.
#
#   sh scripts/check-release.sh 0.1.4
#
# Needs a logged-in GitHub CLI with `gh attestation verify --source-ref`
# (tested with 2.102.0; set GH=/path/to/gh to use another binary), curl,
# python3, uv and Docker. Waits until PyPI's index lists the version, then
# checks: the release files against SHA256SUMS; each file's attestation
# signature, signed by release.yml of this repository for the tag; that the bundle pins the release wheel; that
# PyPI has the same wheel and sdist with provenance from publish.yml; that
# a clean venv installs and runs the version; and that the bundle installs
# a broker in a clean Ubuntu container, refuses a recipe with a wrong
# field, and uninstalls. Does not change GitHub or PyPI.
set -eu

version=${1:?usage: check-release.sh VERSION}
GH=${GH:-gh}
repo=feistify/secretops
tag=v$version
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fail() {
    echo "FAIL: $*" >&2
    exit 1
}

"$GH" attestation verify --help 2>/dev/null | grep -q -- '--source-ref' \
    || fail "needs a GitHub CLI with 'gh attestation verify --source-ref' (found: $("$GH" --version 2>/dev/null | head -n 1)); install a newer gh or set GH=/path/to/gh"

echo "== PyPI index"
tries=0
until curl -fsS -H 'Accept: application/vnd.pypi.simple.v1+json' https://pypi.org/simple/secretops/ \
    | grep -q "secretops-$version-py3-none-any.whl"; do
    tries=$((tries + 1))
    [ "$tries" -lt 40 ] || fail "PyPI does not list $version after 10 minutes"
    sleep 15
done
echo "PyPI lists $version"

echo "== Release files"
"$GH" release download "$tag" --repo "$repo" --dir "$work/release"
(cd "$work/release" && sha256sum --check --strict SHA256SUMS) || fail "files do not match SHA256SUMS"
for file in "secretops-$version-installer.tar.gz" "secretops-$version-py3-none-any.whl" "secretops-$version.tar.gz"; do
    "$GH" attestation verify "$work/release/$file" --repo "$repo" \
        --signer-workflow "$repo/.github/workflows/release.yml" --source-ref "refs/tags/$tag" \
        --deny-self-hosted-runners >/dev/null || fail "attestation of $file does not verify"
    echo "attestation verified: $file"
done
wheel_sha=$(sha256sum "$work/release/secretops-$version-py3-none-any.whl" | cut -d' ' -f1)
tar -xzf "$work/release/secretops-$version-installer.tar.gz" -O "secretops-$version-installer/secretops-release.txt" \
    | grep -qx "secretops==$version --hash=sha256:$wheel_sha" || fail "bundle does not pin the release wheel"
echo "bundle pins the release wheel"

echo "== PyPI files"
curl -fsS "https://pypi.org/pypi/secretops/$version/json" | python3 -c '
import json, sys
files = {f["filename"]: f["digests"]["sha256"] for f in json.load(sys.stdin)["urls"]}
sums = dict(line.split()[::-1] for line in open(sys.argv[1]) if "installer" not in line and "SHA256SUMS" not in line)
assert files == sums, (files, sums)
' "$work/release/SHA256SUMS" || fail "PyPI files differ from the release"
for file in "secretops-$version-py3-none-any.whl" "secretops-$version.tar.gz"; do
    curl -fsS "https://pypi.org/integrity/secretops/$version/$file/provenance" | python3 -c '
import json, sys
publisher = json.load(sys.stdin)["attestation_bundles"][0]["publisher"]
assert (publisher["repository"], publisher["workflow"], publisher.get("environment")) == ("feistify/secretops", "publish.yml", "pypi"), publisher
' || fail "PyPI provenance for $file"
done
echo "PyPI files equal the release, provenance from publish.yml"

echo "== Clean venv"
uv venv -q "$work/venv"
VIRTUAL_ENV=$work/venv uv pip install -q --no-cache --refresh --index-url https://pypi.org/simple "secretops==$version"
[ "$("$work/venv/bin/secretops" version)" = "secretops $version" ] || fail "venv version"
echo "pip install prints secretops $version"

echo "== Bundle in a clean container"
docker run --rm -i -v "$work/release:/release:ro" -e VERSION="$version" ubuntu:24.04 bash -s <<'EOF' \
    || fail "bundle installation"
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends python3-venv openssh-server openssh-client \
    ca-certificates procps iproute2 >/dev/null 2>&1
useradd -m human
printf '#!/bin/sh\nexit 1\n' > /usr/bin/infisical && chmod 0755 /usr/bin/infisical
cd /tmp && tar -xzf "/release/secretops-$VERSION-installer.tar.gz" && cd "secretops-$VERSION-installer"
env SUDO_USER=human ./install.sh --create-agent-user agent --infisical-domain https://app.infisical.com \
    --no-start > /tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
grep -qx "version=$VERSION" /opt/secretops/install-info
/usr/sbin/sshd -f /opt/secretops/ssh/sshd_config
as_agent() { runuser -u agent -- env HOME=/home/agent "$@"; }
[ "$(as_agent secretops-local version </dev/null)" = "secretops $VERSION" ]
printf 'action: vault.read.check\nsecret_name: DEMO_KEY\nenvironment: dev\n' | as_agent secretops-local write-recipe typo.yaml >/dev/null
! as_agent secretops-local plan --file typo.yaml --save </dev/null > /tmp/plan.out 2>&1
grep -q 'FAIL invalid recipe: unknown field environment' /tmp/plan.out
pkill -f 'sshd -f /opt/secretops'
secretops-uninstall --yes --purge </dev/null >/dev/null
test ! -e /opt/secretops
echo "installed $VERSION, refused a wrong field, uninstalled"
EOF

echo "All release checks passed for $version."
