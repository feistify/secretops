"""examples/recipes/ stays true: every example file produces a saved plan."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((REPO / "examples" / "recipes").glob("*.yaml"))
# Settings a developer may have exported; the run uses the packaged policy and
# the dummy vault connection from conftest.py instead.
DEVELOPER_SETTINGS = (
    "SECRETOPS_POLICY_PATH",
    "INFISICAL_DOMAIN",
    "SECRETOPS_INFISICAL_BIN",
    "SECRETOPS_GH_BIN",
    "SECRETOPS_PROVIDER_TIMEOUT_SECONDS",
)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_example_file_produces_a_saved_plan(path, tmp_path, monkeypatch):
    for name in DEVELOPER_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    example = yaml.safe_load(path.read_text(encoding="utf-8"))
    if example["action"] == "vault.secret.import_file":
        # The example names a placeholder path; planning reads names from a real file.
        source = tmp_path / "import.env"
        source.write_text("IMPORTED_NAME=not-a-real-secret\n", encoding="utf-8")
        source.chmod(0o600)
        example = {**example, "source_file": str(source)}
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(yaml.safe_dump(example), encoding="utf-8")
    plan_dir = tmp_path / "plans"
    monkeypatch.setenv("SECRETOPS_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    result = subprocess.run(
        ["uv", "run", "secretops", "plan", "--file", str(recipe), "--save"],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "plan_id:" in result.stdout
    plans = list(plan_dir.glob("*.json"))
    assert len(plans) == 1
    assert json.loads(plans[0].read_text(encoding="utf-8"))["action"] == example["action"]
