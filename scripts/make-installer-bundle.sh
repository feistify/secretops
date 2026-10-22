#!/bin/sh
# make-installer-bundle.sh: build the installer bundle for one release.
#
#   sh scripts/make-installer-bundle.sh dist/secretops-0.1.3-py3-none-any.whl [OUTDIR]
#
# Writes OUTDIR/secretops-VERSION-installer.tar.gz (default OUTDIR: the
# wheel's directory). The bundle holds install.sh, the wrappers, the pinned
# dependency hashes and secretops-release.txt, which pins the program to
# this wheel's SHA-256. The installer then takes the program from PyPI, so
# publish exactly this wheel there. Same inputs and SOURCE_DATE_EPOCH give
# the same archive.
set -eu

die() {
    echo "make-installer-bundle: $*" >&2
    exit 1
}

wheel=${1:-}
[ -f "$wheel" ] || die "usage: make-installer-bundle.sh WHEEL [OUTDIR]"
name=$(basename -- "$wheel")
case $name in
    secretops-*-py3-none-any.whl) ;;
    *) die "not a secretops wheel: $name" ;;
esac
version=${name#secretops-}
version=${version%-py3-none-any.whl}
out=${2:-$(dirname -- "$wheel")}
src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sha256=$(sha256sum -- "$wheel" | cut -d' ' -f1)

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
dir=$stage/secretops-$version-installer
mkdir "$dir"
chmod 0755 "$dir"
for file in install.sh secretops-broker secretops-agent secretops-local secretops-uninstall; do
    cp -- "$src/$file" "$dir/$file"
    chmod 0755 "$dir/$file"
done
cp -- "$src/broker-requirements.txt" "$dir/broker-requirements.txt"
printf 'secretops==%s --hash=sha256:%s\n' "$version" "$sha256" > "$dir/secretops-release.txt"
cat > "$dir/README.txt" <<EOF
secretops $version installer

Installs a local secretops broker on Ubuntu 24.04. Run in your own
terminal, not in a coding agent session:

  sudo ./install.sh --agent-user AGENT_ACCOUNT --infisical-domain https://app.infisical.com
  sudo secretops-configure

Upgrade an existing broker with this version:

  sudo ./install.sh --upgrade

The program is downloaded from PyPI and checked against the SHA-256 in
secretops-release.txt. Guide: https://github.com/feistify/secretops/blob/v$version/docs/INSTALL.md
EOF
chmod 0644 "$dir/broker-requirements.txt" "$dir/secretops-release.txt" "$dir/README.txt"

mkdir -p -- "$out"
bundle=$out/secretops-$version-installer.tar.gz
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@${SOURCE_DATE_EPOCH:-0}" \
    -C "$stage" -cf - "secretops-$version-installer" | gzip -n -9 > "$bundle"
echo "$bundle"
