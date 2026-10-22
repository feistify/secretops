"""docs/RECIPES.md stays true: every action is documented and every example plans."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from secretops.policy import load_policy, require_environment_allowed

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "docs" / "RECIPES.md"
# Allowed by the default policy, but not recipe actions an agent should use:
# vault.read.plan has no recipe form, github.env_secret.plan is a dry-run alias.
NOT_DOCUMENTED = {"vault.read.plan", "github.env_secret.plan"}


def reference_examples() -> list[dict]:
    blocks = re.findall(r"```yaml\n(.*?)```", REFERENCE.read_text(encoding="utf-8"), re.S)
    return [yaml.safe_load(block) for block in blocks]


def reference_sections() -> list[str]:
    return re.findall(r"(?m)^## ([a-z_]+\.[a-z_.]+)$", REFERENCE.read_text(encoding="utf-8"))


def documented_actions() -> set[str]:
    return set(load_policy().allowed_actions) - NOT_DOCUMENTED


def test_every_allowed_action_has_a_section_and_one_example():
    examples = [example["action"] for example in reference_examples()]

    assert sorted(reference_sections()) == sorted(documented_actions())
    assert sorted(examples) == sorted(documented_actions())


def test_overview_table_lists_every_section():
    table = re.findall(r"(?m)^\| \[`([a-z_.]+)`\]\(#", REFERENCE.read_text(encoding="utf-8"))

    assert table == reference_sections()


@pytest.mark.parametrize("example", reference_examples(), ids=lambda example: example["action"])
def test_example_environments_are_allowed_by_default_policy(example):
    environments = [
        example.get(field)
        for field in ("env", "source_env", "target_env", "github_environment")
        if example.get(field)
    ]

    assert environments
    for environment in environments:
        require_environment_allowed(example["action"], environment)


@pytest.mark.parametrize("example", reference_examples(), ids=lambda example: example["action"])
def test_example_produces_a_saved_plan(example, tmp_path):
    if example["action"] == "vault.secret.import_file":
        source = tmp_path / "import.env"
        source.write_text("IMPORTED_NAME=not-a-real-secret\n", encoding="utf-8")
        source.chmod(0o600)
        example = {**example, "source_file": str(source)}
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(yaml.safe_dump(example), encoding="utf-8")
    plan_dir = tmp_path / "plans"
    env = os.environ.copy()
    env["SECRETOPS_PLAN_DIR"] = str(plan_dir)
    env["SECRETOPS_AUDIT_PATH"] = str(tmp_path / "audit.jsonl")

    result = subprocess.run(
        ["uv", "run", "secretops", "plan", "--file", str(recipe), "--save"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "plan_id:" in result.stdout
    plans = list(plan_dir.glob("*.json"))
    assert len(plans) == 1
    assert json.loads(plans[0].read_text(encoding="utf-8"))["action"] == example["action"]
