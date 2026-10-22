"""Recipes are checked field by field before anything is planned."""

import os
import subprocess
from pathlib import Path

import pytest

from secretops.recipes import RECIPE_FIELDS, RecipeError, load_recipe

REPO = Path(__file__).resolve().parent.parent
SYNC = (
    "action: github.env_secret.sync_many\nenv: dev\ngithub_owner: example-owner\n"
    "github_repo: example-app\ngithub_environment: dev\n"
    "secrets:\n  - source: DUMMY_SECRET\n    target: DUMMY_SECRET\n"
)
GENERATE = (
    "action: vault.secret.generate\nsecret_name: DUMMY_SECRET\nenv: dev\n"
    "generator:\n  type: password\n  length: 32\n"
)


def write(tmp_path, text):
    path = tmp_path / "recipe.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text, message",
    [
        (SYNC.replace("github_environment:", "github_env:"), "unknown field github_env for github.env_secret.sync_many; did you mean github_environment?"),
        (SYNC + "value: anything\n", "unknown field value for github.env_secret.sync_many"),
        (SYNC.replace("github_repo: example-app\n", ""), "missing field github_repo"),
        (SYNC.replace("secrets:\n  - source: DUMMY_SECRET\n    target: DUMMY_SECRET\n", "secrets: []\n"), "secrets must be a non-empty list"),
        (SYNC.replace("  - source: DUMMY_SECRET\n    target: DUMMY_SECRET\n", "  - name: DUMMY_SECRET\n"), "secrets entry 1 must have exactly source and target"),
        (GENERATE.replace("type: password", "type: pasword"), "generator.type must be password"),
        (GENERATE.replace("length: 32", "length: 0"), "generator.length must be a whole number"),
        (GENERATE.replace("length: 32", "length: '32'"), "generator.length must be a whole number"),
        (GENERATE.replace("  length: 32\n", "  length: 32\n  charset: all\n"), "generator takes only type and length"),
        (GENERATE + "create_mode: overwrite\n", "create_mode must be create_only"),
        ("action: vault.secret.copy_many\ntarget_env: staging\nsecrets:\n  - source: A_NAME\n    target: A_NAME\n", "missing field source_env or env"),
        ("secret_name: DUMMY_SECRET\nenv: dev\n", "missing field action"),
        ("- just\n- a list\n", "a recipe must be a YAML mapping"),
        ("action: [unclosed\n", "recipe is not valid YAML"),
    ],
)
def test_invalid_recipes_are_refused_with_the_field_named(tmp_path, text, message):
    with pytest.raises(RecipeError) as error:
        load_recipe(write(tmp_path, text))

    assert message in str(error.value)


def test_messages_do_not_repeat_what_was_written(tmp_path):
    for text in (GENERATE + "create_mode: sk_live_not_a_real_value\n", GENERATE.replace("type: password", "type: sk_live_not_a_real_value")):
        with pytest.raises(RecipeError) as error:
            load_recipe(write(tmp_path, text))
        assert "sk_live_not_a_real_value" not in str(error.value)


def test_missing_recipe_file_is_a_recipe_error(tmp_path):
    with pytest.raises(RecipeError, match="cannot read recipe"):
        load_recipe(tmp_path / "missing.yaml")


def test_actions_without_a_field_list_are_left_to_policy(tmp_path):
    assert load_recipe(write(tmp_path, "action: secret.reveal\nsecret_name: DUMMY_SECRET\nenv: dev\n")).action == "secret.reveal"


@pytest.mark.parametrize("example", sorted((REPO / "examples" / "recipes").glob("*.yaml")), ids=lambda path: path.name)
def test_every_shipped_example_passes_the_field_check(example):
    assert load_recipe(example).action in RECIPE_FIELDS


def test_cli_prints_one_line_instead_of_a_traceback(tmp_path):
    plan_dir = tmp_path / "plans"
    env = os.environ.copy()
    env["SECRETOPS_PLAN_DIR"] = str(plan_dir)
    env["SECRETOPS_AUDIT_PATH"] = str(tmp_path / "audit.jsonl")

    result = subprocess.run(
        ["uv", "run", "secretops", "plan", "--file", str(write(tmp_path, SYNC.replace("github_environment:", "github_env:"))), "--save"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "FAIL invalid recipe: unknown field github_env" in result.stdout
    assert "did you mean github_environment?" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert not plan_dir.exists() or not any(plan_dir.iterdir())
