import functools
import os
import sys
from importlib.metadata import version as package_version

import typer
from rich.console import Console

from secretops import notify
from secretops.add_client import (
    EXIT_NOT_WRITTEN,
    ClientError,
    load_broker_access,
    run_add,
)
from secretops.audit import AuditWriteError, write_audit_event
from secretops.handlers import (
    execute_recipe_action,
    execute_vault_read_check_recipe,
    fail_unsupported_recipe_action,
    plan_github_env_secret_recipe,
    plan_recipe_action,
    plan_vault_read_recipe,
    require_read_check_environment,
    require_recipe_allowed,
)
from secretops.human_entry import (
    ENV_PATTERN,
    PROTOCOL_VERSION,
    RequestError,
    prepare_add,
    read_request,
    response_line,
    review_add,
    submit_add,
)
from secretops.import_file import ImportFileError
from secretops.plan_lifecycle import (
    execute_saved_plan,
    notify_human,
    pending_plan_lines,
    preflight_saved_plan,
    show_saved_plan,
    transition_planned_plan,
)
from secretops.plans import (
    PLAN_ID_PATTERN,
    VAULT_ACTIONS,
    PlanDirectoryError,
    build_plan_artifact,
    load_planned_plans,
    save_plan_artifact,
    utc_now,
)
from secretops.policy import (
    PolicyError,
    load_policy,
    require_allowed,
)
from secretops.recipes import check_vault_environment, load_recipe
from secretops.validation import shown, validate_secret_name
from secretops.vault_check import (
    CHECK_SECRET_NAME,
    CheckResult,
    check_vault_connection,
    result_lines,
)
from secretops.vaults import VaultError, configured_vault_destination, get_vault
from secretops.vaults.infisical_cli import CA_FILE_KEY

app = typer.Typer(
    help="Safe secret operations for coding agents: agents plan, humans enter values and approve.",
)
# emoji=False: ":name:" codes in printed values stay literal text.
console = Console(emoji=False)


@app.callback()
def _require_valid_policy() -> None:
    # Every command runs only with a policy file that passes strict
    # validation, so a misspelled key stops the broker instead of being
    # ignored. stderr keeps the JSON stdout of the human endpoints intact.
    try:
        load_policy()
    except PolicyError as error:
        typer.echo(f"FAIL {error}", err=True)
        raise typer.Exit(1) from error


def _load_recipe(file: str):
    # One line an agent can act on, instead of a traceback. The message names
    # fields only, never what was written in them.
    try:
        return load_recipe(file)
    except ValueError as error:
        _refuse_recipe(error)


def _refuse_recipe(error: ValueError) -> None:
    console.print(f"[red]FAIL[/red] invalid recipe: {shown(error)}", soft_wrap=True)
    console.print("no plan was saved")
    raise typer.Exit(1) from error


def _require_vault_environment(env: str) -> None:
    # --env takes the place of a recipe's env field and gets the same check.
    try:
        check_vault_environment("env", env)
    except ValueError as error:
        _refuse_recipe(error)


def _audit_failure_stops(command):
    """Stop with a fixed message when an audit event cannot be written.

    Only for commands whose audit events are all written before any vault or
    GitHub write and before any plan file change; events written after a
    provider write go through their own handling and never raise. The
    human-entry commands (add and its endpoints) are not wrapped: they write
    audit events after a vault write.
    """

    @functools.wraps(command)
    def run(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except AuditWriteError as error:
            console.print("[red]FAIL[/red] the audit record could not be written", soft_wrap=True)
            console.print(
                "the command stopped before any vault or GitHub write; nothing was changed",
                soft_wrap=True,
            )
            console.print("ask the operator to fix the audit log before running this again", soft_wrap=True)
            raise typer.Exit(1) from error

    return run


@app.command(help="Show the secretops version.", rich_help_panel="Information")
def version():
    console.print(f"secretops {shown(package_version('secretops'))}")


@app.command(
    "actions",
    help="List the actions the current policy allows and denies.",
    rich_help_panel="Information",
)
def actions():
    policy = load_policy()

    console.print("Allowed actions")
    for action in policy.allowed_actions:
        console.print(f"- {shown(action)}")

    console.print("Denied actions")
    for action in policy.denied_actions:
        console.print(f"- {shown(action)}")


@app.command(
    "plan",
    help="Show the plan for a recipe; with --save, store it and print its plan_id.",
    rich_help_panel="Plans",
)
@_audit_failure_stops
def plan(
    file: str = typer.Option(..., "--file"),
    save: bool = typer.Option(False, "--save"),
):
    recipe = _load_recipe(file)
    artifact = None
    if save:
        require_recipe_allowed(recipe, console)
        try:
            # Local, non-secret configuration only: no token, no provider call.
            destination = (
                configured_vault_destination() if recipe.action in VAULT_ACTIONS else None
            )
        except VaultError as error:
            console.print(f"[red]FAIL[/red] vault destination is not configured: {shown(error)}")
            console.print("no plan was saved")
            raise typer.Exit(1) from error
        try:
            artifact = build_plan_artifact(recipe, destination)
        except (ImportFileError, ValueError) as error:
            if recipe.action == "vault.secret.import_file":
                console.print("[red]FAIL[/red] import file plan is unsafe")
                console.print(shown(error))
                console.print("secret values were not printed")
                raise typer.Exit(1) from error
            console.print(f"[red]FAIL[/red] plan is invalid: {shown(error)}")
            console.print("no plan was saved")
            raise typer.Exit(1) from error
    plan_recipe_action(recipe, console)
    if save:
        save_plan_artifact(artifact)
        console.print(f"- plan_id: {shown(artifact.plan_id)}")
        notify_human(artifact, console)


@app.command(
    "check-notify",
    help="Send a test notification to the human (human only).",
    rich_help_panel="Information",
)
def check_notify():
    if not notify.is_configured(os.environ):
        typer.echo("FAIL notifications are not configured; set them with sudo secretops-configure --notify-ntfy URL")
        raise typer.Exit(1)
    outcome = notify.send(notify.TEST_MESSAGE)
    if outcome != notify.SENT:
        typer.echo(f"FAIL the test notification was not sent: {outcome}")
        raise typer.Exit(1)
    typer.echo("OK test notification sent; check that it arrived")


@app.command("show-plan", help="Show a saved plan and its status.", rich_help_panel="Plans")
@_audit_failure_stops
def show_plan(plan_id: str = typer.Option(..., "--id")):
    show_saved_plan(plan_id, console)


@app.command(
    "pending-plans",
    help="List the plans that wait for your approval or entry (human only).",
    rich_help_panel="Plans",
)
def pending_plans():
    # Read-only, like show-plan: no lock, no status change, no audit event.
    # Plain text without Rich: every plan field is agent-prepared text.
    try:
        plans, unreadable = load_planned_plans()
    except PlanDirectoryError as error:
        typer.echo("FAIL plan directory could not be read")
        raise typer.Exit(1) from error
    for line in pending_plan_lines(plans, unreadable, utc_now()):
        typer.echo(line)


@app.command("approve-plan", help="Approve a saved plan (human only).", rich_help_panel="Plans")
@_audit_failure_stops
def approve_plan(plan_id: str = typer.Option(..., "--id")):
    transition_planned_plan(plan_id, "approved", "approved", console)


@app.command("reject-plan", help="Reject a saved plan (human only).", rich_help_panel="Plans")
@_audit_failure_stops
def reject_plan(plan_id: str = typer.Option(..., "--id")):
    transition_planned_plan(plan_id, "rejected", "rejected", console)


@app.command(
    "preflight-plan",
    help="Check a saved plan's prerequisites without writing anything.",
    rich_help_panel="Plans",
)
@_audit_failure_stops
def preflight_plan(plan_id: str = typer.Option(..., "--id")):
    preflight_saved_plan(plan_id, console)


@app.command(
    "execute-plan",
    help="Run an approved plan. Exit 0 done, 1 failed, 3 outcome unknown.",
    rich_help_panel="Plans",
)
@_audit_failure_stops
def execute_plan(plan_id: str = typer.Option(..., "--id")):
    execute_saved_plan(plan_id, console)


@app.command(
    "plan-vault-read",
    help="Show the plan for a vault name check; nothing is saved.",
    rich_help_panel="Checks",
)
@_audit_failure_stops
def plan_vault_read(
    name: str = typer.Option(..., "--name"),
    env: str = typer.Option("dev", "--env"),
):
    try:
        name = validate_secret_name(name)
    except ValueError as error:
        console.print(f"[red]{shown(error)}[/red]")
        raise typer.Exit(1) from error
    _require_vault_environment(env)

    require_allowed("vault.read.plan")

    console.print("[bold]Plan[/bold]")
    console.print(f"- read secret: {shown(name)}")
    console.print(f"- environment: {shown(env)}")
    console.print("- secret value will NOT be printed")
    console.print("- execution required: no")
    write_audit_event(
        action="plan-vault-read",
        secret_name=name,
        env=env,
        status="planned",
    )


@app.command(
    "check-vault-read",
    help="Check that a secret name exists in the vault, without its value.",
    rich_help_panel="Checks",
)
@_audit_failure_stops
def check_vault_read(
    name: str = typer.Option(..., "--name"),
    env: str = typer.Option("dev", "--env"),
):
    try:
        name = validate_secret_name(name)
    except ValueError as error:
        console.print(f"[red]{shown(error)}[/red]")
        raise typer.Exit(1) from error
    _require_vault_environment(env)

    # The same action and environment policy as a vault.read.check plan.
    require_read_check_environment(
        console, audit_action="check-vault-read", secret_name=name, env=env, audit_status="fail"
    )

    vault = get_vault()

    try:
        exists = vault.secret_exists(name, env)
    except VaultError as error:
        write_audit_event(
            action="check-vault-read",
            secret_name=name,
            env=env,
            status="fail",
        )
        console.print(f"[red]FAIL[/red] vault error: {shown(error)}")
        console.print("provider output was not printed")
        console.print("secret value was not printed")
        raise typer.Exit(1) from error

    if exists:
        write_audit_event(
            action="check-vault-read",
            secret_name=name,
            env=env,
            status="ok",
        )
        console.print(f"[green]OK[/green] secret exists: {shown(name)}")
        console.print("secret value was not printed")
    else:
        write_audit_event(
            action="check-vault-read",
            secret_name=name,
            env=env,
            status="fail",
        )
        console.print(f"[red]FAIL[/red] secret missing or inaccessible: {shown(name)}")
        raise typer.Exit(1)


@app.command(
    "check-vault-connection",
    help="Check the saved vault configuration (human; usually through secretops-configure --check).",
    rich_help_panel="Checks",
)
@_audit_failure_stops
def check_vault_connection_command(env: str = typer.Option("dev", "--env")):
    if not ENV_PATTERN.fullmatch(env):
        typer.echo("environment name is not valid")
        raise typer.Exit(1)
    destination = None
    try:
        destination = configured_vault_destination()
    except VaultError:
        pass
    if destination is not None:
        typer.echo(
            f"Vault: {destination['domain']}, project {destination['project_id']}, "
            f"environment {env}, folder {destination['path']}"
        )
    result, detail = check_vault_connection(env)
    write_audit_event(
        action="check-vault-connection",
        secret_name=CHECK_SECRET_NAME,
        env=env,
        status="ok" if result is CheckResult.WORKING else "fail",
        details={"result": result.value},
    )
    for line in result_lines(result, detail, env, os.environ.get(CA_FILE_KEY) or None):
        typer.echo(line)
    typer.echo("Provider output and secret values were not printed.")
    raise typer.Exit(0 if result is CheckResult.WORKING else 1)

@app.command("add", rich_help_panel="Secrets")
def add(
    name: str = typer.Argument(None, help="Secret name; asked for when omitted"),
    env: str = typer.Option(None, "--env", help="Environment; asked for when omitted"),
    plan_id: str = typer.Option(None, "--plan", help="Complete a plan prepared by an agent"),
    broker: str = typer.Option(None, "--broker", help="Configured broker to use"),
):
    """Add one new secret: review the destination, enter the value hidden, confirm.

    Create only. In a vault without atomic create (Infisical), another writer
    can still create the same name between the final check and the write.
    """
    def echo(line: str) -> None:
        typer.echo(line)

    try:
        access = load_broker_access(broker)
        if plan_id is not None:
            if name is not None or env is not None:
                raise ClientError("--plan cannot be combined with a secret name or --env")
            if not PLAN_ID_PATTERN.fullmatch(plan_id):
                raise ClientError("plan id is not valid")
            review_args = ["review-add", "--id", plan_id]
        else:
            if name is None:
                name = typer.prompt("Secret name")
            try:
                name = validate_secret_name(name)
            except ValueError as error:
                raise ClientError(str(error)) from error
            if env is None:
                env = typer.prompt("Environment", default="dev")
            if not ENV_PATTERN.fullmatch(env):
                raise ClientError("environment name is not valid")
            review_args = ["prepare-add", "--name", name, "--env", env]
        code = run_add(echo, access=access, review_args=review_args)
    except ClientError as error:
        echo(f"Not saved: {error}. Nothing was sent.")
        raise typer.Exit(EXIT_NOT_WRITTEN) from error
    raise typer.Exit(code)


def _endpoint_exit(response: dict, success: str) -> None:
    sys.stdout.write(response_line(response))
    sys.stdout.flush()
    raise typer.Exit(0 if response["outcome"] == success else 1)


@app.command("prepare-add", hidden=True)
def prepare_add_endpoint(
    name: str = typer.Option(..., "--name"),
    env: str = typer.Option(..., "--env"),
):
    """Human broker endpoint: save a vault.secret.add plan and review it."""
    _endpoint_exit(prepare_add(name, env), "ready")


@app.command("review-add", hidden=True)
def review_add_endpoint(plan_id: str = typer.Option(..., "--id")):
    """Human broker endpoint: review a saved vault.secret.add plan."""
    _endpoint_exit(review_add(plan_id), "ready")


@app.command("submit-add", hidden=True)
def submit_add_endpoint():
    """Human broker endpoint: approve and execute with the value on stdin."""
    try:
        raw = read_request(sys.stdin.fileno())
    except RequestError as error:
        _endpoint_exit(
            {"protocol": PROTOCOL_VERSION, "outcome": "refused", "message": str(error), "plan_id": None},
            "created",
        )
    _endpoint_exit(submit_add(raw), "created")


@app.command(
    "plan-recipe",
    help="Show the plan for a vault.read.check recipe without saving it.",
    rich_help_panel="Single-recipe commands (development)",
)
@_audit_failure_stops
def plan_recipe(file: str = typer.Option(..., "--file")):
    recipe = _load_recipe(file)
    require_recipe_allowed(recipe, console)
    plan_vault_read_recipe(recipe, console)

@app.command(
    "plan-github-env-secret",
    help="Show the plan for a GitHub recipe without saving it.",
    rich_help_panel="Single-recipe commands (development)",
)
@_audit_failure_stops
def plan_github_env_secret(file: str = typer.Option(..., "--file")):
    recipe = _load_recipe(file)
    require_recipe_allowed(recipe, console)

    if recipe.action not in {
        "github.env_secret.plan",
        "github.env_secret.write",
        "github.env_secret.sync_many",
        "github.env_secret.verify_many",
    }:
        console.print(f"[red]unsupported recipe action: {shown(recipe.action)}[/red]")
        raise typer.Exit(1)

    plan_github_env_secret_recipe(recipe, console)

@app.command(
    "execute",
    help="Run a vault.read.check recipe directly, without a saved plan.",
    rich_help_panel="Single-recipe commands (development)",
)
@_audit_failure_stops
def execute(file: str = typer.Option(..., "--file")):
    recipe = _load_recipe(file)
    execute_recipe_action(recipe, console)

@app.command(
    "execute-recipe",
    help="Run a vault.read.check recipe directly, without a saved plan.",
    rich_help_panel="Single-recipe commands (development)",
)
@_audit_failure_stops
def execute_recipe(file: str = typer.Option(..., "--file")):
    recipe = _load_recipe(file)
    require_recipe_allowed(recipe, console)

    if recipe.action != "vault.read.check":
        fail_unsupported_recipe_action(recipe, console)

    execute_vault_read_check_recipe(recipe, console)


def main():
    app()

if __name__ == "__main__":
    main()


