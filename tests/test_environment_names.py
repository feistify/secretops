"""Vault environment names have one allowed form in recipes, saved plans,
the policy and command options, and plan output prints values literally."""

import io
import json
import re
import subprocess
import sys

import pytest
import yaml
from rich.console import Console
from test_plans import (
    copy_many_plan,
    diff_envs_plan,
    generate_plan,
    import_file_plan,
    sync_many_plan,
    verify_many_plan,
    write_fake_infisical,
    write_plan,
)
from typer.testing import CliRunner

from secretops import plan_lifecycle
from secretops.cli import app
from secretops.policy import PolicyError, load_policy
from secretops.recipes import RecipeError, load_recipe
from secretops.validation import (
    GITHUB_ENVIRONMENT_PATTERN,
    GITHUB_HOST_PATTERN,
    GITHUB_OWNER_PATTERN,
    GITHUB_REPO_PATTERN,
    SECRET_NAME_PATTERN,
    VAULT_ENVIRONMENT_PATTERN,
    is_github_environment_name,
    is_vault_environment_name,
    shown,
    validate_github_host,
    validate_github_target_metadata,
    validate_secret_name,
)

MARK = "FORGED"
# Values outside the allowed form. Strings carry MARK so the output can be
# searched for any part of them.
INVALID_TEXT = {
    "newline": f"dev\nstatus: approved {MARK}",
    "escape": f"dev\x1b[2J{MARK}",
    "markup": f"[red]{MARK}[/red]",
    "leading_space": f" {MARK}",
    "trailing_space": f"{MARK} ",
    "trailing_newline": f"{MARK}\n",
    "too_long": MARK + "x" * 59,
    "non_ascii": f"dév{MARK}",
    "leading_dot": f".{MARK}",
}
INVALID_VALUES = {
    **INVALID_TEXT,
    "empty": "",
    "number": 2048,
    "list": [MARK],
    "mapping": {MARK: MARK},
    "null": None,
}
VALID_NAMES = ["dev", "staging", "prod", "Dev2", "eu-west.1", "a_b", "9", "x" * 64]

GITHUB = {
    "github_owner": "example-owner",
    "github_repo": "example-app",
    "github_environment": "dev",
}
RECIPES = {
    "vault.read.check": {"secret_name": "DUMMY_SECRET", "env": "dev"},
    "vault.secret.add": {"secret_name": "NEW_KEY", "env": "dev"},
    "vault.secret.generate": {
        "secret_name": "NEW_KEY", "env": "dev", "generator": {"type": "password", "length": 32},
    },
    "vault.secret.rotate": {
        "secret_name": "NEW_KEY", "env": "dev", "generator": {"type": "password", "length": 32},
    },
    "vault.secret.import_file": {"env": "dev", "source_file": "/nonexistent/import.env"},
    "vault.secret.copy_many": {
        "env": "dev", "source_env": "dev", "target_env": "staging",
        "secrets": [{"source": "A_NAME", "target": "A_NAME"}],
    },
    "vault.secret.diff_envs": {
        "env": "dev", "source_env": "dev", "target_env": "staging", "secrets": [{"name": "A_NAME"}],
    },
    "github.env_secret.plan": {
        "secret_name": "DUMMY_SECRET", "github_secret_name": "DUMMY_SECRET", "env": "dev", **GITHUB,
    },
    "github.env_secret.write": {
        "secret_name": "DUMMY_SECRET", "github_secret_name": "DUMMY_SECRET", "env": "dev", **GITHUB,
    },
    "github.env_secret.sync_many": {
        "env": "dev", "secrets": [{"source": "A_NAME", "target": "A_NAME"}], **GITHUB,
    },
    "github.env_secret.verify_many": {"env": "dev", "secrets": [{"name": "A_NAME"}], **GITHUB},
    # Not a known action: the environment is still checked.
    "vault.not_an_action": {"env": "dev"},
}
CASES = [
    (action, field)
    for action, recipe in RECIPES.items()
    for field in ("env", "source_env", "target_env")
    if field in recipe
]


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    plan_dir = tmp_path / "plans"
    monkeypatch.setenv("SECRETOPS_PLAN_DIR", str(plan_dir))
    monkeypatch.setenv("SECRETOPS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SECRETOPS_INFISICAL_BIN", "/nonexistent/infisical")
    monkeypatch.setenv("SECRETOPS_GH_BIN", "/nonexistent/gh")
    return plan_dir


def invoke(*args, stdin=None):
    return CliRunner().invoke(app, list(args), input=stdin)


def run_endpoint(*args, stdin=""):
    # The human endpoints read stdin by file descriptor, so they run as a process.
    return subprocess.run(
        [sys.executable, "-m", "secretops.cli", *args],
        input=stdin, capture_output=True, text=True, check=False,
    )


def write_recipe(tmp_path, data):
    path = tmp_path / "recipe.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def assert_value_absent(output, value):
    assert MARK not in output
    assert "\x1b" not in output
    assert "Traceback" not in output
    if isinstance(value, str) and value.strip():
        assert value not in output


# Patterns


def test_vault_environment_pattern_is_shared_with_configure():
    from secretops import broker_config, human_entry

    assert broker_config.ENV_NAME is VAULT_ENVIRONMENT_PATTERN
    assert human_entry.ENV_PATTERN is VAULT_ENVIRONMENT_PATTERN


@pytest.mark.parametrize("name", VALID_NAMES)
def test_valid_vault_environment_names(name):
    assert is_vault_environment_name(name)


@pytest.mark.parametrize("kind", sorted(INVALID_VALUES))
def test_invalid_vault_environment_names(kind):
    assert not is_vault_environment_name(INVALID_VALUES[kind])


@pytest.mark.parametrize(
    "pattern, valid",
    [
        (SECRET_NAME_PATTERN, "DUMMY_SECRET"),
        (GITHUB_OWNER_PATTERN, "example-owner"),
        (GITHUB_REPO_PATTERN, "example-app"),
        (GITHUB_ENVIRONMENT_PATTERN, "Production env"),
        (GITHUB_HOST_PATTERN, "github.example.com"),
        (VAULT_ENVIRONMENT_PATTERN, "dev"),
    ],
)
def test_patterns_refuse_a_trailing_newline(pattern, valid):
    # `$` in some patterns also matches before a final newline; fullmatch
    # does not.
    assert pattern.fullmatch(valid)
    assert pattern.fullmatch(valid + "\n") is None
    if pattern.pattern.endswith("$"):
        # Why only fullmatch may apply these patterns.
        assert pattern.match(valid + "\n")


@pytest.mark.parametrize(
    "call",
    [
        lambda suffix: validate_secret_name("DUMMY_SECRET" + suffix),
        lambda suffix: validate_github_host("github.com" + suffix),
        lambda suffix: validate_github_target_metadata(
            owner="example-owner" + suffix, repo="example-app", environment="dev"
        ),
        lambda suffix: validate_github_target_metadata(
            owner="example-owner", repo="example-app" + suffix, environment="dev"
        ),
        lambda suffix: validate_github_target_metadata(
            owner="example-owner", repo="example-app", environment="Production env" + suffix
        ),
    ],
)
def test_validated_fields_refuse_a_trailing_newline(call):
    call("")
    for suffix in ("\n", "\r\n", "\x1b", "\t"):
        with pytest.raises(ValueError):
            call(suffix)


def test_github_environment_names_allow_spaces_but_no_control_characters():
    assert is_github_environment_name("Production env")
    for character in ("\n", "\r", "\t", "\x1b", "\x00", " ", " "):
        assert not is_github_environment_name(f"Production{character}env")


# Recipes


@pytest.mark.parametrize("action, field", CASES)
@pytest.mark.parametrize("kind", sorted(INVALID_VALUES))
def test_recipe_environment_fields_are_refused_at_planning(tmp_path, cli_env, action, field, kind):
    value = INVALID_VALUES[kind]
    recipe = {"action": action, **RECIPES[action], field: value}
    path = write_recipe(tmp_path, recipe)

    with pytest.raises(RecipeError) as error:
        load_recipe(path)
    assert field in str(error.value)
    assert MARK not in str(error.value)

    result = invoke("plan", "--file", str(path), "--save")
    assert result.exit_code == 1
    assert "FAIL invalid recipe: " in result.output
    assert "no plan was saved" in result.output
    assert_value_absent(result.output, value)
    assert not cli_env.exists() or not list(cli_env.glob("*.json"))


@pytest.mark.parametrize("command", ["plan", "plan-recipe", "execute-recipe", "execute"])
def test_every_recipe_command_refuses_invalid_environment_names(tmp_path, cli_env, command):
    path = write_recipe(tmp_path, {"action": "vault.read.check", **RECIPES["vault.read.check"],
                                   "env": INVALID_TEXT["markup"]})

    result = invoke(command, "--file", str(path))

    assert result.exit_code == 1
    assert "FAIL invalid recipe: env " in result.output
    assert_value_absent(result.output, INVALID_TEXT["markup"])


@pytest.mark.parametrize("command", ["plan-vault-read", "check-vault-read"])
@pytest.mark.parametrize("kind", sorted(INVALID_TEXT))
def test_env_option_is_checked_like_a_recipe(cli_env, tmp_path, command, kind):
    result = invoke(command, "--name", "DUMMY_SECRET", "--env", INVALID_TEXT[kind])

    assert result.exit_code == 1
    assert "FAIL invalid recipe: env " in result.output
    assert_value_absent(result.output, INVALID_TEXT[kind])
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("action", [a for a in RECIPES if a.startswith("github.")])
def test_github_environment_with_a_space_needs_an_explicit_vault_env(tmp_path, action):
    recipe = {"action": action, **RECIPES[action], "github_environment": "Production env"}
    del recipe["env"]

    with pytest.raises(RecipeError, match="env is missing and github_environment"):
        load_recipe(write_recipe(tmp_path, recipe))

    recipe["env"] = "prod"
    loaded = load_recipe(write_recipe(tmp_path, recipe))
    assert (loaded.env, loaded.github_environment) == ("prod", "Production env")


@pytest.mark.parametrize("action", list(RECIPES))
def test_valid_environment_names_still_load(tmp_path, action):
    for name in VALID_NAMES:
        recipe = {"action": action, **RECIPES[action]}
        for field in ("env", "source_env", "target_env"):
            if field in recipe:
                recipe[field] = name
        assert load_recipe(write_recipe(tmp_path, recipe)).env == name


# Saved plans

PLANS = {
    "generate": generate_plan,
    "copy_many": copy_many_plan,
    "diff_envs": diff_envs_plan,
    "sync_many": sync_many_plan,
    "verify_many": verify_many_plan,
}
PLAN_CASES = [
    (kind, field)
    for kind, build in PLANS.items()
    for field in ("env", "source_env", "target_env")
    if field in build()
]
PLAN_COMMANDS = ["show-plan", "preflight-plan", "approve-plan", "reject-plan", "execute-plan"]


@pytest.mark.parametrize("plan_kind, field", PLAN_CASES)
@pytest.mark.parametrize("kind", sorted(INVALID_VALUES))
@pytest.mark.parametrize("status", ["planned", "approved"])
def test_saved_plan_with_invalid_environment_is_refused_by_every_command(
    cli_env, plan_kind, field, kind, status
):
    plan = PLANS[plan_kind]("bad-plan", status=status)
    plan[field] = INVALID_VALUES[kind]
    path = write_plan(cli_env, plan)
    original = path.read_bytes()

    for command in PLAN_COMMANDS:
        result = invoke(command, "--id", "bad-plan")
        assert result.exit_code == 1, (command, result.output)
        assert "FAIL plan file does not match the plan schema" in result.output
        assert_value_absent(result.output, INVALID_VALUES[kind])

    result = invoke("pending-plans")
    assert result.exit_code == 0
    assert "1 plan file could not be read" in result.output
    assert_value_absent(result.output, INVALID_VALUES[kind])
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", sorted(INVALID_VALUES))
def test_add_endpoints_refuse_a_plan_with_an_invalid_environment(cli_env, kind):
    plan = {
        "plan_id": "add-plan", "action": "vault.secret.add", "secret_name": "NEW_KEY",
        "env": INVALID_VALUES[kind], "status": "planned", "secret_value_included": False,
        "create_mode": "create_only", "value_source": "human_prompt",
    }
    write_plan(cli_env, plan)

    request = {"protocol": 1, "plan_id": "add-plan", "digest": "sha256-v1:" + "0" * 64,
               "value": "not-stored"}
    review = run_endpoint("review-add", "--id", "add-plan")
    submit = run_endpoint("submit-add", stdin=json.dumps(request) + "\n")

    for result in (review, submit):
        assert result.returncode == 1
        assert json.loads(result.stdout)["outcome"] == "refused"
        assert_value_absent(result.stdout + result.stderr, INVALID_VALUES[kind])


@pytest.mark.parametrize("kind", sorted(INVALID_TEXT))
def test_prepare_add_refuses_an_invalid_environment(cli_env, kind):
    result = invoke("prepare-add", "--name", "NEW_KEY", "--env", INVALID_TEXT[kind])

    assert result.exit_code == 1
    assert json.loads(result.output)["outcome"] == "refused"
    assert_value_absent(result.output, INVALID_TEXT[kind])
    assert not cli_env.exists() or not list(cli_env.glob("*.json"))


@pytest.mark.parametrize("field", ["env", "source_env", "target_env", "target_environment"])
def test_show_plan_refuses_an_older_plan_with_an_invalid_environment(cli_env, field):
    plan = copy_many_plan("old-plan")
    plan[field] = INVALID_TEXT["newline"]
    write_plan(cli_env, plan, legacy=True)

    result = invoke("show-plan", "--id", "old-plan")

    assert result.exit_code == 1
    assert "FAIL plan file does not match the plan schema" in result.output
    assert_value_absent(result.output, INVALID_TEXT["newline"])


# Output


def test_free_text_plan_fields_are_printed_literally(cli_env, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETOPS_INFISICAL_BIN", str(write_fake_infisical(tmp_path) / "infisical"))
    plan = generate_plan("markup-plan")
    plan["create_mode"] = "[red]MODE[/red]"
    plan["generator"]["type"] = "[bold]TYPE\x1b[2J[/bold]"
    write_plan(cli_env, plan)

    shown_output = invoke("show-plan", "--id", "markup-plan").output
    preflight_output = invoke("preflight-plan", "--id", "markup-plan").output

    assert "create_mode: [red]MODE[/red]\n" in shown_output
    assert "generator_type: [bold]TYPE?[2J[/bold]\n" in shown_output
    assert "generator type: [bold]TYPE?[2J[/bold]" in preflight_output
    assert "\x1b" not in shown_output + preflight_output


def test_free_text_plan_fields_get_no_style_on_a_terminal(cli_env):
    plan = import_file_plan("markup-plan")
    plan["source_file"] = "/tmp/[red]FILE[/red]"
    plan["create_mode"] = "[green]MODE[/green]"
    write_plan(cli_env, plan)
    buffer = io.StringIO()

    plan_lifecycle.show_saved_plan(
        "markup-plan", Console(file=buffer, force_terminal=True, color_system="standard", width=200)
    )

    output = buffer.getvalue()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    assert "source_file: /tmp/[red]FILE[/red]" in plain
    assert "create_mode: [green]MODE[/green]" in plain
    assert "\x1b[31m" not in output and "\x1b[32m" not in output


def test_older_plan_fields_are_printed_literally(cli_env):
    plan = generate_plan("old-plan")
    plan["action"] = "[red]ACTION[/red]"
    plan["status"] = "approved\x1b[2J"
    plan["create_mode"] = "[red]MODE[/red]"
    write_plan(cli_env, plan, legacy=True)

    result = invoke("show-plan", "--id", "old-plan")

    assert "action: [red]ACTION[/red]\n" in result.output
    assert "status: approved?[2J\n" in result.output
    assert "\x1b" not in result.output


def test_shown_keeps_valid_values_unchanged():
    for value in [*VALID_NAMES, "Production env", "DUMMY_SECRET", "sha256-v1:abc", "a -> b", 32]:
        assert shown(value) == str(value)
    assert shown("[red]x[/red]") == "\\[red]x\\[/red]"
    assert shown("a\nb\x1b") == "a?b?"


# Policy

POLICY_HEAD = "allowed_actions: [vault.read.check]\ndenied_actions: []\n"


def write_policy(tmp_path, text):
    path = tmp_path / "policy.yaml"
    path.write_text(POLICY_HEAD + text, encoding="utf-8")
    return path


def policy_lists(name):
    quoted = json.dumps(name)
    return {
        "vault allowed_environments": (
            f"actions:\n  vault.read.check:\n    allowed_environments: [dev, {quoted}]\n",
            "entry 2 of allowed_environments in actions entry 'vault.read.check'",
        ),
        "github allowed_environments": (
            f"actions:\n  github.env_secret.write:\n    allowed_environments: [dev, {quoted}]\n",
            "entry 2 of allowed_environments in actions entry 'github.env_secret.write'",
        ),
        "production_environments": (
            f"production_environments: [live, {quoted}]\n",
            "entry 2 of production_environments in the top level",
        ),
    }


@pytest.mark.parametrize("where", list(policy_lists("x")))
@pytest.mark.parametrize("kind", ["newline", "escape", "markup", "leading_space", "trailing_newline",
                                  "non_ascii", "leading_dot", "empty"])
def test_policy_with_an_invalid_environment_name_is_refused(tmp_path, where, kind):
    text, location = policy_lists(INVALID_VALUES[kind])[where]

    with pytest.raises(PolicyError) as error:
        load_policy(write_policy(tmp_path, text))

    message = str(error.value)
    assert location in message and "is not a valid environment name" in message
    assert MARK not in message


def test_policy_refusal_stops_every_command(tmp_path, monkeypatch):
    text, _ = policy_lists(INVALID_TEXT["markup"])["vault allowed_environments"]
    monkeypatch.setenv("SECRETOPS_POLICY_PATH", str(write_policy(tmp_path, text)))

    result = invoke("actions")

    assert result.exit_code == 1
    assert MARK not in result.output


def test_vault_lists_refuse_long_names_and_spaces(tmp_path):
    for name in ["x" * 65, "Production env"]:
        text, _ = policy_lists(name)["vault allowed_environments"]
        with pytest.raises(PolicyError):
            load_policy(write_policy(tmp_path, text))


def test_lists_that_name_github_environments_keep_spaces(tmp_path):
    text = (
        'production_environments: ["Production env"]\n'
        "actions:\n  github.env_secret.verify_many:\n"
        '    allowed_environments: [dev, "Staging env"]\n'
        "  vault.read.check:\n    allowed_environments: [dev, staging, x" + "y" * 63 + "]\n"
    )

    policy = load_policy(write_policy(tmp_path, text))

    assert "Production env" in policy.production_environments
    assert policy.actions["github.env_secret.verify_many"].allowed_environments == ["dev", "Staging env"]


def test_default_policy_still_loads():
    policy = load_policy()
    for action in policy.actions.values():
        assert all(is_vault_environment_name(name) for name in action.allowed_environments)


@pytest.mark.parametrize("kind", ["markup", "leading_dot", "trailing_space", "too_long"])
def test_add_client_refuses_an_invalid_environment_before_contacting_the_broker(tmp_path, kind):
    from test_add_client import run_cli_add
    from test_secret_add import Broker

    from secretops.add_client import EXIT_NOT_WRITTEN

    broker = Broker(tmp_path)
    result = run_cli_add(broker, tmp_path, "NEW_KEY", "--env", INVALID_TEXT[kind])

    assert result.returncode == EXIT_NOT_WRITTEN
    assert "Not saved: environment name is not valid. Nothing was sent." in result.stdout
    assert_value_absent(result.stdout + result.stderr, INVALID_TEXT[kind])
    assert not broker.plan_dir.exists() or not list(broker.plan_dir.glob("*.json"))


@pytest.mark.parametrize(
    "name",
    ["dev", "Production EU", "a b c", "Prod.1-x_y", "a" * 255],
)
def test_github_environment_names_accepted(name):
    assert GITHUB_ENVIRONMENT_PATTERN.fullmatch(name)


@pytest.mark.parametrize(
    "name",
    ["dev ", "dev  x", " dev", "dev" + " " * 300 + "approved", "a" * 256, "dev\n", "dev\tx", ""],
)
def test_github_environment_names_refused(name):
    assert GITHUB_ENVIRONMENT_PATTERN.fullmatch(name) is None


def test_emoji_codes_in_values_stay_literal():
    from secretops.cli import console

    with console.capture() as capture:
        console.print(shown("a:warning:b"))
    assert capture.get() == "a:warning:b\n"
