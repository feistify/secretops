"""install-single-machine.sh: argument checks and pinned versions, before anything changes.

The full run (vault, CLI, broker, bootstrap) needs root and a host; it is
recorded in docs/validation/self-hosted-infisical.md.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "examples" / "infisical" / "install-single-machine.sh"


def run(*args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "--host is required"),
        (["--host", "vault;id", "--create-agent-user", "agent"], "--host must be a DNS name"),
        (["--host", "vault.example", "--port", "0", "--create-agent-user", "agent"], "--port must be a number"),
        (["--host", "vault.example", "--port", "70000", "--create-agent-user", "agent"], "--port must be a number"),
        (["--host", "vault.example", "--port", "x", "--create-agent-user", "agent"], "--port must be a number"),
        (["--host", "vault.example", "--dir", "relative", "--create-agent-user", "agent"], "--dir must be an absolute"),
        (["--host", "vault.example", "--dir", "/", "--create-agent-user", "agent"], "--dir must be an absolute"),
        (["--host", "vault.example"], "give exactly one of"),
        (["--host", "vault.example", "--create-agent-user", "a", "--agent-user", "b"], "give exactly one of"),
        (["--host", "vault.example", "--bogus"], "unknown option"),
    ],
)
def test_bad_arguments_stop_before_anything(args, message):
    result = run(*args)
    assert result.returncode == 1
    assert message in result.stderr


def test_without_root_it_stops():
    result = run("--host", "vault.example", "--create-agent-user", "agent")
    assert result.returncode == 1
    assert "run with sudo" in result.stderr


def test_help():
    result = run("--help")
    assert result.returncode == 0
    assert "--create-agent-user NAME" in result.stdout


def test_pinned_secretops_version_is_the_package_version():
    text = SCRIPT.read_text()
    pinned = re.search(r"^SECRETOPS_VERSION=(\S+)$", text, re.M).group(1)
    project = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert pinned == project


def test_pinned_infisical_cli_version_is_a_tested_one():
    pinned = re.search(r"^INFISICAL_CLI_VERSION=(\S+)$", SCRIPT.read_text(), re.M).group(1)
    assert f"`{pinned}`" in (ROOT / "docs" / "LIMITATIONS.md").read_text()


def test_it_never_installs_or_restarts_a_container_runtime():
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("nerdctl-full", "containerd.io", "docker-ce", "docker.io", "get.docker.com",
                      "systemctl enable", "systemctl restart", "systemctl start"):
        assert forbidden not in code
    assert not re.search(r"apt_get install[^\n]*(containerd|docker|nerdctl|runc|cni)", code)
    # Every package install keeps the system's other packages.
    for line in re.findall(r"apt_get install[^\n]*", code):
        assert "--no-remove" in line


def test_nerdctl_uses_the_default_namespace():
    assert 'compose="nerdctl --namespace default compose"' in SCRIPT.read_text()


def _function(name):
    match = re.search(rf"^{name}\(\) {{\n.*?^}}\n", SCRIPT.read_text(), re.M | re.S)
    assert match
    return match.group(0)


@pytest.fixture
def keys(tmp_path):
    """Two made-up signing keys in one armored file, like a download with an extra key."""
    if not shutil.which("gpg"):
        pytest.skip("gpg is not installed")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)

    def gpg(*args):
        return subprocess.run(["gpg", "--batch", "--homedir", str(home), *args],
                              capture_output=True, text=True, check=True).stdout

    fingerprints = []
    for name in ("Pinned <pinned@example.test>", "Extra <extra@example.test>"):
        gpg("--passphrase", "", "--quick-gen-key", name, "ed25519", "sign", "1d")
    for line in gpg("--with-colons", "--list-keys").splitlines():
        if line.startswith("fpr:") and len(fingerprints) < 2 and line.split(":")[9] not in fingerprints:
            fingerprints.append(line.split(":")[9])
    both = tmp_path / "both.asc"
    both.write_text(gpg("--armor", "--export"))
    only_extra = tmp_path / "extra.asc"
    only_extra.write_text(gpg("--armor", "--export", fingerprints[1]))
    yield fingerprints, both, only_extra
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=False)


def _pinned_key(fingerprint, source, out):
    body = f'INFISICAL_KEY_FINGERPRINT={fingerprint}\n{_function("pinned_key")}pinned_key "$1" "$2"\n'
    return subprocess.run(["bash", "-c", body, "test", str(source), str(out)], capture_output=True, text=True)


def _pub_keys(path):
    listing = subprocess.run(["gpg", "--batch", "--show-keys", "--with-colons", str(path)],
                             capture_output=True, text=True, check=True).stdout
    return [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]


def test_only_the_pinned_key_reaches_the_keyring(keys, tmp_path):
    (pinned, extra), both, _ = keys
    out = tmp_path / "keyring.gpg"
    assert _pinned_key(pinned, both, out).returncode == 0
    listed = _pub_keys(out)
    assert listed[0] == pinned
    assert extra not in listed


def test_a_download_without_the_pinned_key_is_refused(keys, tmp_path):
    (pinned, _), _, only_extra = keys
    assert _pinned_key(pinned, only_extra, tmp_path / "keyring.gpg").returncode == 1
