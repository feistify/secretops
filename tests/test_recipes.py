import os
import subprocess

import pytest

from secretops.recipes import load_recipe


DUMMY_SECRET_NAME = "DUMMY_SECRET"
DUMMY_SECRET_VALUE = "not-a-real-secret"
EXAMPLE_RECIPE = "examples/recipes/dummy-vault-check.yaml"
GITHUB_RECIPE = "examples/recipes/github-env-secret-plan.yaml"
GITHUB_WRITE_RECIPE = "examples/recipes/github-env-secret-write.yaml"
GITHUB_SYNC_MANY_RECIPE = "examples/recipes/github-env-secret-sync-many.yaml"
GITHUB_VERIFY_MANY_RECIPE = "examples/recipes/github-env-secret-verify-many.yaml"
GITHUB_BACKUP_SYNC_MANY_RECIPE = "examples/recipes/github-backup-secrets-sync-many.yaml"
GENERATE_RECIPE = "examples/recipes/vault-secret-generate.yaml"
ROTATE_RECIPE = "examples/recipes/vault-secret-rotate.yaml"
IMPORT_FILE_RECIPE = "examples/recipes/vault-secret-import-file.yaml"
COPY_MANY_RECIPE = "examples/recipes/vault-secret-copy-many.yaml"
DIFF_ENVS_RECIPE = "examples/recipes/vault-secret-diff-envs.yaml"


def run_secretops(
    *args: str, audit_path=None, path_prefix=None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env[DUMMY_SECRET_NAME] = DUMMY_SECRET_VALUE
    if audit_path is not None:
        env["SECRETOPS_AUDIT_PATH"] = str(audit_path)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
        fake_infisical = os.path.join(str(path_prefix), "infisical")
        if os.path.exists(fake_infisical):
            env["SECRETOPS_INFISICAL_BIN"] = fake_infisical
        fake_gh = os.path.join(str(path_prefix), "gh")
        if os.path.exists(fake_gh):
            env["SECRETOPS_GH_BIN"] = fake_gh

    return subprocess.run(
        ["uv", "run", "secretops", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def assert_no_secret_leak(result: subprocess.CompletedProcess[str], audit_path=None) -> None:
    assert DUMMY_SECRET_VALUE not in result.stdout
    assert DUMMY_SECRET_VALUE not in result.stderr
    if audit_path is not None and audit_path.exists():
        assert DUMMY_SECRET_VALUE not in audit_path.read_text()


def write_fake_infisical(tmp_path):
    fake_infisical = tmp_path / "infisical"
    fake_infisical.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "child_env = os.environ.copy()\n"
        f"child_env[{DUMMY_SECRET_NAME!r}] = {DUMMY_SECRET_VALUE!r}\n"
        "raise SystemExit(subprocess.run(command, env=child_env).returncode)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return tmp_path


def test_recipe_loading():
    recipe = load_recipe(EXAMPLE_RECIPE)

    assert recipe.action == "vault.read.check"
    assert recipe.secret_name == DUMMY_SECRET_NAME
    assert recipe.env == "dev"


def test_generic_plan_works_for_dummy_vault_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops("plan", "--file", EXAMPLE_RECIPE, audit_path=audit_path)

    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "vault.read.check" in output
    assert DUMMY_SECRET_NAME in output
    assert "dev" in output
    assert "secret value will NOT be printed" in output
    assert_no_secret_leak(result, audit_path)


def test_plan_recipe_output_contains_safe_plan_fields(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops("plan-recipe", "--file", EXAMPLE_RECIPE, audit_path=audit_path)

    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "vault.read.check" in output
    assert DUMMY_SECRET_NAME in output
    assert "dev" in output
    assert "secret value will NOT be printed" in output
    assert_no_secret_leak(result, audit_path)


def test_plan_recipe_writes_safe_audit_event(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops("plan-recipe", "--file", EXAMPLE_RECIPE, audit_path=audit_path)

    assert result.returncode == 0
    assert audit_path.exists()
    assert "vault.read.check" in audit_path.read_text()
    assert DUMMY_SECRET_NAME in audit_path.read_text()
    assert_no_secret_leak(result, audit_path)


def test_plan_recipe_applies_policy(tmp_path):
    denied_recipe = tmp_path / "denied-recipe.yaml"
    audit_path = tmp_path / "secretops-test.jsonl"
    denied_recipe.write_text(
        "action: secret.reveal\nsecret_name: DUMMY_SECRET\nenv: dev\n",
        encoding="utf-8",
    )

    result = run_secretops("plan-recipe", "--file", str(denied_recipe), audit_path=audit_path)

    assert result.returncode != 0
    assert_no_secret_leak(result, audit_path)


def test_load_recipe_requires_allowed_action(tmp_path):
    recipe_path = tmp_path / "denied-recipe.yaml"
    recipe_path.write_text(
        "action: secret.reveal\nsecret_name: DUMMY_SECRET\nenv: dev\n",
        encoding="utf-8",
    )
    recipe = load_recipe(recipe_path)

    with pytest.raises(PermissionError):
        recipe.require_allowed()


def test_generic_execute_works_for_dummy_vault_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "execute",
        "--file",
        EXAMPLE_RECIPE,
        audit_path=audit_path,
        path_prefix=fake_bin,
    )

    assert result.returncode == 0
    assert f"OK secret exists: {DUMMY_SECRET_NAME}" in result.stdout
    assert "secret value was not printed" in result.stdout
    assert_no_secret_leak(result, audit_path)


def test_execute_recipe_succeeds_for_dummy_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "execute-recipe",
        "--file",
        EXAMPLE_RECIPE,
        audit_path=audit_path,
        path_prefix=fake_bin,
    )

    assert result.returncode == 0
    assert f"OK secret exists: {DUMMY_SECRET_NAME}" in result.stdout
    assert "secret value was not printed" in result.stdout
    assert_no_secret_leak(result, audit_path)


def test_execute_recipe_writes_safe_audit_event(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "execute-recipe",
        "--file",
        EXAMPLE_RECIPE,
        audit_path=audit_path,
        path_prefix=fake_bin,
    )

    assert result.returncode == 0
    assert audit_path.exists()
    assert "vault.read.check" in audit_path.read_text()
    assert DUMMY_SECRET_NAME in audit_path.read_text()
    assert "success" in audit_path.read_text()
    assert_no_secret_leak(result, audit_path)


def test_execute_recipe_unsupported_action_fails_safely(tmp_path):
    unsupported_recipe = tmp_path / "unsupported-recipe.yaml"
    audit_path = tmp_path / "secretops-test.jsonl"
    unsupported_recipe.write_text(
        "action: vault.read.plan\nsecret_name: DUMMY_SECRET\nenv: dev\n",
        encoding="utf-8",
    )

    result = run_secretops(
        "execute-recipe",
        "--file",
        str(unsupported_recipe),
        audit_path=audit_path,
    )

    assert result.returncode != 0
    assert "unsupported recipe action" in (result.stdout + result.stderr)
    assert_no_secret_leak(result, audit_path)


def test_vault_secret_generate_recipe_loading():
    recipe_text = open(GENERATE_RECIPE, encoding="utf-8").read()
    recipe = load_recipe(GENERATE_RECIPE)

    assert recipe.action == "vault.secret.generate"
    assert recipe.secret_name == "GENERATED_DUMMY_PASSWORD"
    assert recipe.env == "dev"
    assert recipe.generator == {"type": "password", "length": 32}
    assert recipe.create_mode == "create_only"
    assert "value:" not in recipe_text
    assert "secret_value" not in recipe_text
    assert DUMMY_SECRET_VALUE not in recipe_text


def test_vault_secret_rotate_recipe_loading():
    recipe_text = open(ROTATE_RECIPE, encoding="utf-8").read()
    recipe = load_recipe(ROTATE_RECIPE)

    assert recipe.action == "vault.secret.rotate"
    assert recipe.secret_name == "GENERATED_DUMMY_PASSWORD"
    assert recipe.env == "dev"
    assert recipe.generator == {"type": "password", "length": 32}
    assert recipe.rotation_mode == "replace_existing"
    assert "value:" not in recipe_text
    assert "secret_value" not in recipe_text
    assert DUMMY_SECRET_VALUE not in recipe_text


def test_vault_secret_import_file_recipe_loading():
    recipe_text = open(IMPORT_FILE_RECIPE, encoding="utf-8").read()
    recipe = load_recipe(IMPORT_FILE_RECIPE)

    assert recipe.action == "vault.secret.import_file"
    assert recipe.secret_name == "BACKUP_AWS_ACCESS_KEY_ID"
    assert recipe.env == "staging"
    assert recipe.source_file == "/path/to/local/secret-file"
    assert recipe.create_mode == "create_only"
    assert "value:" not in recipe_text
    assert "secret_value" not in recipe_text
    assert DUMMY_SECRET_VALUE not in recipe_text


def test_vault_secret_copy_many_recipe_loading():
    recipe_text = open(COPY_MANY_RECIPE, encoding="utf-8").read()
    recipe = load_recipe(COPY_MANY_RECIPE)

    assert recipe.action == "vault.secret.copy_many"
    assert recipe.source_env == "dev"
    assert recipe.target_env == "staging"
    assert recipe.create_mode == "create_only"
    assert recipe.secrets == [
        {"source": "BACKUP_AWS_ACCESS_KEY_ID", "target": "BACKUP_AWS_ACCESS_KEY_ID"},
        {"source": "BACKUP_AWS_SECRET_ACCESS_KEY", "target": "BACKUP_AWS_SECRET_ACCESS_KEY"},
        {"source": "BACKUP_S3_BUCKET", "target": "BACKUP_S3_BUCKET"},
        {"source": "BACKUP_AWS_REGION", "target": "BACKUP_AWS_REGION"},
    ]
    assert "value:" not in recipe_text
    assert "secret_value" not in recipe_text
    assert DUMMY_SECRET_VALUE not in recipe_text


def test_vault_secret_diff_envs_recipe_loading_contains_names_only():
    recipe_text = open(DIFF_ENVS_RECIPE, encoding="utf-8").read()
    recipe = load_recipe(DIFF_ENVS_RECIPE)

    assert recipe.action == "vault.secret.diff_envs"
    assert recipe.source_env == "dev"
    assert recipe.target_env == "staging"
    assert recipe.env == "dev"
    assert recipe.secrets == [
        {"name": "BACKUP_AWS_ACCESS_KEY_ID"},
        {"name": "BACKUP_AWS_SECRET_ACCESS_KEY"},
        {"name": "BACKUP_S3_BUCKET"},
        {"name": "BACKUP_AWS_REGION"},
    ]
    assert "value:" not in recipe_text
    assert "secret_value" not in recipe_text
    assert DUMMY_SECRET_VALUE not in recipe_text


def test_github_env_secret_plan_recipe_loading():
    recipe = load_recipe(GITHUB_RECIPE)

    assert recipe.action == "github.env_secret.plan"
    assert recipe.secret_name == DUMMY_SECRET_NAME
    assert recipe.env == "dev"
    assert recipe.github_owner == "example-owner"
    assert recipe.github_repo == "secretops"
    assert recipe.github_environment == "dev"
    assert recipe.github_secret_name == DUMMY_SECRET_NAME


def test_github_env_secret_plan_policy_allows_action():
    recipe = load_recipe(GITHUB_RECIPE)

    recipe.require_allowed()


def test_generic_plan_works_for_github_dry_run_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "plan",
        "--file",
        GITHUB_RECIPE,
        audit_path=audit_path,
    )

    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "DUMMY_SECRET" in output
    assert "dev" in output
    assert "example-owner" in output
    assert "secretops" in output
    assert "no GitHub API call was made" in output
    assert "secret value will NOT be printed" in output
    assert_no_secret_leak(result, audit_path)


def test_plan_github_env_secret_outputs_safe_dry_run(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "plan-github-env-secret",
        "--file",
        GITHUB_RECIPE,
        audit_path=audit_path,
    )

    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "DUMMY_SECRET" in output
    assert "dev" in output
    assert "example-owner" in output
    assert "secretops" in output
    assert "no GitHub API call was made" in output
    assert "secret value will NOT be printed" in output
    assert_no_secret_leak(result, audit_path)


def test_generic_execute_fails_safely_for_github_dry_run_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "execute",
        "--file",
        GITHUB_RECIPE,
        audit_path=audit_path,
    )

    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "planning only" in output
    assert "no GitHub API call was made" in output
    assert_no_secret_leak(result, audit_path)


def test_github_env_secret_write_recipe_loading():
    recipe = load_recipe(GITHUB_WRITE_RECIPE)

    assert recipe.action == "github.env_secret.write"
    assert recipe.secret_name == DUMMY_SECRET_NAME
    assert recipe.env == "dev"
    assert recipe.github_owner == "example-owner"
    assert recipe.github_repo == "secretops"
    assert recipe.github_environment == "dev"
    assert recipe.github_secret_name == DUMMY_SECRET_NAME


def test_github_env_secret_write_policy_allows_action():
    recipe = load_recipe(GITHUB_WRITE_RECIPE)

    recipe.require_allowed()


def test_generic_plan_prefers_github_write_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "plan",
        "--file",
        GITHUB_WRITE_RECIPE,
        audit_path=audit_path,
    )

    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "github.env_secret.write" in output
    assert "DUMMY_SECRET" in output
    assert "dev" in output
    assert "example-owner" in output
    assert "secretops" in output
    assert "no GitHub API call was made" in output
    assert "secret value will NOT be printed" in output
    assert_no_secret_leak(result, audit_path)


def test_plan_github_env_secret_accepts_write_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "plan-github-env-secret",
        "--file",
        GITHUB_WRITE_RECIPE,
        audit_path=audit_path,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "github.env_secret.write" in output
    assert "DUMMY_SECRET" in output
    assert "secret value will NOT be printed" in output
    assert_no_secret_leak(result, audit_path)


def test_github_env_secret_sync_many_recipe_loading():
    recipe = load_recipe(GITHUB_SYNC_MANY_RECIPE)

    assert recipe.action == "github.env_secret.sync_many"
    assert recipe.secret_name is None
    assert recipe.env == "dev"
    assert recipe.github_owner == "example-owner"
    assert recipe.github_repo == "secretops"
    assert recipe.github_environment == "dev"
    assert recipe.secrets == [{"source": DUMMY_SECRET_NAME, "target": DUMMY_SECRET_NAME}]


def test_github_env_secret_verify_many_recipe_loading_contains_expected_names_only():
    recipe_text = open(GITHUB_VERIFY_MANY_RECIPE, encoding="utf-8").read()
    recipe = load_recipe(GITHUB_VERIFY_MANY_RECIPE)

    assert recipe.action == "github.env_secret.verify_many"
    assert recipe.secret_name is None
    assert recipe.env == "staging"
    assert recipe.github_owner == "example-owner"
    assert recipe.github_repo == "secretops"
    assert recipe.github_environment == "staging"
    assert recipe.secrets == [
        {"name": "BACKUP_AWS_ACCESS_KEY_ID"},
        {"name": "BACKUP_AWS_SECRET_ACCESS_KEY"},
        {"name": "BACKUP_S3_BUCKET"},
        {"name": "BACKUP_AWS_REGION"},
    ]
    assert "value:" not in recipe_text
    assert "secret_value" not in recipe_text
    assert DUMMY_SECRET_VALUE not in recipe_text


def test_github_backup_secrets_sync_many_recipe_loading_contains_names_only():
    recipe_text = open(GITHUB_BACKUP_SYNC_MANY_RECIPE, encoding="utf-8").read()
    recipe = load_recipe(GITHUB_BACKUP_SYNC_MANY_RECIPE)
    expected_mappings = [
        {"source": "BACKUP_AWS_ACCESS_KEY_ID", "target": "BACKUP_AWS_ACCESS_KEY_ID"},
        {
            "source": "BACKUP_AWS_SECRET_ACCESS_KEY",
            "target": "BACKUP_AWS_SECRET_ACCESS_KEY",
        },
        {"source": "BACKUP_S3_BUCKET", "target": "BACKUP_S3_BUCKET"},
        {"source": "BACKUP_AWS_REGION", "target": "BACKUP_AWS_REGION"},
    ]

    assert recipe.action == "github.env_secret.sync_many"
    assert recipe.secret_name is None
    assert recipe.env == "staging"
    assert recipe.github_owner == "example-owner"
    assert recipe.github_repo == "example-repo"
    assert recipe.github_environment == "staging"
    assert recipe.secrets == expected_mappings
    assert "value:" not in recipe_text
    assert "secret_value" not in recipe_text
    assert DUMMY_SECRET_VALUE not in recipe_text
    for mapping in expected_mappings:
        assert mapping["source"] in recipe_text
        assert mapping["target"] in recipe_text


def test_github_env_secret_sync_many_policy_allows_action():
    recipe = load_recipe(GITHUB_SYNC_MANY_RECIPE)

    recipe.require_allowed()


def test_generic_plan_works_for_sync_many_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "plan",
        "--file",
        GITHUB_SYNC_MANY_RECIPE,
        audit_path=audit_path,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "github.env_secret.sync_many" in output
    assert "DUMMY_SECRET -> DUMMY_SECRET" in output
    assert "example-owner" in output
    assert "no GitHub API call was made" in output
    assert "secret value will NOT be printed" in output
    assert_no_secret_leak(result, audit_path)


def test_plan_github_env_secret_accepts_sync_many_recipe(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "plan-github-env-secret",
        "--file",
        GITHUB_SYNC_MANY_RECIPE,
        audit_path=audit_path,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "github.env_secret.sync_many" in output
    assert "DUMMY_SECRET -> DUMMY_SECRET" in output
    assert_no_secret_leak(result, audit_path)


def test_plan_github_env_secret_writes_safe_audit_event(tmp_path):
    audit_path = tmp_path / "secretops-test.jsonl"

    result = run_secretops(
        "plan-github-env-secret",
        "--file",
        GITHUB_RECIPE,
        audit_path=audit_path,
    )

    assert result.returncode == 0
    assert audit_path.exists()
    assert "github.env_secret.plan" in audit_path.read_text()
    assert DUMMY_SECRET_NAME in audit_path.read_text()
    assert_no_secret_leak(result, audit_path)






def test_recipe_rejects_invalid_secret_names_for_all_secret_fields(tmp_path):
    invalid_names = [
        "X') or __import__('os').system('echo pwned')#",
        "BAD-NAME",
        "bad_lowercase",
        "BAD NAME",
        "BAD/NAME",
        "BAD=NAME",
        "",
    ]
    templates = [
        "action: vault.read.check\nsecret_name: {name!r}\nenv: dev\n",
        "action: vault.secret.generate\nsecret_name: {name!r}\nenv: dev\ngenerator:\n  type: password\n",
        "action: vault.secret.rotate\nsecret_name: {name!r}\nenv: dev\ngenerator:\n  type: password\n",
        "action: github.env_secret.write\nsecret_name: DUMMY_SECRET\ngithub_secret_name: {name!r}\ngithub_owner: example-owner\ngithub_repo: secretops\ngithub_environment: dev\n",
        "action: github.env_secret.sync_many\nenv: dev\ngithub_owner: example-owner\ngithub_repo: secretops\ngithub_environment: dev\nsecrets:\n  - source: {name!r}\n    target: DUMMY_SECRET\n",
        "action: github.env_secret.sync_many\nenv: dev\ngithub_owner: example-owner\ngithub_repo: secretops\ngithub_environment: dev\nsecrets:\n  - source: DUMMY_SECRET\n    target: {name!r}\n",
        "action: vault.secret.copy_many\nsource_env: dev\ntarget_env: staging\nsecrets:\n  - source: {name!r}\n    target: DUMMY_SECRET\n",
        "action: vault.secret.copy_many\nsource_env: dev\ntarget_env: staging\nsecrets:\n  - source: DUMMY_SECRET\n    target: {name!r}\n",
        "action: vault.secret.diff_envs\nsource_env: dev\ntarget_env: staging\nsecrets:\n  - name: {name!r}\n",
        "action: github.env_secret.verify_many\ngithub_owner: example-owner\ngithub_repo: secretops\ngithub_environment: dev\nsecrets:\n  - name: {name!r}\n",
    ]

    for invalid in invalid_names:
        for index, template in enumerate(templates):
            recipe_path = tmp_path / f"invalid-{index}.yaml"
            recipe_path.write_text(template.format(name=invalid), encoding="utf-8")
            with pytest.raises(ValueError):
                load_recipe(recipe_path)


def test_recipe_loading_rejects_flag_like_github_target_metadata(tmp_path):
    templates = [
        "action: github.env_secret.write\nsecret_name: DUMMY_SECRET\ngithub_secret_name: DUMMY_SECRET\ngithub_owner: --owner-flag\ngithub_repo: secretops\ngithub_environment: dev\n",
        "action: github.env_secret.write\nsecret_name: DUMMY_SECRET\ngithub_secret_name: DUMMY_SECRET\ngithub_owner: example-owner\ngithub_repo: --repo-flag\ngithub_environment: dev\n",
        "action: github.env_secret.write\nsecret_name: DUMMY_SECRET\ngithub_secret_name: DUMMY_SECRET\ngithub_owner: example-owner\ngithub_repo: secretops\ngithub_environment: --env-flag\n",
        "action: github.env_secret.sync_many\nenv: dev\ngithub_owner: --owner-flag\ngithub_repo: secretops\ngithub_environment: dev\nsecrets:\n  - source: DUMMY_SECRET\n    target: DUMMY_SECRET\n",
        "action: github.env_secret.verify_many\ngithub_owner: example-owner\ngithub_repo: --repo-flag\ngithub_environment: dev\nsecrets:\n  - name: DUMMY_SECRET\n",
    ]

    for index, template in enumerate(templates):
        recipe_path = tmp_path / f"invalid-github-target-{index}.yaml"
        recipe_path.write_text(template, encoding="utf-8")
        with pytest.raises(ValueError, match="invalid GitHub target metadata"):
            load_recipe(recipe_path)
