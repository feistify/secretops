import sys

import typer
from rich.console import Console

from secretops.add_client import (
    EXIT_NOT_WRITTEN,
    ClientError,
    load_broker_access,
    run_add,
)
from secretops.audit import write_audit_event
from secretops.handlers import (
    execute_recipe_action,
    execute_vault_read_check_recipe,
    fail_unsupported_recipe_action,
    plan_github_env_secret_recipe,
    plan_recipe_action,
    plan_vault_read_recipe,
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
    preflight_saved_plan,
    show_saved_plan,
    transition_planned_plan,
)
from secretops.plans import (
    PLAN_ID_PATTERN,
    VAULT_ACTIONS,
    build_plan_artifact,
    save_plan_artifact,
)
from secretops.policy import load_policy, require_allowed
from secretops.recipes import load_recipe
from secretops.validation import validate_secret_name
from secretops.vaults import VaultError, configured_vault_destination, get_vault

app = typer.Typer(
    help="Safe secret operations for coding agents: agents plan, humans enter values and approve.",
)
console = Console()


def _load_recipe(file: str):
    # One line an agent can act on, instead of a traceback. The message names
    # fields only, never what was written in them.
    try:
        return load_recipe(file)
    except ValueError as error:
        console.print(f"[red]FAIL[/red] invalid recipe: {error}", soft_wrap=True)
        console.print("no plan was saved")
        raise typer.Exit(1) from error


@app.command(help="Show the secretops version.", rich_help_panel="Information")
def version():
    console.print("secretops 0.1.3")


@app.command(
    "actions",
    help="List the actions the current policy allows and denies.",
    rich_help_panel="Information",
)
def actions():
    policy = load_policy()

    console.print("Allowed actions")
    for action in policy.allowed_actions:
        console.print(f"- {action}")

    console.print("Denied actions")
    for action in policy.denied_actions:
        console.print(f"- {action}")


@app.command(
    "plan",
    help="Show the plan for a recipe; with --save, store it and print its plan_id.",
    rich_help_panel="Plans",
)
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
            console.print(f"[red]FAIL[/red] vault destination is not configured: {error}")
            console.print("no plan was saved")
            raise typer.Exit(1) from error
        try:
            artifact = build_plan_artifact(recipe, destination)
        except (ImportFileError, ValueError) as error:
            if recipe.action == "vault.secret.import_file":
                console.print("[red]FAIL[/red] import file plan is unsafe")
                console.print(str(error))
                console.print("secret values were not printed")
                raise typer.Exit(1) from error
            console.print(f"[red]FAIL[/red] plan is invalid: {error}")
            console.print("no plan was saved")
            raise typer.Exit(1) from error
    plan_recipe_action(recipe, console)
    if save:
        save_plan_artifact(artifact)
        console.print(f"- plan_id: {artifact.plan_id}")


@app.command("show-plan", help="Show a saved plan and its status.", rich_help_panel="Plans")
def show_plan(plan_id: str = typer.Option(..., "--id")):
    show_saved_plan(plan_id, console)


@app.command("approve-plan", help="Approve a saved plan (human only).", rich_help_panel="Plans")
def approve_plan(plan_id: str = typer.Option(..., "--id")):
    transition_planned_plan(plan_id, "approved", "approved", console)


@app.command("reject-plan", help="Reject a saved plan (human only).", rich_help_panel="Plans")
def reject_plan(plan_id: str = typer.Option(..., "--id")):
    transition_planned_plan(plan_id, "rejected", "rejected", console)


@app.command(
    "preflight-plan",
    help="Check a saved plan's prerequisites without writing anything.",
    rich_help_panel="Plans",
)
def preflight_plan(plan_id: str = typer.Option(..., "--id")):
    preflight_saved_plan(plan_id, console)


@app.command(
    "execute-plan",
    help="Run an approved plan. Exit 0 done, 1 failed, 3 outcome unknown.",
    rich_help_panel="Plans",
)
def execute_plan(plan_id: str = typer.Option(..., "--id")):
    execute_saved_plan(plan_id, console)


@app.command(
    "plan-vault-read",
    help="Show the plan for a vault name check; nothing is saved.",
    rich_help_panel="Checks",
)
def plan_vault_read(
    name: str = typer.Option(..., "--name"),
    env: str = typer.Option("dev", "--env"),
):
    try:
        name = validate_secret_name(name)
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error

    require_allowed("vault.read.plan")

    console.print("[bold]Plan[/bold]")
    console.print(f"- read secret: {name}")
    console.print(f"- environment: {env}")
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
def check_vault_read(
    name: str = typer.Option(..., "--name"),
    env: str = typer.Option("dev", "--env"),
):
    try:
        name = validate_secret_name(name)
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error

    require_allowed("vault.read.check")

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
        console.print(f"[red]FAIL[/red] vault error: {error}")
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
        console.print(f"[green]OK[/green] secret exists: {name}")
        console.print("secret value was not printed")
    else:
        write_audit_event(
            action="check-vault-read",
            secret_name=name,
            env=env,
            status="fail",
        )
        console.print(f"[red]FAIL[/red] secret missing or inaccessible: {name}")
        raise typer.Exit(1)

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
def plan_recipe(file: str = typer.Option(..., "--file")):
    recipe = _load_recipe(file)
    require_recipe_allowed(recipe, console)
    plan_vault_read_recipe(recipe, console)

@app.command(
    "plan-github-env-secret",
    help="Show the plan for a GitHub recipe without saving it.",
    rich_help_panel="Single-recipe commands (development)",
)
def plan_github_env_secret(file: str = typer.Option(..., "--file")):
    recipe = _load_recipe(file)
    require_recipe_allowed(recipe, console)

    if recipe.action not in {
        "github.env_secret.plan",
        "github.env_secret.write",
        "github.env_secret.sync_many",
        "github.env_secret.verify_many",
    }:
        console.print(f"[red]unsupported recipe action: {recipe.action}[/red]")
        raise typer.Exit(1)

    plan_github_env_secret_recipe(recipe, console)

@app.command(
    "execute",
    help="Run a vault.read.check recipe directly, without a saved plan.",
    rich_help_panel="Single-recipe commands (development)",
)
def execute(file: str = typer.Option(..., "--file")):
    recipe = _load_recipe(file)
    execute_recipe_action(recipe, console)

@app.command(
    "execute-recipe",
    help="Run a vault.read.check recipe directly, without a saved plan.",
    rich_help_panel="Single-recipe commands (development)",
)
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


