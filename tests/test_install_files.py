import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_broker_requirements_match_the_lockfile():
    exported = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt",
         "--no-header"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    shipped = (SCRIPTS / "broker-requirements.txt").read_text()
    body = "\n".join(line for line in shipped.splitlines() if not line.startswith("#")) + "\n"
    assert body.strip() == "\n".join(
        line for line in exported.splitlines() if not line.startswith("#")
    ).strip(), "run: uv export --no-dev --no-emit-project --format requirements-txt --output-file scripts/broker-requirements.txt"


def test_shipped_requirements_are_hash_pinned():
    lines = [line for line in (SCRIPTS / "broker-requirements.txt").read_text().splitlines()
             if line and not line.startswith(("#", " "))]
    assert lines
    assert all("==" in line and line.endswith("\\") for line in lines)


@pytest.mark.parametrize("script", ["install.sh", "secretops-local"])
def test_install_scripts_parse(script):
    shell = "bash" if script.endswith(".sh") else "sh"
    subprocess.run([shell, "-n", str(SCRIPTS / script)], check=True)
