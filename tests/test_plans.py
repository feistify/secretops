import json
import os
import subprocess
import sys

import pytest


DUMMY_SECRET_NAME = "DUMMY_SECRET"
DUMMY_SECRET_VALUE = "not-a-real-secret"
VAULT_RECIPE = "examples/recipes/dummy-vault-check.yaml"
GITHUB_RECIPE = "examples/recipes/github-env-secret-plan.yaml"
GITHUB_WRITE_RECIPE = "examples/recipes/github-env-secret-write.yaml"
GITHUB_SYNC_MANY_RECIPE = "examples/recipes/github-env-secret-sync-many.yaml"
GITHUB_VERIFY_MANY_RECIPE = "examples/recipes/github-env-secret-verify-many.yaml"
GENERATE_RECIPE = "examples/recipes/vault-secret-generate.yaml"
ROTATE_RECIPE = "examples/recipes/vault-secret-rotate.yaml"
IMPORT_FILE_RECIPE = "examples/recipes/vault-secret-import-file.yaml"
COPY_MANY_RECIPE = "examples/recipes/vault-secret-copy-many.yaml"
DIFF_ENVS_RECIPE = "examples/recipes/vault-secret-diff-envs.yaml"

COPY_SOURCE_VALUES = {
    "BACKUP_AWS_ACCESS_KEY_ID": "copy-access-key-value",
    "BACKUP_AWS_SECRET_ACCESS_KEY": "copy-secret-key-value",
    "BACKUP_S3_BUCKET": "copy-bucket-value",
    "BACKUP_AWS_REGION": "copy-region-value",
}


def assert_no_copy_value_leak(result: subprocess.CompletedProcess[str], *paths) -> None:
    for value in COPY_SOURCE_VALUES.values():
        assert value not in result.stdout
        assert value not in result.stderr
        for path in paths:
            if path.exists():
                assert value not in path.read_text(encoding="utf-8")


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def run_secretops(
    *args: str, audit_path=None, plan_dir=None, path_prefix=None, policy_path=None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env[DUMMY_SECRET_NAME] = DUMMY_SECRET_VALUE
    if audit_path is not None:
        env["SECRETOPS_AUDIT_PATH"] = str(audit_path)
    if plan_dir is not None:
        env["SECRETOPS_PLAN_DIR"] = str(plan_dir)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
        fake_infisical = os.path.join(str(path_prefix), "infisical")
        if os.path.exists(fake_infisical):
            env["SECRETOPS_INFISICAL_BIN"] = fake_infisical
        fake_gh = os.path.join(str(path_prefix), "gh")
        if os.path.exists(fake_gh):
            env["SECRETOPS_GH_BIN"] = fake_gh
    if policy_path is not None:
        env["SECRETOPS_POLICY_PATH"] = str(policy_path)

    return subprocess.run(
        ["uv", "run", "secretops", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def assert_no_secret_leak(result: subprocess.CompletedProcess[str], *paths) -> None:
    assert DUMMY_SECRET_VALUE not in result.stdout
    assert DUMMY_SECRET_VALUE not in result.stderr
    for path in paths:
        if path.exists():
            assert DUMMY_SECRET_VALUE not in path.read_text(encoding="utf-8")


def saved_plan_file(plan_dir):
    files = list(plan_dir.glob("*.json"))
    assert len(files) == 1
    return files[0]


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


TEST_VAULT_DESTINATION = {
    "backend": "infisical",
    "domain": "https://eu.infisical.com",
    "project_id": "dummy-project",
    "path": "/",
}


def bind_plan(plan):
    """Turn a hand-written plan into a contract version 1 plan for the test
    vault configuration, approved with a digest when its status says so."""
    from secretops.plans import CONTRACT_VERSION, GITHUB_ACTIONS, VAULT_ACTIONS, plan_digest

    plan = json.loads(json.dumps(plan))
    plan.setdefault("contract_version", CONTRACT_VERSION)
    if plan["action"] in VAULT_ACTIONS:
        plan.setdefault("vault_destination", dict(TEST_VAULT_DESTINATION))
    if plan["action"] in GITHUB_ACTIONS and isinstance(plan.get("target"), dict):
        plan["target"].setdefault("target_host", "github.com")
        plan["target"].setdefault("api_call_made", False)
        plan["target"].setdefault("secret_value_printed", False)
    if plan.get("status") == "approved" and "approval_digest" not in plan:
        plan["approval_digest"] = plan_digest(plan)
    return plan


def write_plan(plan_dir, plan, *, legacy=False):
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / f"{plan['plan_id']}.json"
    if not legacy:
        plan = bind_plan(plan)
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def read_plan(plan_path):
    return json.loads(plan_path.read_text(encoding="utf-8"))


def github_plan(plan_id="github-plan", status="planned", action="github.env_secret.plan"):
    return {
        "plan_id": plan_id,
        "action": action,
        "secret_name": DUMMY_SECRET_NAME,
        "env": "dev",
        "status": status,
        "secret_value_included": False,
        "target": {
            "target_owner": "example-owner",
            "target_repo": "secretops",
            "target_environment": "dev",
            "target_secret_name": DUMMY_SECRET_NAME,
            "api_call_made": False,
            "secret_value_printed": False,
        },
    }


def vault_plan(plan_id="vault-plan", status="planned"):
    return {
        "plan_id": plan_id,
        "action": "vault.read.check",
        "secret_name": DUMMY_SECRET_NAME,
        "env": "dev",
        "status": status,
        "secret_value_included": False,
    }


def generate_plan(plan_id="generate-plan", status="planned"):
    return {
        "plan_id": plan_id,
        "action": "vault.secret.generate",
        "secret_name": "GENERATED_DUMMY_PASSWORD",
        "env": "dev",
        "status": status,
        "secret_value_included": False,
        "create_mode": "create_only",
        "generator": {"type": "password", "length": 32},
    }


def rotate_plan(plan_id="rotate-plan", status="planned"):
    return {
        "plan_id": plan_id,
        "action": "vault.secret.rotate",
        "secret_name": "GENERATED_DUMMY_PASSWORD",
        "env": "dev",
        "status": status,
        "secret_value_included": False,
        "rotation_mode": "replace_existing",
        "generator": {"type": "password", "length": 32},
    }


def import_file_plan(
    plan_id="import-file-plan",
    status="planned",
    source_file="/tmp/imported-secret",
    secret_mappings=None,
):
    return {
        "plan_id": plan_id,
        "action": "vault.secret.import_file",
        "secret_name": "BACKUP_AWS_ACCESS_KEY_ID",
        "env": "staging",
        "status": status,
        "secret_value_included": False,
        "source_file": source_file,
        "create_mode": "create_only",
        "secret_mappings": secret_mappings
        or [{"name": "BACKUP_AWS_ACCESS_KEY_ID"}],
    }


def copy_many_plan(plan_id="copy-many-plan", status="planned"):
    return {
        "plan_id": plan_id,
        "action": "vault.secret.copy_many",
        "env": "dev",
        "source_env": "dev",
        "target_env": "staging",
        "status": status,
        "secret_value_included": False,
        "create_mode": "create_only",
        "secret_mappings": [
            {"source": "BACKUP_AWS_ACCESS_KEY_ID", "target": "BACKUP_AWS_ACCESS_KEY_ID"},
            {"source": "BACKUP_AWS_SECRET_ACCESS_KEY", "target": "BACKUP_AWS_SECRET_ACCESS_KEY"},
            {"source": "BACKUP_S3_BUCKET", "target": "BACKUP_S3_BUCKET"},
            {"source": "BACKUP_AWS_REGION", "target": "BACKUP_AWS_REGION"},
        ],
    }


def sync_many_plan(plan_id="sync-many-plan", status="planned"):
    return {
        "plan_id": plan_id,
        "action": "github.env_secret.sync_many",
        "env": "dev",
        "status": status,
        "secret_value_included": False,
        "target": {
            "target_owner": "example-owner",
            "target_repo": "secretops",
            "target_environment": "dev",
            "api_call_made": False,
            "secret_value_printed": False,
        },
        "secret_mappings": [
            {"source": DUMMY_SECRET_NAME, "target": DUMMY_SECRET_NAME}
        ],
    }


def verify_many_plan(plan_id="verify-many-plan", status="planned"):
    return {
        "plan_id": plan_id,
        "action": "github.env_secret.verify_many",
        "env": "staging",
        "status": status,
        "secret_value_included": False,
        "target": {
            "target_owner": "example-owner",
            "target_repo": "secretops",
            "target_environment": "staging",
            "api_call_made": False,
            "secret_value_printed": False,
        },
        "secret_mappings": [
            {"name": "BACKUP_AWS_ACCESS_KEY_ID"},
            {"name": "BACKUP_AWS_SECRET_ACCESS_KEY"},
            {"name": "BACKUP_S3_BUCKET"},
            {"name": "BACKUP_AWS_REGION"},
        ],
    }


def diff_envs_plan(plan_id="diff-envs-plan", status="planned"):
    return {
        "plan_id": plan_id,
        "action": "vault.secret.diff_envs",
        "env": "dev",
        "source_env": "dev",
        "target_env": "staging",
        "status": status,
        "secret_value_included": False,
        "secret_mappings": [
            {"name": "BACKUP_AWS_ACCESS_KEY_ID"},
            {"name": "BACKUP_AWS_SECRET_ACCESS_KEY"},
            {"name": "BACKUP_S3_BUCKET"},
            {"name": "BACKUP_AWS_REGION"},
        ],
    }


def test_plan_save_creates_safe_vault_plan_artifact(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        VAULT_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert "plan_id" in result.stdout
    assert plan_path.name == f"{plan['plan_id']}.json"
    assert plan["action"] == "vault.read.check"
    assert plan["env"] == "dev"
    assert plan["secret_name"] == DUMMY_SECRET_NAME
    assert plan["status"] == "planned"
    assert plan["secret_value_included"] is False
    assert "target" not in plan
    assert_no_secret_leak(result, plan_path, audit_path)


def test_plan_save_creates_safe_generate_plan_artifact(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        GENERATE_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert "plan_id" in result.stdout
    assert plan["action"] == "vault.secret.generate"
    assert plan["env"] == "dev"
    assert plan["secret_name"] == "GENERATED_DUMMY_PASSWORD"
    assert plan["status"] == "planned"
    assert plan["generator"] == {"type": "password", "length": 32}
    assert plan["create_mode"] == "create_only"
    assert plan["secret_value_included"] is False
    assert "target" not in plan
    assert "secret_mappings" not in plan
    assert "not-a-real-secret" not in plan_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_plan_save_creates_safe_rotate_plan_artifact(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        ROTATE_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert "plan_id" in result.stdout
    assert plan["action"] == "vault.secret.rotate"
    assert plan["env"] == "dev"
    assert plan["secret_name"] == "GENERATED_DUMMY_PASSWORD"
    assert plan["status"] == "planned"
    assert plan["generator"] == {"type": "password", "length": 32}
    assert plan["rotation_mode"] == "replace_existing"
    assert plan["secret_value_included"] is False
    assert "target" not in plan
    assert "secret_mappings" not in plan
    assert "not-a-real-secret" not in plan_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)



def test_plan_save_creates_safe_import_file_plan_artifact(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    dotenv_path = tmp_path / "import.env"
    dotenv_path.write_text("BACKUP_AWS_ACCESS_KEY_ID=not-a-real-secret\n", encoding="utf-8")
    dotenv_path.chmod(0o600)
    recipe_path = tmp_path / "import.yaml"
    recipe_path.write_text(
        f"action: vault.secret.import_file\n"
        f"env: staging\n"
        f"source_file: {dotenv_path}\n"
        f"create_mode: create_only\n",
        encoding="utf-8",
    )

    result = run_secretops(
        "plan",
        "--file",
        str(recipe_path),
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert "plan_id" in result.stdout
    assert plan["action"] == "vault.secret.import_file"
    assert plan["env"] == "staging"
    assert "secret_name" not in plan
    assert plan["secret_mappings"] == [{"name": "BACKUP_AWS_ACCESS_KEY_ID"}]
    assert plan["source_file"] == str(dotenv_path)
    assert plan["create_mode"] == "create_only"
    assert plan["secret_value_included"] is False
    assert "target" not in plan
    assert "not-a-real-secret" not in plan_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_plan_save_parses_dotenv_import_file_names_without_values(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    dotenv_path = tmp_path / "bootstrap.env"
    dotenv_path.write_text(
        "BACKUP_AWS_ACCESS_KEY_ID=secret-access-id\n"
        "BACKUP_AWS_REGION=eu-central-1\n",
        encoding="utf-8",
    )
    dotenv_path.chmod(0o600)
    recipe_path = tmp_path / "import.yaml"
    recipe_path.write_text(
        f"action: vault.secret.import_file\n"
        f"env: staging\n"
        f"source_file: {dotenv_path}\n"
        f"create_mode: create_only\n",
        encoding="utf-8",
    )

    result = run_secretops(
        "plan",
        "--file",
        str(recipe_path),
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan_text = plan_path.read_text(encoding="utf-8")
    plan = json.loads(plan_text)
    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "secret_name" not in plan
    assert plan["secret_mappings"] == [
        {"name": "BACKUP_AWS_ACCESS_KEY_ID"},
        {"name": "BACKUP_AWS_REGION"},
    ]
    assert "secret-access-id" not in output
    assert "eu-central-1" not in output
    assert "secret-access-id" not in plan_text
    assert "eu-central-1" not in plan_text
    assert_no_secret_leak(result, plan_path, audit_path)


def test_plan_save_import_file_rejects_malformed_dotenv_without_leaking_token(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    token = "mF9m9B2ZW5mVkFzRk1SVEJNU0pJSz0="
    dotenv_path = tmp_path / "malformed.env"
    dotenv_path.write_text(f"{token}=x\n", encoding="utf-8")
    dotenv_path.chmod(0o600)
    recipe_path = tmp_path / "import.yaml"
    recipe_path.write_text(
        f"action: vault.secret.import_file\n"
        f"env: staging\n"
        f"source_file: {dotenv_path}\n"
        f"create_mode: create_only\n",
        encoding="utf-8",
    )

    result = run_secretops(
        "plan",
        "--file",
        str(recipe_path),
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL import file plan is unsafe" in output
    assert "invalid dotenv key name at line 1" in output
    assert "Traceback" not in output
    assert token not in output
    assert list(plan_dir.glob("*.json")) == []
    if audit_path.exists():
        assert token not in audit_path.read_text(encoding="utf-8")


def test_plan_save_import_file_rejects_missing_source_file_cleanly(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    missing_path = tmp_path / "missing.env"
    recipe_path = tmp_path / "import.yaml"
    recipe_path.write_text(
        f"action: vault.secret.import_file\n"
        f"env: staging\n"
        f"source_file: {missing_path}\n"
        f"create_mode: create_only\n",
        encoding="utf-8",
    )

    result = run_secretops(
        "plan",
        "--file",
        str(recipe_path),
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL import file plan is unsafe" in output
    assert "source file exists: False" in output
    assert "Traceback" not in output
    assert list(plan_dir.glob("*.json")) == []


def test_plan_save_import_file_rejects_world_readable_source(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    dotenv_path = tmp_path / "world.env"
    dotenv_path.write_text("BOOTSTRAP_TEST_SECRET=secret-value\n", encoding="utf-8")
    dotenv_path.chmod(0o644)
    recipe_path = tmp_path / "import.yaml"
    recipe_path.write_text(
        f"action: vault.secret.import_file\n"
        f"env: staging\n"
        f"source_file: {dotenv_path}\n"
        f"create_mode: create_only\n",
        encoding="utf-8",
    )

    result = run_secretops(
        "plan",
        "--file",
        str(recipe_path),
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "source file permissions restricted: False" in output
    assert "secret-value" not in output
    assert list(plan_dir.glob("*.json")) == []


def test_plan_save_import_file_policy_denial_happens_before_dotenv_parse(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    canary = "policy-denied-canary-value-never-read-or-printed"
    dotenv_path = tmp_path / "denied.env"
    dotenv_path.write_text(f"bad-key={canary}\n", encoding="utf-8")
    dotenv_path.chmod(0o600)
    recipe_path = tmp_path / "import.yaml"
    recipe_path.write_text(
        f"action: vault.secret.import_file\n"
        f"env: staging\n"
        f"source_file: {dotenv_path}\n"
        f"create_mode: create_only\n",
        encoding="utf-8",
    )
    policy_path = tmp_path / "deny-import.yaml"
    policy_path.write_text(
        "allowed_actions: []\n"
        "denied_actions:\n"
        "  - vault.secret.import_file\n",
        encoding="utf-8",
    )

    result = run_secretops(
        "plan",
        "--file",
        str(recipe_path),
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
        policy_path=policy_path,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Action denied by policy: vault.secret.import_file" in output
    assert "invalid dotenv key name" not in output
    assert canary not in output
    assert list(plan_dir.glob("*.json")) == []
    if audit_path.exists():
        assert canary not in audit_path.read_text(encoding="utf-8")


def test_copy_many_plan_can_be_saved_with_name_only_mappings(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        COPY_MANY_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = read_plan(plan_path)
    assert result.returncode == 0
    assert plan["action"] == "vault.secret.copy_many"
    assert plan["source_env"] == "dev"
    assert plan["target_env"] == "staging"
    assert plan["create_mode"] == "create_only"
    assert plan["secret_value_included"] is False
    assert plan["secret_mappings"] == copy_many_plan()["secret_mappings"]
    assert "target" not in plan
    assert "secret_name" not in plan
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_copy_value_leak(result, plan_path, audit_path)


def test_show_plan_displays_copy_many_mappings_safely(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, copy_many_plan("show-copy-many"))

    result = run_secretops(
        "show-plan",
        "--id",
        "show-copy-many",
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: vault.secret.copy_many" in output
    assert "source_env: dev" in output
    assert "target_env: staging" in output
    assert "create_mode: create_only" in output
    assert "secret_mapping: BACKUP_AWS_ACCESS_KEY_ID -> BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "secret_mapping: BACKUP_AWS_SECRET_ACCESS_KEY -> BACKUP_AWS_SECRET_ACCESS_KEY" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert '"secret_mappings"' not in output
    assert_no_secret_leak(result, plan_path)
    assert_no_copy_value_leak(result, plan_path)


def test_plan_save_creates_safe_github_target_plan_artifact(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        GITHUB_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert "plan_id" in result.stdout
    assert plan["action"] == "github.env_secret.plan"
    assert plan["env"] == "dev"
    assert plan["secret_name"] == DUMMY_SECRET_NAME
    assert plan["status"] == "planned"
    assert plan["secret_value_included"] is False
    assert plan["target"] == {
        "target_host": "github.com",
        "target_owner": "example-owner",
        "target_repo": "secretops",
        "target_environment": "dev",
        "target_secret_name": DUMMY_SECRET_NAME,
        "api_call_made": False,
        "secret_value_printed": False,
    }
    assert_no_secret_leak(result, plan_path, audit_path)

def write_failing_external_commands(tmp_path):
    for command_name in ("infisical", "gh"):
        command_path = tmp_path / command_name
        marker_path = tmp_path / f"{command_name}-called"
        command_path.write_text(
            "#!/usr/bin/env python3\n"
            f"from pathlib import Path\n"
            f"Path({str(marker_path)!r}).write_text('called', encoding='utf-8')\n"
            "raise SystemExit(99)\n",
            encoding="utf-8",
        )
        command_path.chmod(0o700)
    return tmp_path


def test_show_plan_displays_saved_vault_plan_safely(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        {
            "plan_id": "vault-plan",
            "action": "vault.read.check",
            "secret_name": DUMMY_SECRET_NAME,
            "env": "dev",
            "status": "planned",
            "secret_value_included": False,
        },
    )

    before_plan = json.loads(plan_path.read_text(encoding="utf-8"))

    result = run_secretops(
        "show-plan",
        "--id",
        "vault-plan",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    after_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert after_plan == before_plan
    assert "plan_id: vault-plan" in output
    assert "status: planned" in output
    assert "action: vault.read.check" in output
    assert f"source_secret_name: {DUMMY_SECRET_NAME}" in output
    assert "source_environment: dev" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert "\"secret_name\"" not in output
    assert not audit_path.exists()
    assert_no_secret_leak(result, plan_path)


def test_show_plan_displays_saved_github_target_metadata_safely(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        {
            "plan_id": "github-plan",
            "action": "github.env_secret.plan",
            "secret_name": DUMMY_SECRET_NAME,
            "env": "dev",
            "status": "planned",
            "secret_value_included": False,
            "target": {
                "target_owner": "example-owner",
                "target_repo": "secretops",
                "target_environment": "dev",
                "target_secret_name": DUMMY_SECRET_NAME,
                "api_call_made": False,
                "secret_value_printed": False,
            },
        },
    )

    before_plan = json.loads(plan_path.read_text(encoding="utf-8"))

    result = run_secretops(
        "show-plan",
        "--id",
        "github-plan",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    after_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert after_plan == before_plan
    assert "plan_id: github-plan" in output
    assert "status: planned" in output
    assert "action: github.env_secret.plan" in output
    assert f"source_secret_name: {DUMMY_SECRET_NAME}" in output
    assert "source_environment: dev" in output
    assert "target_owner: example-owner" in output
    assert "target_repo: secretops" in output
    assert "target_environment: dev" in output
    assert f"target_secret_name: {DUMMY_SECRET_NAME}" in output
    assert "api_call_made: False" in output
    assert "secret_value_printed: False" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert "\"target\"" not in output
    assert not audit_path.exists()
    assert_no_secret_leak(result, plan_path)


def test_show_plan_respects_secretops_plan_dir(tmp_path):
    plan_dir = tmp_path / "custom-plans"
    plan_path = write_plan(
        plan_dir,
        {
            "plan_id": "custom-dir-plan",
            "action": "vault.read.check",
            "secret_name": DUMMY_SECRET_NAME,
            "env": "dev",
            "status": "planned",
            "secret_value_included": False,
        },
    )

    result = run_secretops(
        "show-plan",
        "--id",
        "custom-dir-plan",
        plan_dir=plan_dir,
    )

    assert result.returncode == 0
    assert "plan_id: custom-dir-plan" in result.stdout
    assert_no_secret_leak(result, plan_path)


def test_show_plan_does_not_call_infisical_or_github(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        {
            "plan_id": "no-external-calls-plan",
            "action": "github.env_secret.plan",
            "secret_name": DUMMY_SECRET_NAME,
            "env": "dev",
            "status": "planned",
            "secret_value_included": False,
            "target": {
                "target_owner": "example-owner",
                "target_repo": "secretops",
                "target_environment": "dev",
                "target_secret_name": DUMMY_SECRET_NAME,
            },
        },
    )
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "show-plan",
        "--id",
        "no-external-calls-plan",
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode == 0
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path)


def test_approve_plan_changes_status_planned_to_approved(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, vault_plan("approve-me"))

    result = run_secretops(
        "approve-plan",
        "--id",
        "approve-me",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )
    show_result = run_secretops(
        "show-plan",
        "--id",
        "approve-me",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "OK plan approved: approve-me"
    assert read_plan(plan_path)["status"] == "approved"
    assert read_plan(plan_path)["approval_digest"].startswith("sha256-v1:")
    assert "status: approved" in show_result.stdout
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_secret_leak(show_result, plan_path, audit_path)


def test_reject_plan_changes_status_planned_to_rejected(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, vault_plan("reject-me"))

    result = run_secretops(
        "reject-plan",
        "--id",
        "reject-me",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )
    show_result = run_secretops(
        "show-plan",
        "--id",
        "reject-me",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "OK plan rejected: reject-me"
    assert read_plan(plan_path)["status"] == "rejected"
    assert "status: rejected" in show_result.stdout
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_secret_leak(show_result, plan_path, audit_path)


def test_rejected_plan_cannot_execute(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, vault_plan("rejected-plan", status="rejected"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "rejected-plan",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "rejected plan cannot execute" in output
    assert read_plan(plan_path)["status"] == "rejected"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_github_saved_plan_cannot_execute_unless_approved(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, github_plan("unapproved-github-plan"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "unapproved-github-plan",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "GitHub plan requires approval" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_approved_github_plan_only_saved_plan_cannot_execute(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, github_plan("approved-github-plan", status="approved"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "approved-github-plan",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "planning only" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_approved_github_write_saved_plan_can_execute_with_fakes(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    gh_call_path = write_fake_gh(tmp_path)
    fake_bin = write_fake_infisical(tmp_path)
    plan_path = write_plan(
        plan_dir,
        github_plan("approved-github-write", status="approved", action="github.env_secret.write"),
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        "approved-github-write",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    gh_call = json.loads(gh_call_path.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        f"OK GitHub write acknowledged: {DUMMY_SECRET_NAME}",
        "secret values were not printed",
    ]
    assert read_plan(plan_path)["status"] == "executed"
    assert gh_call["stdin_matches_secret"] is True
    assert gh_call["secret_in_args"] is False
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "approved-github-write" in audit_text
    assert '"status":"success"' in audit_text
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_generate_plan_missing_secret_name_fails_cleanly(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = generate_plan("missing-generate-secret", status="approved")
    plan.pop("secret_name")
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "missing-generate-secret",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL" in output
    assert "plan file does not match the plan schema" in output
    assert "Traceback" not in output
    # Refused by the plan schema; the file is left as is.
    assert read_plan(plan_path)["status"] == "approved"
    assert not (tmp_path / "infisical-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_github_write_plan_missing_secret_name_fails_cleanly(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = github_plan("missing-github-source", status="approved", action="github.env_secret.write")
    plan.pop("secret_name")
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "missing-github-source",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL" in output
    assert "plan file does not match the plan schema" in output
    assert "Traceback" not in output
    # Refused by the plan schema; the file is left as is.
    assert read_plan(plan_path)["status"] == "approved"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_generate_plan_missing_secret_name_fails_cleanly(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = generate_plan("preflight-missing-generate-secret", status="approved")
    plan.pop("secret_name")
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-missing-generate-secret",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL" in output
    assert "plan file does not match the plan schema" in output
    assert "Traceback" not in output
    assert not (tmp_path / "infisical-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_rotate_plan_missing_secret_name_fails_cleanly(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = rotate_plan("preflight-missing-rotate-secret", status="approved")
    plan.pop("secret_name")
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-missing-rotate-secret",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL" in output
    assert "plan file does not match the plan schema" in output
    assert "Traceback" not in output
    assert not (tmp_path / "infisical-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_import_file_plan_missing_secret_name_fails_cleanly(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = import_file_plan("preflight-missing-import-secret", status="approved")
    plan.pop("secret_name")
    plan.pop("secret_mappings")
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-missing-import-secret",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL" in output
    assert "plan file does not match the plan schema" in output
    assert "Traceback" not in output
    assert not (tmp_path / "infisical-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_read_check_plan_missing_secret_name_fails_cleanly(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = vault_plan("execute-missing-read-secret", status="approved")
    plan.pop("secret_name")
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-missing-read-secret",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL" in output
    assert "plan file does not match the plan schema" in output
    assert "Traceback" not in output
    # Refused by the plan schema; the file is left as is.
    assert read_plan(plan_path)["status"] == "approved"
    assert not (tmp_path / "infisical-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_failed_execute_plan_changes_status_to_failed(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        {
            "plan_id": "failing-plan",
            "action": "secret.reveal",
            "secret_name": DUMMY_SECRET_NAME,
            "env": "dev",
            "status": "planned",
            "secret_value_included": False,
        },
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        "failing-plan",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    # An action outside the plan schema is refused and the file is left as is.
    assert result.returncode != 0
    assert "plan file does not match the plan schema" in result.stdout
    assert read_plan(plan_path)["status"] == "planned"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_plan_save_creates_safe_github_write_plan_artifact(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        GITHUB_WRITE_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert "plan_id" in result.stdout
    assert plan["action"] == "github.env_secret.write"
    assert plan["env"] == "dev"
    assert plan["secret_name"] == DUMMY_SECRET_NAME
    assert plan["status"] == "planned"
    assert plan["secret_value_included"] is False
    assert plan["target"] == {
        "target_host": "github.com",
        "target_owner": "example-owner",
        "target_repo": "secretops",
        "target_environment": "dev",
        "target_secret_name": DUMMY_SECRET_NAME,
        "api_call_made": False,
        "secret_value_printed": False,
    }
    assert_no_secret_leak(result, plan_path, audit_path)


def test_show_plan_displays_generate_metadata_safely(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, generate_plan("show-generate"))

    result = run_secretops(
        "show-plan",
        "--id",
        "show-generate",
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: vault.secret.generate" in output
    assert "source_secret_name: GENERATED_DUMMY_PASSWORD" in output
    assert "source_environment: dev" in output
    assert "generator_type: password" in output
    assert "generator_length: 32" in output
    assert "create_mode: create_only" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert '"generator"' not in output
    assert_no_secret_leak(result, plan_path)


def test_show_plan_displays_rotate_metadata_safely(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, rotate_plan("show-rotate"))

    result = run_secretops(
        "show-plan",
        "--id",
        "show-rotate",
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: vault.secret.rotate" in output
    assert "source_secret_name: GENERATED_DUMMY_PASSWORD" in output
    assert "source_environment: dev" in output
    assert "generator_type: password" in output
    assert "generator_length: 32" in output
    assert "rotation_mode: replace_existing" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert '"generator"' not in output
    assert_no_secret_leak(result, plan_path)



def test_show_plan_displays_import_file_metadata_safely(tmp_path):
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "imported-secret.txt"
    source_file.write_text(DUMMY_SECRET_VALUE, encoding="utf-8")
    plan_path = write_plan(plan_dir, import_file_plan("show-import-file", source_file=str(source_file)))

    result = run_secretops(
        "show-plan",
        "--id",
        "show-import-file",
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: vault.secret.import_file" in output
    assert "source_secret_name: BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "source_environment: staging" in output
    assert f"source_file: {source_file}" in "".join(output.splitlines())
    assert "create_mode: create_only" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert DUMMY_SECRET_VALUE not in output
    assert_no_secret_leak(result, plan_path)


def test_show_plan_displays_saved_github_write_metadata_safely(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        github_plan("github-write-plan", action="github.env_secret.write"),
    )

    result = run_secretops(
        "show-plan",
        "--id",
        "github-write-plan",
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: github.env_secret.write" in output
    assert f"source_secret_name: {DUMMY_SECRET_NAME}" in output
    assert "source_environment: dev" in output
    assert "target_owner: example-owner" in output
    assert "target_repo: secretops" in output
    assert "target_environment: dev" in output
    assert f"target_secret_name: {DUMMY_SECRET_NAME}" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert_no_secret_leak(result, plan_path)


def test_github_write_plan_cannot_execute_unless_approved(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        github_plan("unapproved-github-write", action="github.env_secret.write"),
    )
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "unapproved-github-write",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "GitHub write plan requires approval" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_approved_github_write_plan_executes_with_fakes(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    gh_call_path = write_fake_gh(tmp_path)
    fake_bin = write_fake_infisical(tmp_path)
    plan_path = write_plan(
        plan_dir,
        github_plan(
            "approved-github-write",
            status="approved",
            action="github.env_secret.write",
        ),
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        "approved-github-write",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    gh_call = json.loads(gh_call_path.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        f"OK GitHub write acknowledged: {DUMMY_SECRET_NAME}",
        "secret values were not printed",
    ]
    assert read_plan(plan_path)["status"] == "executed"
    assert gh_call["args"] == [
        "secret",
        "set",
        DUMMY_SECRET_NAME,
        "--repo",
        "github.com/example-owner/secretops",
        "--env",
        "dev",
    ]
    assert gh_call["stdin_matches_secret"] is True
    assert gh_call["secret_in_args"] is False
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "approved-github-write" in audit_text
    assert '"status":"success"' in audit_text
    assert_no_secret_leak(result, plan_path, audit_path)


def test_old_github_plan_action_remains_compatible(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        GITHUB_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = read_plan(plan_path)
    assert result.returncode == 0
    assert plan["action"] == "github.env_secret.plan"
    assert plan["secret_value_included"] is False
    assert_no_secret_leak(result, plan_path, audit_path)


def write_marker_gh(tmp_path):
    fake_gh = tmp_path / "gh"
    marker_path = tmp_path / "gh-called"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker_path)!r}).write_text('called', encoding='utf-8')\n"
        "raise SystemExit(99)\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    return tmp_path


def test_preflight_plan_succeeds_when_source_secret_exists(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, vault_plan("preflight-vault", status="approved"))
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-vault",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: vault.read.check" in output
    assert f"source secret exists: {DUMMY_SECRET_NAME}" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert '"status":"success"' in audit_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_plan_fails_safely_when_source_secret_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        {
            **vault_plan("preflight-missing", status="planned"),
            "secret_name": "MISSING_SECRET",
        },
    )
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-missing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: vault.read.check" in output
    assert "missing source secret: MISSING_SECRET" in output
    assert read_plan(plan_path)["status"] == "planned"
    assert '"status":"failure"' in audit_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_github_write_checks_vault_but_does_not_call_github(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        github_plan("preflight-write", status="approved", action="github.env_secret.write"),
    )
    fake_bin = write_fake_infisical(tmp_path)
    write_marker_gh(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-write",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: github.env_secret.write" in output
    assert f"source secret exists: {DUMMY_SECRET_NAME}" in output
    assert "GitHub API call was not made" in output
    assert not (tmp_path / "gh-called").exists()
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_sync_many_succeeds_and_does_not_call_github(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, sync_many_plan("preflight-sync", status="rejected"))
    fake_bin = write_fake_infisical(tmp_path)
    write_marker_gh(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-sync",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: github.env_secret.sync_many" in output
    assert "environment: dev" in output
    assert "mappings checked: 1" in output
    assert "missing source secrets: none" in output
    assert "GitHub API call was not made" in output
    assert not (tmp_path / "gh-called").exists()
    assert read_plan(plan_path)["status"] == "rejected"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_sync_many_reports_missing_sources_safely(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = sync_many_plan("preflight-sync-missing", status="approved")
    plan["secret_mappings"] = [
        {"source": DUMMY_SECRET_NAME, "target": DUMMY_SECRET_NAME},
        {"source": "MISSING_SECRET", "target": "MISSING_SECRET"},
    ]
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_fake_infisical(tmp_path)
    write_marker_gh(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-sync-missing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: github.env_secret.sync_many" in output
    assert "environment: dev" in output
    assert "mappings checked: 2" in output
    assert "missing source secrets: MISSING_SECRET" in output
    assert "GitHub API call was not made" in output
    assert not (tmp_path / "gh-called").exists()
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def write_existing_generate_secret_infisical(tmp_path):
    fake_infisical = tmp_path / "infisical"
    fake_infisical.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "if 'secrets' in sys.argv and 'set' in sys.argv:\n"
        "    from pathlib import Path\n"
        f"    Path({str(tmp_path / 'infisical-set-called')!r}).write_text('called', encoding='utf-8')\n"
        "    raise SystemExit(77)\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "child_env = os.environ.copy()\n"
        "child_env['GENERATED_DUMMY_PASSWORD'] = 'existing-placeholder'\n"
        "raise SystemExit(subprocess.run(command, env=child_env).returncode)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return tmp_path


def write_counting_missing_generate_infisical(tmp_path):
    fake_infisical = tmp_path / "infisical"
    exists_marker = tmp_path / "infisical-exists-called"
    set_marker = tmp_path / "infisical-set-called"
    fake_infisical.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "if 'secrets' in sys.argv and 'set' in sys.argv:\n"
        f"    Path({str(set_marker)!r}).write_text('called', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        f"Path({str(exists_marker)!r}).write_text('called', encoding='utf-8')\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "raise SystemExit(subprocess.run(command).returncode)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return tmp_path


def write_fake_infisical_for_generate(tmp_path):
    fake_infisical = tmp_path / "infisical"
    record_path = tmp_path / "infisical-generate-call.json"
    fake_infisical.write_text(
        f"#!{sys.executable}\n"
        f"import sys; sys.path.insert(0, {TESTS_DIR!r})\n"
        "from fake_infisical import SetRejected, parse_set_args\n"
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "import os\n"
        "args = sys.argv[1:]\n"
        f"record_path = Path({str(record_path)!r})\n"
        "if 'run' in args:\n"
        "    command = sys.argv[sys.argv.index('--') + 1:]\n"
        "    child_env = os.environ.copy()\n"
        "    if record_path.exists():\n"
        "        child_env['GENERATED_DUMMY_PASSWORD'] = json.loads(record_path.read_text(encoding='utf-8'))['generated_value']\n"
        "    raise SystemExit(subprocess.run(command, env=child_env).returncode)\n"
        "file_arg = next(arg for arg in args if arg.startswith('--file='))\n"
        "temp_path = Path(file_arg.split('=', 1)[1])\n"
        "try:\n"
        "    stored_now, _ = parse_set_args(args[args.index('set') + 1:])\n"
        "except SetRejected:\n"
        "    raise SystemExit(1)\n"
        "value = stored_now['GENERATED_DUMMY_PASSWORD']\n"
        "record = {\n"
        "    'args': args,\n"
        "    'secret_assignment': file_arg,\n"
        "    'secret_value_in_args': value in args,\n"
        "    'temp_path': str(temp_path),\n"
        "    'temp_file_exists_during_call': temp_path.exists(),\n"
        "    'temp_value_non_empty': value != '',\n"
        "    'generated_value': value,\n"
        "}\n"
        f"Path({str(record_path)!r}).write_text(json.dumps(record), encoding='utf-8')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return record_path


def write_failing_infisical_for_generate(tmp_path):
    fake_infisical = tmp_path / "infisical"
    record_path = tmp_path / "infisical-generate-failed-call.json"
    fake_infisical.write_text(
        f"#!{sys.executable}\n"
        f"import sys; sys.path.insert(0, {TESTS_DIR!r})\n"
        "from fake_infisical import SetRejected, parse_set_args\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "import subprocess\n"
        "args = sys.argv[1:]\n"
        "if 'run' in args:\n"
        "    command = sys.argv[sys.argv.index('--') + 1:]\n"
        "    raise SystemExit(subprocess.run(command).returncode)\n"
        "file_arg = next(arg for arg in args if arg.startswith('--file='))\n"
        "temp_path = Path(file_arg.split('=', 1)[1])\n"
        "try:\n"
        "    stored_now, _ = parse_set_args(args[args.index('set') + 1:])\n"
        "except SetRejected:\n"
        "    raise SystemExit(1)\n"
        "value = stored_now['GENERATED_DUMMY_PASSWORD']\n"
        "record = {'args': args, 'temp_path': str(temp_path), 'generated_value': value}\n"
        f"Path({str(record_path)!r}).write_text(json.dumps(record), encoding='utf-8')\n"
        "raise SystemExit(44)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return record_path


def write_fake_infisical_for_rotate(tmp_path):
    fake_infisical = tmp_path / "infisical"
    record_path = tmp_path / "infisical-rotate-call.json"
    order_path = tmp_path / "infisical-rotate-order.txt"
    fake_infisical.write_text(
        f"#!{sys.executable}\n"
        f"import sys; sys.path.insert(0, {TESTS_DIR!r})\n"
        "from fake_infisical import SetRejected, parse_set_args\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"record_path = Path({str(record_path)!r})\n"
        f"order_path = Path({str(order_path)!r})\n"
        "args = sys.argv[1:]\n"
        "if 'run' in args:\n"
        "    order_path.write_text(order_path.read_text(encoding='utf-8') + 'exists\\n' if order_path.exists() else 'exists\\n', encoding='utf-8')\n"
        "    command = sys.argv[sys.argv.index('--') + 1:]\n"
        "    child_env = os.environ.copy()\n"
        "    child_env['GENERATED_DUMMY_PASSWORD'] = json.loads(record_path.read_text(encoding='utf-8'))['rotated_value'] if record_path.exists() else 'existing-placeholder'\n"
        "    raise SystemExit(subprocess.run(command, env=child_env).returncode)\n"
        "order_path.write_text(order_path.read_text(encoding='utf-8') + 'set\\n', encoding='utf-8')\n"
        "file_arg = next(arg for arg in args if arg.startswith('--file='))\n"
        "temp_path = Path(file_arg.split('=', 1)[1])\n"
        "try:\n"
        "    stored_now, _ = parse_set_args(args[args.index('set') + 1:])\n"
        "except SetRejected:\n"
        "    raise SystemExit(1)\n"
        "value = stored_now['GENERATED_DUMMY_PASSWORD']\n"
        "record = {\n"
        "    'args': args,\n"
        "    'secret_assignment': file_arg,\n"
        "    'secret_value_in_args': value in args,\n"
        "    'temp_path': str(temp_path),\n"
        "    'temp_file_exists_during_call': temp_path.exists(),\n"
        "    'temp_value_non_empty': value != '',\n"
        "    'rotated_value': value,\n"
        "    'order': order_path.read_text(encoding='utf-8').splitlines(),\n"
        "}\n"
        "record_path.write_text(json.dumps(record), encoding='utf-8')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return record_path


def test_unapproved_generate_plan_cannot_execute(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, generate_plan("unapproved-generate"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "unapproved-generate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "vault.secret.generate plan requires approval" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_approved_generate_plan_executes_with_fake_infisical(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    record_path = write_fake_infisical_for_generate(tmp_path)
    plan_path = write_plan(plan_dir, generate_plan("approved-generate", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "approved-generate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    generated_value = record["generated_value"]
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        "OK generated secret stored: GENERATED_DUMMY_PASSWORD",
        "secret value was not printed",
    ]
    assert record["args"][:5] == [
        "--domain",
        "https://eu.infisical.com",
        "secrets",
        "set",
        record["secret_assignment"],
    ]
    assert record["secret_assignment"].startswith("--file=")
    assert record["secret_assignment"].endswith(".yaml")
    assert not any(arg.startswith("GENERATED_DUMMY_PASSWORD=") for arg in record["args"])
    assert record["secret_value_in_args"] is False
    assert record["temp_file_exists_during_call"] is True
    assert record["temp_value_non_empty"] is True
    assert not os.path.exists(record["temp_path"])
    assert read_plan(plan_path)["status"] == "executed"
    assert generated_value not in result.stdout
    assert generated_value not in result.stderr
    assert generated_value not in audit_path.read_text(encoding="utf-8")
    assert generated_value not in plan_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_approved_generate_plan_fails_create_only_when_target_exists(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = generate_plan("existing-generate", status="approved")
    plan["generator"] = {"type": "unsupported", "length": 32}
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_existing_generate_secret_infisical(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "existing-generate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL generated secret already exists: GENERATED_DUMMY_PASSWORD" in output
    assert "create_mode: create_only" in output
    assert "secret value was not generated" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-set-called").exists()
    assert "unsupported generator" not in output
    assert_no_secret_leak(result, plan_path, audit_path)


def test_generate_with_cli_failure_after_start_is_outcome_unknown(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    record_path = write_failing_infisical_for_generate(tmp_path)
    plan_path = write_plan(plan_dir, generate_plan("failed-generate", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "failed-generate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    generated_value = record["generated_value"]
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL generated secret write outcome unknown: GENERATED_DUMMY_PASSWORD" in output
    assert "check GENERATED_DUMMY_PASSWORD before any retry" in output
    assert "was not stored" not in output
    assert read_plan(plan_path)["status"] == "failed"
    assert '"status":"outcome_unknown"' in audit_path.read_text(encoding="utf-8")
    assert not os.path.exists(record["temp_path"])
    assert generated_value not in result.stdout
    assert generated_value not in result.stderr
    assert generated_value not in audit_path.read_text(encoding="utf-8")
    assert generated_value not in plan_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_rotate_passes_when_target_secret_exists(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, rotate_plan("preflight-rotate-existing", status="approved"))
    fake_bin = write_existing_generate_secret_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-rotate-existing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: vault.secret.rotate" in output
    assert "environment: dev" in output
    assert "target secret: GENERATED_DUMMY_PASSWORD" in output
    assert "rotation_mode: replace_existing" in output
    assert "target secret exists: True" in output
    assert "rotation can proceed later" in output
    assert "secret value was not generated" in output
    assert not (tmp_path / "infisical-set-called").exists()
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_rotate_fails_when_target_secret_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, rotate_plan("preflight-rotate-missing", status="approved"))
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-rotate-missing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: vault.secret.rotate" in output
    assert "environment: dev" in output
    assert "target secret: GENERATED_DUMMY_PASSWORD" in output
    assert "rotation_mode: replace_existing" in output
    assert "target secret exists: False" in output
    assert "use vault.secret.generate instead" in output
    assert "secret value was not generated" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_unapproved_rotate_plan_cannot_execute(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, rotate_plan("unapproved-rotate"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "unapproved-rotate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "vault.secret.rotate plan requires approval" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_rejected_rotate_plan_cannot_execute(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, rotate_plan("rejected-rotate", status="rejected"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "rejected-rotate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "rejected plan cannot execute" in output
    assert read_plan(plan_path)["status"] == "rejected"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_approved_rotate_plan_fails_when_target_secret_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = rotate_plan("missing-rotate", status="approved")
    plan["generator"] = {"type": "unsupported", "length": 32}
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_counting_missing_generate_infisical(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "missing-rotate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL rotated secret target is missing: GENERATED_DUMMY_PASSWORD" in output
    assert "use vault.secret.generate instead" in output
    assert "secret value was not generated" in output
    assert "unsupported generator" not in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-set-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_approved_rotate_plan_executes_with_fake_infisical(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    record_path = write_fake_infisical_for_rotate(tmp_path)
    plan_path = write_plan(plan_dir, rotate_plan("approved-rotate", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "approved-rotate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    rotated_value = record["rotated_value"]
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        "OK rotated secret stored: GENERATED_DUMMY_PASSWORD",
        "secret value was not printed",
    ]
    assert record["order"] == ["exists", "set"]
    assert record["args"][:5] == [
        "--domain",
        "https://eu.infisical.com",
        "secrets",
        "set",
        record["secret_assignment"],
    ]
    assert record["secret_assignment"].startswith("--file=")
    assert record["secret_assignment"].endswith(".yaml")
    assert not any(arg.startswith("GENERATED_DUMMY_PASSWORD=") for arg in record["args"])
    assert record["secret_value_in_args"] is False
    assert record["temp_file_exists_during_call"] is True
    assert record["temp_value_non_empty"] is True
    assert not os.path.exists(record["temp_path"])
    assert read_plan(plan_path)["status"] == "executed"
    assert rotated_value not in result.stdout
    assert rotated_value not in result.stderr
    assert rotated_value not in audit_path.read_text(encoding="utf-8")
    assert rotated_value not in plan_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)



def write_existing_import_target_infisical(tmp_path):
    fake_infisical = tmp_path / "infisical"
    fake_infisical.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "if 'secrets' in sys.argv and 'set' in sys.argv:\n"
        "    raise SystemExit(77)\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "child_env = os.environ.copy()\n"
        "child_env['BACKUP_AWS_ACCESS_KEY_ID'] = 'existing-placeholder'\n"
        "raise SystemExit(subprocess.run(command, env=child_env).returncode)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return tmp_path


def write_fake_infisical_for_import_file(tmp_path, *, existing_names=None, fail_set_name=None):
    fake_infisical = tmp_path / "infisical"
    record_path = tmp_path / "import-file-record.json"
    fake_infisical.write_text(
        f"#!{sys.executable}\n"
        f"import sys; sys.path.insert(0, {TESTS_DIR!r})\n"
        "from fake_infisical import SetRejected, parse_set_args\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"record_path = Path({str(record_path)!r})\n"
        f"existing_names = set({list(existing_names or [])!r})\n"
        f"fail_set_name = {fail_set_name!r}\n"
        "def load_record():\n"
        "    return json.loads(record_path.read_text(encoding='utf-8')) if record_path.exists() else {'sets': [], 'exists_checks': []}\n"
        "def save_record(record):\n"
        "    record_path.write_text(json.dumps(record), encoding='utf-8')\n"
        "args = sys.argv[1:]\n"
        "if 'run' in args:\n"
        "    name = args[-1]\n"
        "    record = load_record()\n"
        "    record['exists_checks'].append(name)\n"
        "    save_record(record)\n"
        "    command = sys.argv[sys.argv.index('--') + 1:]\n"
        "    child_env = os.environ.copy()\n"
        "    if name in existing_names:\n"
        "        child_env[name] = 'existing-placeholder'\n"
        "    child_env.update(record.get('stored', {}))\n"
        "    raise SystemExit(subprocess.run(command, env=child_env).returncode)\n"
        "if 'secrets' in args and 'set' in args:\n"
        "    temp_path = next(arg for arg in args if arg.startswith('--file=')).split('=', 1)[1]\n"
        "    try:\n"
        "        stored_now, _ = parse_set_args(args[args.index('set') + 1:])\n"
        "    except SetRejected:\n"
        "        raise SystemExit(1)\n"
        "    [(name, value)] = stored_now.items()\n"
        "    record = load_record()\n"
        "    record['sets'].append({\n"
        "        'name': name,\n"
        "        'value': value,\n"
        "        'env': args[args.index('--env') + 1],\n"
        "        'secret_value_in_args': value in ' '.join(args),\n"
        "        'temp_file_exists_during_call': Path(temp_path).exists(),\n"
        "    })\n"
        "    if name != fail_set_name:\n"
        "        record.setdefault('stored', {})[name] = value\n"
        "    save_record(record)\n"
        "    raise SystemExit(1 if name == fail_set_name else 0)\n"
        "raise SystemExit(99)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return record_path


def test_preflight_import_file_passes_when_source_exists_and_target_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "imported-secret.txt"
    source_file.write_text("BACKUP_AWS_ACCESS_KEY_ID=not-a-real-secret\n", encoding="utf-8")
    source_file.chmod(0o600)
    plan_path = write_plan(plan_dir, import_file_plan("preflight-import-file", status="approved", source_file=str(source_file)))
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-import-file",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: vault.secret.import_file" in output
    assert "environment: staging" in output
    assert f"source_file: {source_file}" in "".join(output.splitlines())
    assert "source file exists: True" in output
    assert "source file is file: True" in output
    assert "create_mode: create_only" in output
    assert "existing target secrets: none" in output
    assert "import can proceed later" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_import_file_fails_safely_when_source_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "missing-secret.txt"
    plan_path = write_plan(plan_dir, import_file_plan("preflight-import-file-missing", status="approved", source_file=str(source_file)))
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-import-file-missing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: vault.secret.import_file" in output
    assert "source file exists: False" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_import_file_fails_safely_when_create_only_target_exists(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "imported-secret.txt"
    source_file.write_text("BACKUP_AWS_ACCESS_KEY_ID=not-a-real-secret\n", encoding="utf-8")
    source_file.chmod(0o600)
    plan_path = write_plan(plan_dir, import_file_plan("preflight-import-file-existing", status="approved", source_file=str(source_file)))
    fake_bin = write_existing_import_target_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-import-file-existing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: vault.secret.import_file" in output
    assert "existing target secrets: BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "execution would not overwrite existing secrets" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_import_file_imports_dotenv_without_leaking_values(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "imported.env"
    import_values = {
        "BACKUP_AWS_ACCESS_KEY_ID": "import-access-id",
        "BACKUP_AWS_REGION": "eu-central-1",
    }
    source_file.write_text(
        "# bootstrap secrets\n"
        "BACKUP_AWS_ACCESS_KEY_ID=import-access-id\n"
        "BACKUP_AWS_REGION=eu-central-1\n",
        encoding="utf-8",
    )
    source_file.chmod(0o600)
    plan_path = write_plan(
        plan_dir,
        import_file_plan(
            "execute-import-file",
            status="approved",
            source_file=str(source_file),
            secret_mappings=[{"name": name} for name in import_values],
        ),
    )
    record_path = write_fake_infisical_for_import_file(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-import-file",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK imported dotenv secrets: 2" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "executed"
    assert [(item["name"], item["env"], item["value"]) for item in record["sets"]] == [
        ("BACKUP_AWS_ACCESS_KEY_ID", "staging", "import-access-id"),
        ("BACKUP_AWS_REGION", "staging", "eu-central-1"),
    ]
    assert all(item["temp_file_exists_during_call"] for item in record["sets"])
    assert all(not item["secret_value_in_args"] for item in record["sets"])
    for value in import_values.values():
        assert value not in output
        assert value not in plan_path.read_text(encoding="utf-8")
        assert value not in audit_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_import_file_rejects_invalid_dotenv_key_without_provider_call(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "invalid.env"
    source_file.write_text("bad-key=secret-value\n", encoding="utf-8")
    source_file.chmod(0o600)
    plan_path = write_plan(plan_dir, import_file_plan("execute-invalid-import", status="approved", source_file=str(source_file)))
    record_path = write_fake_infisical_for_import_file(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-invalid-import",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    output = result.stdout + result.stderr
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {"sets": []}
    assert result.returncode != 0
    assert "FAIL vault.secret.import_file safety check failed" in output
    assert "invalid dotenv key name" in output
    assert "secret-value" not in output
    assert record["sets"] == []
    assert read_plan(plan_path)["status"] == "failed"
    assert "secret-value" not in audit_path.read_text(encoding="utf-8")


def test_execute_import_file_rejects_duplicate_dotenv_keys_deterministically(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "duplicate.env"
    source_file.write_text(
        "BACKUP_AWS_REGION=first-secret-value\n"
        "BACKUP_AWS_REGION=second-secret-value\n",
        encoding="utf-8",
    )
    source_file.chmod(0o600)
    plan_path = write_plan(plan_dir, import_file_plan("execute-duplicate-import", status="approved", source_file=str(source_file)))
    record_path = write_fake_infisical_for_import_file(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-duplicate-import",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    output = result.stdout + result.stderr
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {"sets": []}
    assert result.returncode != 0
    assert "duplicate dotenv key" in output
    assert "first-secret-value" not in output
    assert "second-secret-value" not in output
    assert record["sets"] == []
    assert read_plan(plan_path)["status"] == "failed"


def test_preflight_import_file_rejects_world_readable_source(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "world-readable.env"
    source_file.write_text("BACKUP_AWS_REGION=secret-region\n", encoding="utf-8")
    source_file.chmod(0o644)
    plan_path = write_plan(plan_dir, import_file_plan("preflight-world-readable-import", status="approved", source_file=str(source_file)))
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-world-readable-import",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "source file permissions restricted: False" in output
    assert "secret-region" not in output
    assert read_plan(plan_path)["status"] == "approved"
    assert "secret-region" not in audit_path.read_text(encoding="utf-8")


def test_execute_import_file_create_only_refuses_existing_targets(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "existing.env"
    source_file.write_text("BACKUP_AWS_ACCESS_KEY_ID=new-secret-value\n", encoding="utf-8")
    source_file.chmod(0o600)
    plan_path = write_plan(plan_dir, import_file_plan("execute-existing-import", status="approved", source_file=str(source_file)))
    record_path = write_fake_infisical_for_import_file(tmp_path, existing_names=["BACKUP_AWS_ACCESS_KEY_ID"])

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-existing-import",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    output = result.stdout + result.stderr
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert result.returncode != 0
    assert "existing target secrets: BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "execution would not overwrite existing secrets" in output
    assert "new-secret-value" not in output
    assert record["sets"] == []
    assert read_plan(plan_path)["status"] == "failed"


def test_preflight_import_file_rejects_names_changed_since_plan(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "changed.env"
    source_file.write_text("BACKUP_AWS_ACCESS_KEY_ID=original-secret\n", encoding="utf-8")
    source_file.chmod(0o600)
    plan_path = write_plan(plan_dir, import_file_plan("preflight-changed-import", status="approved", source_file=str(source_file)))
    source_file.write_text("BACKUP_AWS_REGION=changed-secret\n", encoding="utf-8")
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-changed-import",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "source file names do not match saved plan" in output
    assert "original-secret" not in output
    assert "changed-secret" not in output
    assert read_plan(plan_path)["status"] == "approved"
    assert "changed-secret" not in audit_path.read_text(encoding="utf-8")


def test_execute_import_file_rejects_empty_planned_names_even_if_file_exists(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "unreviewed.env"
    source_file.write_text("BACKUP_AWS_REGION=unreviewed-secret\n", encoding="utf-8")
    source_file.chmod(0o600)
    plan = import_file_plan("execute-empty-planned-names", status="approved", source_file=str(source_file))
    plan.pop("secret_name")
    plan.pop("secret_mappings")
    plan_path = write_plan(plan_dir, plan)
    record_path = write_fake_infisical_for_import_file(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-empty-planned-names",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    output = result.stdout + result.stderr
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {"sets": []}
    assert result.returncode != 0
    assert "plan file does not match the plan schema" in output
    assert "unreviewed-secret" not in output
    assert record["sets"] == []
    # Refused by the plan schema; the file is left as is.
    assert read_plan(plan_path)["status"] == "approved"
    assert "unreviewed-secret" not in audit_path.read_text(encoding="utf-8")


def test_execute_import_file_rejects_names_changed_since_plan(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "execute-changed.env"
    source_file.write_text("BACKUP_AWS_ACCESS_KEY_ID=original-secret\n", encoding="utf-8")
    source_file.chmod(0o600)
    plan_path = write_plan(plan_dir, import_file_plan("execute-changed-import", status="approved", source_file=str(source_file)))
    source_file.write_text("BACKUP_AWS_REGION=changed-secret\n", encoding="utf-8")
    record_path = write_fake_infisical_for_import_file(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-changed-import",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    output = result.stdout + result.stderr
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {"sets": []}
    assert result.returncode != 0
    assert "source file names do not match saved plan" in output
    assert "original-secret" not in output
    assert "changed-secret" not in output
    assert record["sets"] == []
    assert read_plan(plan_path)["status"] == "failed"


def write_fake_infisical_for_copy_many(tmp_path, *, missing_source=None, existing_target=None, fail_set=False):
    fake_infisical = tmp_path / "infisical"
    record_path = tmp_path / "copy-many-record.json"
    source_values = COPY_SOURCE_VALUES
    fake_infisical.write_text(
        f"#!{sys.executable}\n"
        f"import sys; sys.path.insert(0, {TESTS_DIR!r})\n"
        "from fake_infisical import SetRejected, parse_set_args\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"record_path = Path({str(record_path)!r})\n"
        f"source_values = {source_values!r}\n"
        f"missing_source = {missing_source!r}\n"
        f"existing_target = {existing_target!r}\n"
        f"fail_set = {fail_set!r}\n"
        "def load_record():\n"
        "    return json.loads(record_path.read_text(encoding='utf-8')) if record_path.exists() else {'sets': []}\n"
        "def save_record(record):\n"
        "    record_path.write_text(json.dumps(record), encoding='utf-8')\n"
        "args = sys.argv[1:]\n"
        "if 'run' in args:\n"
        "    env_arg = next(arg for arg in args if arg.startswith('--env='))\n"
        "    env_name = env_arg.split('=', 1)[1]\n"
        "    command = sys.argv[sys.argv.index('--') + 1:]\n"
        "    child_env = os.environ.copy()\n"
        "    if env_name == 'dev':\n"
        "        for key, value in source_values.items():\n"
        "            if key != missing_source:\n"
        "                child_env[key] = value\n"
        "    if env_name == 'staging' and existing_target:\n"
        "        child_env[existing_target] = 'existing-target-placeholder'\n"
        "    child_env.update(load_record().get('stored', {}).get(env_name, {}))\n"
        "    raise SystemExit(subprocess.run(command, env=child_env).returncode)\n"
        "if 'secrets' in args and 'set' in args:\n"
        "    temp_path = next(arg for arg in args if arg.startswith('--file=')).split('=', 1)[1]\n"
        "    try:\n"
        "        stored_now, _ = parse_set_args(args[args.index('set') + 1:])\n"
        "    except SetRejected:\n"
        "        raise SystemExit(1)\n"
        "    [(name, value)] = stored_now.items()\n"
        "    record = load_record()\n"
        "    record['sets'].append({\n"
        "        'args': args,\n"
        "        'name': name,\n"
        "        'env': args[args.index('--env') + 1],\n"
        "        'secret_value_in_args': value in args,\n"
        "        'temp_file_exists_during_call': Path(temp_path).exists(),\n"
        "        'value': value,\n"
        "    })\n"
        "    if not fail_set:\n"
        "        record.setdefault('stored', {}).setdefault(args[args.index('--env') + 1], {})[name] = value\n"
        "    save_record(record)\n"
        "    raise SystemExit(42 if fail_set else 0)\n"
        "raise SystemExit(99)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return record_path


def test_preflight_copy_many_passes_when_sources_exist_and_targets_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, copy_many_plan("preflight-copy-many", status="approved"))
    fake_bin = write_fake_infisical_for_copy_many(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-copy-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin.parent,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: vault.secret.copy_many" in output
    assert "source_env: dev" in output
    assert "target_env: staging" in output
    assert "mappings checked: 4" in output
    assert "missing source secrets: none" in output
    assert "existing target secrets: none" in output
    assert "secret values were not read" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_copy_value_leak(result, plan_path, audit_path)


def test_preflight_copy_many_fails_when_source_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, copy_many_plan("preflight-copy-many-missing", status="approved"))
    fake_bin = write_fake_infisical_for_copy_many(tmp_path, missing_source="BACKUP_S3_BUCKET")

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-copy-many-missing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin.parent,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: vault.secret.copy_many" in output
    assert "missing source secrets: BACKUP_S3_BUCKET" in output
    assert "existing target secrets: none" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_copy_value_leak(result, plan_path, audit_path)


def test_preflight_copy_many_fails_when_create_only_target_exists(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, copy_many_plan("preflight-copy-many-existing", status="approved"))
    fake_bin = write_fake_infisical_for_copy_many(tmp_path, existing_target="BACKUP_AWS_REGION")

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-copy-many-existing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin.parent,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: vault.secret.copy_many" in output
    assert "missing source secrets: none" in output
    assert "existing target secrets: BACKUP_AWS_REGION" in output
    assert "execution would not overwrite existing secrets" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_copy_value_leak(result, plan_path, audit_path)


def test_unapproved_copy_many_cannot_execute(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, copy_many_plan("unapproved-copy-many"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "unapproved-copy-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "vault.secret.copy_many plan requires approval" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_copy_value_leak(result, plan_path, audit_path)


def test_approved_copy_many_executes_with_fake_infisical(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    record_path = write_fake_infisical_for_copy_many(tmp_path)
    plan_path = write_plan(plan_dir, copy_many_plan("approved-copy-many", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "approved-copy-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        "OK vault secrets copied: 4",
        "secret values were not printed",
    ]
    assert read_plan(plan_path)["status"] == "executed"
    assert len(record["sets"]) == 4
    assert [entry["name"] for entry in record["sets"]] == [
        "BACKUP_AWS_ACCESS_KEY_ID",
        "BACKUP_AWS_SECRET_ACCESS_KEY",
        "BACKUP_S3_BUCKET",
        "BACKUP_AWS_REGION",
    ]
    assert {entry["env"] for entry in record["sets"]} == {"staging"}
    assert all(entry["secret_value_in_args"] is False for entry in record["sets"])
    assert all(entry["temp_file_exists_during_call"] is True for entry in record["sets"])
    assert [entry["value"] for entry in record["sets"]] == list(COPY_SOURCE_VALUES.values())
    audit_text = audit_path.read_text(encoding="utf-8")
    assert '"status":"success"' in audit_text
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_copy_value_leak(result, plan_path, audit_path)


def test_copy_many_stops_after_unknown_write_and_reports_partial_result(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    record_path = write_fake_infisical_for_copy_many(tmp_path, fail_set=True)
    plan_path = write_plan(plan_dir, copy_many_plan("failed-copy-many", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "failed-copy-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL copied secret write outcome unknown: BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "written and confirmed: none" in output
    assert (
        "not written: BACKUP_AWS_SECRET_ACCESS_KEY, BACKUP_S3_BUCKET, BACKUP_AWS_REGION" in output
    )
    assert "no automatic rollback or retry was attempted" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert len(record["sets"]) == 1
    assert record["sets"][0]["secret_value_in_args"] is False
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    stop_event = events[-1]
    assert stop_event["status"] == "outcome_unknown"
    assert stop_event["secret_name"] == "BACKUP_AWS_ACCESS_KEY_ID"
    assert stop_event["env"] == "staging"
    assert stop_event["confirmed_names"] == []
    assert stop_event["not_written_names"] == [
        "BACKUP_AWS_SECRET_ACCESS_KEY",
        "BACKUP_S3_BUCKET",
        "BACKUP_AWS_REGION",
    ]
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_copy_value_leak(result, plan_path, audit_path)


def test_preflight_generate_passes_when_target_secret_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, generate_plan("preflight-generate", status="approved"))
    fake_bin = write_fake_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-generate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: vault.secret.generate" in output
    assert "environment: dev" in output
    assert "target secret: GENERATED_DUMMY_PASSWORD" in output
    assert "create_mode: create_only" in output
    assert "target secret exists: False" in output
    assert "generation can proceed" in output
    assert "secret value was not generated" in output
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_preflight_generate_fails_when_target_secret_exists(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, generate_plan("preflight-generate-existing", status="approved"))
    fake_bin = write_existing_generate_secret_infisical(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-generate-existing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: vault.secret.generate" in output
    assert "environment: dev" in output
    assert "target secret: GENERATED_DUMMY_PASSWORD" in output
    assert "create_mode: create_only" in output
    assert "target secret exists: True" in output
    assert "execution would not overwrite existing secret" in output
    assert "secret value was not generated" in output
    assert not (tmp_path / "infisical-set-called").exists()
    assert read_plan(plan_path)["status"] == "approved"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_plan_succeeds_for_saved_vault_read_check_plan(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    fake_bin = write_fake_infisical(tmp_path)
    save_result = run_secretops(
        "plan",
        "--file",
        VAULT_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )
    plan_path = saved_plan_file(plan_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    result = run_secretops(
        "execute-plan",
        "--id",
        plan["plan_id"],
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert save_result.returncode == 0
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        f"OK secret exists: {DUMMY_SECRET_NAME}",
        "secret value was not printed",
    ]
    audit_text = audit_path.read_text(encoding="utf-8")
    assert plan["plan_id"] in audit_text
    assert '"status":"success"' in audit_text
    assert_no_secret_leak(result, plan_path, audit_path)


def write_fake_gh(tmp_path):
    fake_gh = tmp_path / "gh"
    gh_call_path = tmp_path / "gh-call.json"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "stdin = sys.stdin.read()\n"
        f"expected = {DUMMY_SECRET_VALUE!r}\n"
        "record = {\n"
        "    'args': sys.argv[1:],\n"
        "    'stdin_matches_secret': stdin == expected,\n"
        "    'secret_in_args': expected in sys.argv[1:] if expected else False,\n"
        "}\n"
        f"open({str(gh_call_path)!r}, 'w', encoding='utf-8').write(json.dumps(record))\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    return gh_call_path


def write_fake_gh_secret_list(tmp_path, names):
    fake_gh = tmp_path / "gh"
    gh_call_path = tmp_path / "gh-list-call.json"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"expected_value = {DUMMY_SECRET_VALUE!r}\n"
        "record = {\n"
        "    'args': sys.argv[1:],\n"
        "    'stdin': sys.stdin.read(),\n"
        "    'secret_value_in_args': expected_value in sys.argv[1:] if expected_value else False,\n"
        "}\n"
        f"open({str(gh_call_path)!r}, 'w', encoding='utf-8').write(json.dumps(record))\n"
        "expected_args = ['secret', 'list', '--repo', 'github.com/example-owner/secretops', '--env', 'staging']\n"
        "if sys.argv[1:] != expected_args:\n"
        "    raise SystemExit(2)\n"
        f"names = {names!r}\n"
        "for name in names:\n"
        "    print(name + '\\t2026-01-01T00:00:00Z')\n"
        "print(expected_value, file=sys.stderr)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    return gh_call_path


def write_failing_gh(tmp_path):
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    return tmp_path


def test_verify_many_plan_can_be_saved_with_expected_names_only(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        GITHUB_VERIFY_MANY_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = read_plan(plan_path)
    assert result.returncode == 0
    assert plan["action"] == "github.env_secret.verify_many"
    assert plan["env"] == "staging"
    assert "secret_name" not in plan
    assert plan["secret_value_included"] is False
    assert plan["target"] == {
        "target_host": "github.com",
        "target_owner": "example-owner",
        "target_repo": "secretops",
        "target_environment": "staging",
        "api_call_made": False,
        "secret_value_printed": False,
    }
    assert plan["secret_mappings"] == [
        {"name": "BACKUP_AWS_ACCESS_KEY_ID"},
        {"name": "BACKUP_AWS_SECRET_ACCESS_KEY"},
        {"name": "BACKUP_S3_BUCKET"},
        {"name": "BACKUP_AWS_REGION"},
    ]
    assert_no_secret_leak(result, plan_path, audit_path)


def test_show_plan_displays_verify_many_expected_names_safely(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, verify_many_plan("show-verify-many"))

    result = run_secretops("show-plan", "--id", "show-verify-many", plan_dir=plan_dir)

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: github.env_secret.verify_many" in output
    assert "source_environment: staging" in output
    assert "target_owner: example-owner" in output
    assert "target_repo: secretops" in output
    assert "target_environment: staging" in output
    assert "expected_secret: BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "expected_secret: BACKUP_AWS_SECRET_ACCESS_KEY" in output
    assert "expected_secret: BACKUP_S3_BUCKET" in output
    assert "expected_secret: BACKUP_AWS_REGION" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert '"secret_mappings"' not in output
    assert DUMMY_SECRET_VALUE not in output
    assert_no_secret_leak(result, plan_path)


def test_preflight_verify_many_requires_no_provider_call(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, verify_many_plan("preflight-verify-many"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-verify-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: github.env_secret.verify_many" in output
    assert "target_owner: example-owner" in output
    assert "target_repo: secretops" in output
    assert "target_environment: staging" in output
    assert "expected secrets configured: 4" in output
    assert "GitHub API call was not made" in output
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert read_plan(plan_path)["status"] == "planned"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_verify_many_succeeds_when_all_expected_names_are_present(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, verify_many_plan("execute-verify-many"))
    gh_call_path = write_fake_gh_secret_list(
        tmp_path,
        [
            "BACKUP_AWS_ACCESS_KEY_ID",
            "BACKUP_AWS_SECRET_ACCESS_KEY",
            "BACKUP_S3_BUCKET",
            "BACKUP_AWS_REGION",
            "EXTRA_SAFE_NAME",
        ],
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-verify-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    output = result.stdout + result.stderr
    gh_call = json.loads(gh_call_path.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert "OK GitHub environment secrets verified: 4" in output
    assert "expected count: 4" in output
    assert "present names: BACKUP_AWS_ACCESS_KEY_ID, BACKUP_AWS_SECRET_ACCESS_KEY, BACKUP_S3_BUCKET, BACKUP_AWS_REGION" in output
    assert "missing names: none" in output
    assert read_plan(plan_path)["status"] == "executed"
    assert '"status":"success"' in audit_path.read_text(encoding="utf-8")
    assert gh_call["args"] == ["secret", "list", "--repo", "github.com/example-owner/secretops", "--env", "staging"]
    assert gh_call["stdin"] == ""
    assert gh_call["secret_value_in_args"] is False
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_verify_many_fails_safely_when_expected_names_are_missing(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, verify_many_plan("execute-verify-many-missing"))
    write_fake_gh_secret_list(
        tmp_path,
        ["BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_S3_BUCKET"],
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-verify-many-missing",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL GitHub environment secret verification missing expected names" in output
    assert "expected count: 4" in output
    assert "present names: BACKUP_AWS_ACCESS_KEY_ID, BACKUP_S3_BUCKET" in output
    assert "missing names: BACKUP_AWS_SECRET_ACCESS_KEY, BACKUP_AWS_REGION" in output
    assert "not-a-real-secret" not in output
    assert read_plan(plan_path)["status"] == "failed"
    assert '"status":"failure"' in audit_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def write_fake_infisical_for_diff_envs(tmp_path, *, missing_source=None, missing_target=None):
    fake_infisical = tmp_path / "infisical"
    expected = [mapping["name"] for mapping in diff_envs_plan()["secret_mappings"]]
    fake_infisical.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        f"expected = {expected!r}\n"
        f"missing_source = {missing_source!r}\n"
        f"missing_target = {missing_target!r}\n"
        "args = sys.argv[1:]\n"
        "if 'secrets' in args or 'get' in args:\n"
        "    raise SystemExit(99)\n"
        "if 'run' not in args:\n"
        "    raise SystemExit(98)\n"
        "env_arg = next(arg for arg in args if arg.startswith('--env='))\n"
        "env_name = env_arg.split('=', 1)[1]\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "child_env = os.environ.copy()\n"
        "for name in expected:\n"
        "    if env_name == 'dev' and name != missing_source:\n"
        "        child_env[name] = 'diff-secret-value'\n"
        "    if env_name == 'staging' and name != missing_target:\n"
        "        child_env[name] = 'diff-secret-value'\n"
        "raise SystemExit(subprocess.run(command, env=child_env).returncode)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return tmp_path


def assert_no_diff_value_leak(result: subprocess.CompletedProcess[str], *paths) -> None:
    assert "diff-secret-value" not in result.stdout
    assert "diff-secret-value" not in result.stderr
    for path in paths:
        if path.exists():
            assert "diff-secret-value" not in path.read_text(encoding="utf-8")


def test_diff_envs_plan_can_be_saved_with_expected_names_only(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        DIFF_ENVS_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = read_plan(plan_path)
    assert result.returncode == 0
    assert plan["action"] == "vault.secret.diff_envs"
    assert plan["env"] == "dev"
    assert plan["source_env"] == "dev"
    assert plan["target_env"] == "staging"
    assert "secret_name" not in plan
    assert plan["secret_value_included"] is False
    assert plan["secret_mappings"] == diff_envs_plan()["secret_mappings"]
    assert "source secret values will NOT be read during planning" in result.stdout
    assert "target secret values will NOT be read during planning" in result.stdout
    assert_no_secret_leak(result, plan_path, audit_path)


def test_show_plan_displays_diff_envs_expected_names_safely(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, diff_envs_plan("show-diff-envs"))

    result = run_secretops("show-plan", "--id", "show-diff-envs", plan_dir=plan_dir)

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: vault.secret.diff_envs" in output
    assert "source_environment: dev" in output
    assert "source_env: dev" in output
    assert "target_env: staging" in output
    assert "expected_secret: BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "expected_secret: BACKUP_AWS_SECRET_ACCESS_KEY" in output
    assert "expected_secret: BACKUP_S3_BUCKET" in output
    assert "expected_secret: BACKUP_AWS_REGION" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert '"secret_mappings"' not in output
    assert_no_secret_leak(result, plan_path)


def test_preflight_diff_envs_reports_all_present_when_both_envs_match(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, diff_envs_plan("preflight-diff-envs"))
    fake_bin = write_fake_infisical_for_diff_envs(tmp_path)

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-diff-envs",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK preflight passed: vault.secret.diff_envs" in output
    assert "expected count: 4" in output
    assert "present in source: BACKUP_AWS_ACCESS_KEY_ID, BACKUP_AWS_SECRET_ACCESS_KEY, BACKUP_S3_BUCKET, BACKUP_AWS_REGION" in output
    assert "missing in source: none" in output
    assert "present in target: BACKUP_AWS_ACCESS_KEY_ID, BACKUP_AWS_SECRET_ACCESS_KEY, BACKUP_S3_BUCKET, BACKUP_AWS_REGION" in output
    assert "missing in target: none" in output
    assert "secret values were not read" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "planned"
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_diff_value_leak(result, plan_path, audit_path)


def test_preflight_diff_envs_reports_missing_target_names(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, diff_envs_plan("preflight-diff-envs-missing-target"))
    fake_bin = write_fake_infisical_for_diff_envs(tmp_path, missing_target="BACKUP_S3_BUCKET")

    result = run_secretops(
        "preflight-plan",
        "--id",
        "preflight-diff-envs-missing-target",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL preflight failed: vault.secret.diff_envs" in output
    assert "expected count: 4" in output
    assert "missing in source: none" in output
    assert "missing in target: BACKUP_S3_BUCKET" in output
    assert "secret values were not read" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "planned"
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_diff_value_leak(result, plan_path, audit_path)


def test_execute_diff_envs_succeeds_when_both_envs_match_without_approval(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, diff_envs_plan("execute-diff-envs"))
    fake_bin = write_fake_infisical_for_diff_envs(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-diff-envs",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "OK vault environment secret names match: 4" in output
    assert "expected count: 4" in output
    assert "missing in source: none" in output
    assert "missing in target: none" in output
    assert "secret values were not read" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "executed"
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_diff_value_leak(result, plan_path, audit_path)


def test_execute_diff_envs_fails_safely_when_source_is_missing_names(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, diff_envs_plan("execute-diff-envs-missing-source"))
    fake_bin = write_fake_infisical_for_diff_envs(tmp_path, missing_source="BACKUP_AWS_REGION")

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-diff-envs-missing-source",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL vault environment secret name diff found missing expected names" in output
    assert "missing in source: BACKUP_AWS_REGION" in output
    assert "missing in target: none" in output
    assert "secret values were not read" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_diff_value_leak(result, plan_path, audit_path)


def test_execute_diff_envs_fails_safely_when_target_is_missing_names(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, diff_envs_plan("execute-diff-envs-missing-target"))
    fake_bin = write_fake_infisical_for_diff_envs(tmp_path, missing_target="BACKUP_AWS_SECRET_ACCESS_KEY")

    result = run_secretops(
        "execute-plan",
        "--id",
        "execute-diff-envs-missing-target",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL vault environment secret name diff found missing expected names" in output
    assert "missing in source: none" in output
    assert "missing in target: BACKUP_AWS_SECRET_ACCESS_KEY" in output
    assert "secret values were not read" in output
    assert "secret values were not printed" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_secret_leak(result, plan_path, audit_path)
    assert_no_diff_value_leak(result, plan_path, audit_path)


def test_sync_many_plan_can_be_saved_with_name_only_mappings(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"

    result = run_secretops(
        "plan",
        "--file",
        GITHUB_SYNC_MANY_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    plan_path = saved_plan_file(plan_dir)
    plan = read_plan(plan_path)
    assert result.returncode == 0
    assert plan["action"] == "github.env_secret.sync_many"
    assert plan["env"] == "dev"
    assert "secret_name" not in plan
    assert plan["secret_value_included"] is False
    assert plan["target"] == {
        "target_host": "github.com",
        "target_owner": "example-owner",
        "target_repo": "secretops",
        "target_environment": "dev",
        "api_call_made": False,
        "secret_value_printed": False,
    }
    assert plan["secret_mappings"] == [
        {"source": DUMMY_SECRET_NAME, "target": DUMMY_SECRET_NAME}
    ]
    assert_no_secret_leak(result, plan_path, audit_path)


def test_show_plan_displays_sync_many_mappings_safely(tmp_path):
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, sync_many_plan("show-sync-many"))

    result = run_secretops(
        "show-plan",
        "--id",
        "show-sync-many",
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: github.env_secret.sync_many" in output
    assert "source_environment: dev" in output
    assert "target_owner: example-owner" in output
    assert "target_repo: secretops" in output
    assert "target_environment: dev" in output
    assert output.count("target_environment: dev") == 1
    assert f"secret_mapping: {DUMMY_SECRET_NAME} -> {DUMMY_SECRET_NAME}" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert '"secret_mappings"' not in output
    assert_no_secret_leak(result, plan_path)


def test_show_plan_displays_sync_many_target_environment_once_with_top_level_metadata(tmp_path):
    plan_dir = tmp_path / "plans"
    plan = sync_many_plan("show-sync-many-staging")
    plan["env"] = "dev"
    plan["target"]["target_environment"] = "staging"
    plan_path = write_plan(plan_dir, plan)

    result = run_secretops(
        "show-plan",
        "--id",
        "show-sync-many-staging",
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert "action: github.env_secret.sync_many" in output
    assert "source_environment: dev" in output
    assert "target_owner: example-owner" in output
    assert "target_repo: secretops" in output
    assert "target_environment: staging" in output
    assert output.count("target_environment: staging") == 1
    assert "target_env: staging" not in output
    assert f"secret_mapping: {DUMMY_SECRET_NAME} -> {DUMMY_SECRET_NAME}" in output
    assert "secret_value_included: False" in output
    assert "{" not in output
    assert '"target"' not in output
    assert_no_secret_leak(result, plan_path)


def test_production_sync_many_execution_denied_before_provider_calls(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = sync_many_plan("production-sync-many", status="approved")
    plan["env"] = "production"
    plan["target"]["target_environment"] = "production"
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "production-sync-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "production execution denied by policy: github.env_secret.sync_many" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_production_rotate_execution_denied_before_generation_or_provider_calls(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan = rotate_plan("production-rotate", status="approved")
    plan["env"] = "production"
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "production-rotate",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "production execution denied by policy: vault.secret.rotate" in output
    assert "secret value was not generated" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_unapproved_sync_many_cannot_execute(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, sync_many_plan("unapproved-sync-many"))
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "unapproved-sync-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "GitHub sync_many plan requires approval" in output
    assert read_plan(plan_path)["status"] == "failed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()
    assert_no_secret_leak(result, plan_path, audit_path)


def test_approved_sync_many_executes_with_fakes(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    gh_call_path = write_fake_gh(tmp_path)
    fake_bin = write_fake_infisical(tmp_path)
    plan_path = write_plan(plan_dir, sync_many_plan("approved-sync-many", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "approved-sync-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    gh_call = json.loads(gh_call_path.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        "OK GitHub writes acknowledged: 1",
        "secret values were not printed",
    ]
    assert read_plan(plan_path)["status"] == "executed"
    assert gh_call["args"] == [
        "secret",
        "set",
        DUMMY_SECRET_NAME,
        "--repo",
        "github.com/example-owner/secretops",
        "--env",
        "dev",
    ]
    assert gh_call["stdin_matches_secret"] is True
    assert gh_call["secret_in_args"] is False
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "approved-sync-many" in audit_text
    assert '"status":"success"' in audit_text
    assert_no_secret_leak(result, plan_path, audit_path)


def test_executed_sync_many_plan_cannot_run_again_and_keeps_its_status(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    gh_call_path = write_fake_gh(tmp_path)
    fake_bin = write_fake_infisical(tmp_path)
    plan_path = write_plan(plan_dir, sync_many_plan("replayed-sync-many", status="approved"))

    def run():
        return run_secretops(
            "execute-plan",
            "--id",
            "replayed-sync-many",
            audit_path=audit_path,
            plan_dir=plan_dir,
            path_prefix=fake_bin,
        )

    assert run().returncode == 0
    gh_call_path.unlink()
    audit_before = audit_path.read_text(encoding="utf-8")

    result = run()

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "plan already executed" in output
    assert read_plan(plan_path)["status"] == "executed"
    assert not gh_call_path.exists()
    assert audit_path.read_text(encoding="utf-8") == audit_before
    assert_no_secret_leak(result, plan_path, audit_path)


@pytest.mark.parametrize(
    "plan",
    [
        vault_plan("executed-vault", status="executed"),
        rotate_plan("executed-rotate", status="executed"),
        github_plan("executed-github", status="executed", action="github.env_secret.write"),
        verify_many_plan("executed-verify", status="executed"),
    ],
    ids=lambda plan: plan["action"],
)
def test_executed_plan_is_refused_before_any_check(tmp_path, plan):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, plan)
    fake_bin = write_failing_external_commands(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        plan["plan_id"],
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode == 1
    assert "plan already executed" in result.stdout + result.stderr
    assert read_plan(plan_path)["status"] == "executed"
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()


def test_failed_sync_many_execution_updates_status_to_failed(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(plan_dir, sync_many_plan("failed-sync-many", status="approved"))
    fake_bin = write_fake_infisical(tmp_path)
    write_failing_gh(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "failed-sync-many",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    # gh started and exited nonzero: the write may have reached GitHub.
    assert result.returncode == 3
    assert "Sync stopped." in output
    assert "Unknown: 1" in output
    assert "Do not retry automatically" in output
    assert read_plan(plan_path)["status"] == "failed"
    audit_text = audit_path.read_text(encoding="utf-8")
    assert '"status":"outcome_unknown"' in audit_text
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_plan_updates_github_env_secret_from_saved_github_write_plan(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    gh_call_path = write_fake_gh(tmp_path)
    fake_bin = write_fake_infisical(tmp_path)
    save_result = run_secretops(
        "plan",
        "--file",
        GITHUB_WRITE_RECIPE,
        "--save",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )
    plan_path = saved_plan_file(plan_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    approve_result = run_secretops(
        "approve-plan",
        "--id",
        plan["plan_id"],
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        plan["plan_id"],
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    gh_call = json.loads(gh_call_path.read_text(encoding="utf-8"))
    assert save_result.returncode == 0
    assert approve_result.returncode == 0
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == [
        f"OK GitHub write acknowledged: {DUMMY_SECRET_NAME}",
        "secret values were not printed",
    ]
    assert gh_call["args"] == [
        "secret",
        "set",
        DUMMY_SECRET_NAME,
        "--repo",
        "github.com/example-owner/secretops",
        "--env",
        "dev",
    ]
    assert gh_call["stdin_matches_secret"] is True
    assert gh_call["secret_in_args"] is False
    audit_text = audit_path.read_text(encoding="utf-8")
    assert plan["plan_id"] in audit_text
    assert '"status":"success"' in audit_text
    assert_no_secret_leak(result, plan_path, audit_path)


def test_execute_plan_fails_safely_for_unsupported_saved_action(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    plan_path = write_plan(
        plan_dir,
        {
            "plan_id": "unsupported-plan",
            "action": "secret.reveal",
            "secret_name": DUMMY_SECRET_NAME,
            "env": "dev",
            "status": "planned",
            "secret_value_included": False,
        },
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        "unsupported-plan",
        audit_path=audit_path,
        plan_dir=plan_dir,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "plan file does not match the plan schema" in output
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "unsupported-plan" in audit_text
    assert '"status":"failure"' in audit_text
    assert_no_secret_leak(result, plan_path, audit_path)











def test_saved_plan_rejects_invalid_secret_names_before_provider_calls(tmp_path):
    plan_dir = tmp_path / "plans"
    invalid_names = [
        "X') or __import__('os').system('echo pwned')#",
        "BAD-NAME",
        "bad_lowercase",
        "BAD NAME",
        "BAD/NAME",
        "BAD=NAME",
        "",
    ]

    for index, invalid in enumerate(invalid_names):
        plan = vault_plan(plan_id=f"invalid-plan-{index}")
        plan["secret_name"] = invalid
        write_plan(plan_dir, plan)
        result = run_secretops(
            "preflight-plan",
            "--id",
            plan["plan_id"],
            plan_dir=plan_dir,
        )
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "plan file does not match the plan schema" in output
        assert "pwned" not in output
        assert "BAD-NAME" not in output


def test_execute_denied_saved_plan_fails_before_provider_calls(tmp_path):
    plan_dir = tmp_path / "plans"
    audit_path = tmp_path / "audit.jsonl"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command_name in ["infisical", "gh"]:
        command_path = fake_bin / command_name
        command_path.write_text("#!/usr/bin/env sh\necho provider-called >&2\nexit 99\n", encoding="utf-8")
        command_path.chmod(0o700)
    plan = vault_plan(plan_id="denied-exec", status="approved")
    plan["action"] = "secret.reveal"
    write_plan(plan_dir, plan)

    result = run_secretops(
        "execute-plan",
        "--id",
        "denied-exec",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )
    output = result.stdout + result.stderr
    saved = read_plan(plan_dir / "denied-exec.json")

    assert result.returncode != 0
    assert "plan file does not match the plan schema" in output
    assert "provider-called" not in output
    assert saved["status"] == "approved"
    assert_no_secret_leak(result, audit_path)


def test_execute_unknown_saved_plan_fails_before_provider_calls(tmp_path):
    plan_dir = tmp_path / "plans"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_path = fake_bin / "infisical"
    command_path.write_text("#!/usr/bin/env sh\necho provider-called >&2\nexit 99\n", encoding="utf-8")
    command_path.chmod(0o700)
    plan = vault_plan(plan_id="unknown-exec", status="approved")
    plan["action"] = "unknown.action"
    write_plan(plan_dir, plan)

    result = run_secretops(
        "execute-plan",
        "--id",
        "unknown-exec",
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )
    output = result.stdout + result.stderr
    saved = read_plan(plan_dir / "unknown-exec.json")

    assert result.returncode != 0
    assert "plan file does not match the plan schema" in output
    assert "provider-called" not in output
    assert saved["status"] == "approved"


def test_policy_requires_approval_metadata_is_enforced_for_planned_actions(tmp_path):
    requiring_approval = [
        generate_plan("approval-generate"),
        rotate_plan("approval-rotate"),
        import_file_plan("approval-import"),
        copy_many_plan("approval-copy"),
        github_plan("approval-write", action="github.env_secret.write"),
        sync_many_plan("approval-sync"),
    ]
    for plan in requiring_approval:
        plan_dir = tmp_path / plan["plan_id"]
        write_plan(plan_dir, plan)
        result = run_secretops("execute-plan", "--id", plan["plan_id"], plan_dir=plan_dir)
        assert result.returncode != 0
        assert "requires approval" in (result.stdout + result.stderr)


def test_policy_skips_approval_for_metadata_only_actions(tmp_path):
    safe_plans = [verify_many_plan("approval-verify"), diff_envs_plan("approval-diff")]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    infisical = fake_bin / "infisical"
    infisical.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "for name in ['BACKUP_AWS_ACCESS_KEY_ID', 'BACKUP_AWS_SECRET_ACCESS_KEY', 'BACKUP_S3_BUCKET', 'BACKUP_AWS_REGION']:\n"
        "    os.environ[name] = 'present'\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "raise SystemExit(subprocess.run(command).returncode)\n",
        encoding="utf-8",
    )
    infisical.chmod(0o700)
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "print('BACKUP_AWS_ACCESS_KEY_ID\\t2026-01-01')\n"
        "print('BACKUP_AWS_SECRET_ACCESS_KEY\\t2026-01-01')\n"
        "print('BACKUP_S3_BUCKET\\t2026-01-01')\n"
        "print('BACKUP_AWS_REGION\\t2026-01-01')\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)

    for plan in safe_plans:
        plan_dir = tmp_path / plan["plan_id"]
        write_plan(plan_dir, plan)
        result = run_secretops(
            "execute-plan",
            "--id",
            plan["plan_id"],
            plan_dir=plan_dir,
            path_prefix=fake_bin,
        )
        assert result.returncode == 0


def test_github_env_secret_write_uses_saved_plan_secret_and_env(tmp_path):
    plan_dir = tmp_path / "plans"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    infisical = fake_bin / "infisical"
    infisical.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "assert '--env=staging' in sys.argv, sys.argv\n"
        "os.environ['SOURCE_SECRET'] = 'source-secret-value'\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "raise SystemExit(subprocess.run(command).returncode)\n",
        encoding="utf-8",
    )
    infisical.chmod(0o700)
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "assert 'SOURCE_SECRET' not in sys.argv, sys.argv\n"
        "assert 'TARGET_SECRET' in sys.argv, sys.argv\n"
        "assert 'not-a-real-secret' not in sys.argv, sys.argv\n"
        "value = sys.stdin.read()\n"
        "assert value == 'source-secret-value', value\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)
    plan = github_plan("saved-write", status="approved", action="github.env_secret.write")
    plan["secret_name"] = "SOURCE_SECRET"
    plan["env"] = "staging"
    plan["target"]["target_environment"] = "staging"
    plan["target"]["target_secret_name"] = "TARGET_SECRET"
    write_plan(plan_dir, plan)

    result = run_secretops(
        "execute-plan",
        "--id",
        "saved-write",
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0
    assert "OK GitHub write acknowledged: TARGET_SECRET" in output
    assert "source-secret-value" not in output


# Infisical adapter hardening (docs/design/infisical-adapter-hardening.md)


def write_vault_infisical(tmp_path, vault_secrets, *, run_exit=None):
    """Fake CLI holding its own secrets, like a real vault. `run` gives the
    child the CLI's environment plus these secrets; `secrets set` only
    leaves a marker."""
    fake_infisical = tmp_path / "infisical"
    run_marker = tmp_path / "infisical-run-called"
    set_marker = tmp_path / "infisical-set-called"
    fake_infisical.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "if 'secrets' in sys.argv and 'set' in sys.argv:\n"
        f"    Path({str(set_marker)!r}).write_text('called', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        f"Path({str(run_marker)!r}).write_text('called', encoding='utf-8')\n"
        f"run_exit = {run_exit!r}\n"
        "if run_exit is not None:\n"
        "    raise SystemExit(run_exit)\n"
        "command = sys.argv[sys.argv.index('--') + 1:]\n"
        "child_env = os.environ.copy()\n"
        f"child_env.update({dict(vault_secrets)!r})\n"
        "raise SystemExit(subprocess.run(command, env=child_env).returncode)\n",
        encoding="utf-8",
    )
    fake_infisical.chmod(0o700)
    return tmp_path


def test_broker_variable_never_reaches_github_in_sync_many(tmp_path):
    # run_secretops puts DUMMY_SECRET in the broker environment; the vault is empty.
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    fake_bin = write_vault_infisical(tmp_path, {})
    write_marker_gh(tmp_path)
    plan_path = write_plan(plan_dir, sync_many_plan("broker-var-sync", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "broker-var-sync",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode != 0
    assert not (tmp_path / "gh-called").exists()
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_broker_variable_never_reaches_github_in_write(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    fake_bin = write_vault_infisical(tmp_path, {})
    write_marker_gh(tmp_path)
    plan_path = write_plan(
        plan_dir,
        github_plan("broker-var-write", status="approved", action="github.env_secret.write"),
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        "broker-var-write",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode != 0
    assert not (tmp_path / "gh-called").exists()
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_missing_token_stops_preflight_before_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("INFISICAL_TOKEN")
    plan_dir = tmp_path / "plans"
    fake_bin = write_failing_external_commands(tmp_path)
    plan_path = write_plan(plan_dir, vault_plan("no-token-preflight"))

    result = run_secretops(
        "preflight-plan",
        "--id",
        "no-token-preflight",
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode != 0
    assert "INFISICAL_TOKEN is not set" in result.stdout
    assert not (tmp_path / "infisical-called").exists()
    assert read_plan(plan_path)["status"] == "planned"


def test_missing_token_stops_execution_before_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("INFISICAL_TOKEN")
    plan_dir = tmp_path / "plans"
    fake_bin = write_failing_external_commands(tmp_path)
    plan_path = write_plan(plan_dir, generate_plan("no-token-generate", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "no-token-generate",
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode != 0
    assert "INFISICAL_TOKEN is not set" in result.stdout
    assert not (tmp_path / "infisical-called").exists()
    assert read_plan(plan_path)["status"] == "failed"


def test_provider_failure_stops_generate_before_write(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    fake_bin = write_vault_infisical(tmp_path, {}, run_exit=1)
    plan_path = write_plan(plan_dir, generate_plan("provider-failure", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "provider-failure",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode != 0
    assert "vault error" in result.stdout
    assert "provider output was not printed" in result.stdout
    assert (tmp_path / "infisical-run-called").exists()
    assert not (tmp_path / "infisical-set-called").exists()
    assert read_plan(plan_path)["status"] == "failed"
    assert '"status":"failure"' in audit_path.read_text(encoding="utf-8")


def test_provider_failure_in_preflight_does_not_change_plan_status(tmp_path):
    plan_dir = tmp_path / "plans"
    fake_bin = write_vault_infisical(tmp_path, {}, run_exit=1)
    plan_path = write_plan(plan_dir, generate_plan("provider-failure-preflight"))

    result = run_secretops(
        "preflight-plan",
        "--id",
        "provider-failure-preflight",
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode != 0
    assert "vault error" in result.stdout
    assert "generation can proceed" not in result.stdout
    assert read_plan(plan_path)["status"] == "planned"


def test_empty_existing_secret_blocks_create_only_generate(tmp_path):
    plan_dir = tmp_path / "plans"
    fake_bin = write_vault_infisical(tmp_path, {"GENERATED_DUMMY_PASSWORD": ""})
    plan_path = write_plan(plan_dir, generate_plan("empty-existing", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "empty-existing",
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode != 0
    assert "generated secret already exists: GENERATED_DUMMY_PASSWORD" in result.stdout
    assert not (tmp_path / "infisical-set-called").exists()
    assert read_plan(plan_path)["status"] == "failed"


def test_saved_plan_with_reserved_name_is_rejected_before_provider_calls(tmp_path):
    plan_dir = tmp_path / "plans"
    fake_bin = write_failing_external_commands(tmp_path)
    plan = vault_plan("reserved-name", status="approved")
    plan["secret_name"] = "PATH"
    write_plan(plan_dir, plan)

    for command in ("preflight-plan", "execute-plan"):
        result = run_secretops(command, "--id", "reserved-name", plan_dir=plan_dir, path_prefix=fake_bin)

        assert result.returncode != 0
        assert "plan file does not match the plan schema" in (result.stdout + result.stderr)
    assert not (tmp_path / "infisical-called").exists()


def test_planning_and_github_only_preflight_work_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("INFISICAL_TOKEN")
    plan_dir = tmp_path / "plans"
    fake_bin = write_failing_external_commands(tmp_path)

    planned = run_secretops("plan", "--file", VAULT_RECIPE, "--save", plan_dir=plan_dir, path_prefix=fake_bin)
    assert planned.returncode == 0

    write_plan(plan_dir, verify_many_plan("verify-no-token"))
    preflight = run_secretops(
        "preflight-plan", "--id", "verify-no-token", plan_dir=plan_dir, path_prefix=fake_bin
    )

    assert preflight.returncode == 0
    assert "OK preflight passed: github.env_secret.verify_many" in preflight.stdout
    assert not (tmp_path / "infisical-called").exists()
    assert not (tmp_path / "gh-called").exists()


def test_import_stops_after_unknown_write_and_reports_partial_result(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "imported.env"
    import_values = {
        "BACKUP_AWS_ACCESS_KEY_ID": "import-access-id",
        "BACKUP_AWS_SECRET_ACCESS_KEY": "import-secret-key",
        "BACKUP_AWS_REGION": "eu-central-1",
    }
    source_file.write_text(
        "".join(f"{name}={value}\n" for name, value in import_values.items()),
        encoding="utf-8",
    )
    source_file.chmod(0o600)
    plan_path = write_plan(
        plan_dir,
        import_file_plan(
            "partial-import",
            status="approved",
            source_file=str(source_file),
            secret_mappings=[{"name": name} for name in import_values],
        ),
    )
    record_path = write_fake_infisical_for_import_file(
        tmp_path, fail_set_name="BACKUP_AWS_SECRET_ACCESS_KEY"
    )

    result = run_secretops(
        "execute-plan",
        "--id",
        "partial-import",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL imported secret write outcome unknown: BACKUP_AWS_SECRET_ACCESS_KEY" in output
    assert "written and confirmed: BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "not written: BACKUP_AWS_REGION" in output
    assert "no automatic rollback or retry was attempted" in output
    assert [item["name"] for item in record["sets"]] == [
        "BACKUP_AWS_ACCESS_KEY_ID",
        "BACKUP_AWS_SECRET_ACCESS_KEY",
    ]
    assert read_plan(plan_path)["status"] == "failed"
    stop_event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert stop_event["status"] == "outcome_unknown"
    assert stop_event["secret_name"] == "BACKUP_AWS_SECRET_ACCESS_KEY"
    assert stop_event["env"] == "staging"
    assert stop_event["confirmed_names"] == ["BACKUP_AWS_ACCESS_KEY_ID"]
    assert stop_event["not_written_names"] == ["BACKUP_AWS_REGION"]
    for value in import_values.values():
        assert value not in output
        assert value not in audit_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_rotate_with_cli_failure_after_start_is_outcome_unknown(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    fake_bin = write_vault_infisical(tmp_path, {"GENERATED_DUMMY_PASSWORD": "existing-placeholder"})
    fake = fake_bin / "infisical"
    fake.write_text(
        fake.read_text(encoding="utf-8").replace("    raise SystemExit(0)\n", "    raise SystemExit(1)\n", 1),
        encoding="utf-8",
    )
    plan_path = write_plan(plan_dir, rotate_plan("rotate-unknown", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "rotate-unknown",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "FAIL rotated secret write outcome unknown: GENERATED_DUMMY_PASSWORD" in output
    assert "was not stored" not in output
    assert (tmp_path / "infisical-set-called").exists()
    assert read_plan(plan_path)["status"] == "failed"
    assert '"status":"outcome_unknown"' in audit_path.read_text(encoding="utf-8")
    assert_no_secret_leak(result, plan_path, audit_path)


def test_source_read_error_stops_sync_before_github(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    fake_bin = write_vault_infisical(tmp_path, {}, run_exit=1)
    write_marker_gh(tmp_path)
    plan_path = write_plan(plan_dir, sync_many_plan("sync-read-error", status="approved"))

    result = run_secretops(
        "execute-plan",
        "--id",
        "sync-read-error",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=fake_bin,
    )

    assert result.returncode != 0
    assert "vault error: infisical CLI failed with exit status 1" in result.stdout
    assert not (tmp_path / "gh-called").exists()
    assert read_plan(plan_path)["status"] == "failed"
    assert_no_secret_leak(result, plan_path, audit_path)


def test_import_stops_before_empty_value_and_reports_partial_result(tmp_path):
    # The Infisical CLI cannot store empty values, so the write is refused
    # before the CLI starts: a failure, not an unknown outcome.
    audit_path = tmp_path / "audit.jsonl"
    plan_dir = tmp_path / "plans"
    source_file = tmp_path / "imported.env"
    source_file.write_text(
        "BACKUP_AWS_ACCESS_KEY_ID=import-access-id\n"
        "BACKUP_AWS_SECRET_ACCESS_KEY=\n"
        "BACKUP_AWS_REGION=eu-central-1\n",
        encoding="utf-8",
    )
    source_file.chmod(0o600)
    names = ["BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY", "BACKUP_AWS_REGION"]
    plan_path = write_plan(
        plan_dir,
        import_file_plan(
            "empty-value-import",
            status="approved",
            source_file=str(source_file),
            secret_mappings=[{"name": name} for name in names],
        ),
    )
    record_path = write_fake_infisical_for_import_file(tmp_path)

    result = run_secretops(
        "execute-plan",
        "--id",
        "empty-value-import",
        audit_path=audit_path,
        plan_dir=plan_dir,
        path_prefix=tmp_path,
    )

    record = json.loads(record_path.read_text(encoding="utf-8"))
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "cannot store an empty or whitespace-only value" in output
    assert "written and confirmed: BACKUP_AWS_ACCESS_KEY_ID" in output
    assert "outcome unknown" not in output
    assert [item["name"] for item in record["sets"]] == ["BACKUP_AWS_ACCESS_KEY_ID"]
    assert read_plan(plan_path)["status"] == "failed"
    stop_event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert stop_event["status"] == "failure"
    assert stop_event["secret_name"] == "BACKUP_AWS_SECRET_ACCESS_KEY"
    assert stop_event["confirmed_names"] == ["BACKUP_AWS_ACCESS_KEY_ID"]
    assert stop_event["not_written_names"] == [
        "BACKUP_AWS_SECRET_ACCESS_KEY",
        "BACKUP_AWS_REGION",
    ]
    assert "import-access-id" not in output
    assert "import-access-id" not in audit_path.read_text(encoding="utf-8")
