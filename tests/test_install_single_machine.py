"""install-single-machine.sh: argument checks and pinned versions, before anything changes.

The full run (vault, CLI, broker, bootstrap) needs root and a host; it is
recorded in docs/validation/self-hosted-infisical.md.
"""

import re
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


def test_without_root_it_stops(tmp_path):
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


def test_it_never_installs_a_container_runtime():
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("nerdctl-full", "containerd.io", "docker-ce", "get.docker.com", "systemctl enable"):
        assert forbidden not in code
