import typer
from rich.console import Console

from secretops.audit import write_audit_event
from secretops.targets import GitHubEnvSecretTarget
from secretops.vaults import VaultError, get_vault


def _audit_secret_name(recipe) -> str:
    return recipe.secret_name or "multiple"


def require_recipe_allowed(recipe, console: Console) -> None:
    try:
        recipe.require_allowed()
    except PermissionError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1) from error


def plan_vault_read_recipe(recipe, console: Console) -> None:
    console.print("[bold]Recipe plan[/bold]")
    console.print(f"- action: {recipe.action}")
    console.print(f"- read secret: {recipe.secret_name}")
    console.print(f"- environment: {recipe.env}")
    console.print("- secret value will NOT be printed")
    console.print("- execution required: no")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.env,
        status="planned",
    )


def plan_github_env_secret_recipe(recipe, console: Console) -> None:
    if recipe.action == "github.env_secret.verify_many":
        console.print("[bold]GitHub environment secret verification dry-run plan[/bold]")
        console.print(f"- action: {recipe.action}")
        console.print(f"- target GitHub host: {recipe.github_host or 'github.com'}")
        console.print(f"- target GitHub owner: {recipe.github_owner}")
        console.print(f"- target GitHub repo: {recipe.github_repo}")
        console.print(f"- target GitHub environment: {recipe.github_environment}")
        for secret in recipe.secrets or []:
            console.print(f"- expected secret: {secret['name']}")
        console.print("- no GitHub API call was made")
        console.print("- secret value will NOT be printed")
        write_audit_event(
            action=recipe.action,
            secret_name=_audit_secret_name(recipe),
            env=recipe.github_environment or recipe.env,
            status="planned",
        )
        return

    if recipe.action == "github.env_secret.sync_many":
        console.print("[bold]GitHub environment secret multi-sync dry-run plan[/bold]")
        console.print(f"- action: {recipe.action}")
        console.print(f"- source environment: {recipe.env}")
        console.print(f"- target GitHub host: {recipe.github_host or 'github.com'}")
        console.print(f"- target GitHub owner: {recipe.github_owner}")
        console.print(f"- target GitHub repo: {recipe.github_repo}")
        console.print(f"- target GitHub environment: {recipe.github_environment}")
        for mapping in recipe.secrets or []:
            console.print(
                f"- secret mapping: {mapping['source']} -> {mapping['target']}"
            )
        console.print("- no GitHub API call was made")
        console.print("- secret value will NOT be printed")
        write_audit_event(
            action=recipe.action,
            secret_name=_audit_secret_name(recipe),
            env=recipe.env,
            status="planned",
        )
        return

    target = GitHubEnvSecretTarget(
        owner=recipe.github_owner,
        repo=recipe.github_repo,
        environment=recipe.github_environment,
        secret_name=recipe.github_secret_name,
        host=recipe.github_host or "github.com",
    )
    plan = target.describe_write_plan()

    console.print("[bold]GitHub environment secret dry-run plan[/bold]")
    console.print(f"- action: {recipe.action}")
    console.print(f"- source vault secret: {recipe.secret_name}")
    console.print(f"- source environment: {recipe.env}")
    console.print(f"- target GitHub host: {plan['target_host']}")
    console.print(f"- target GitHub owner: {plan['target_owner']}")
    console.print(f"- target GitHub repo: {plan['target_repo']}")
    console.print(f"- target GitHub environment: {plan['target_environment']}")
    console.print(f"- target GitHub secret: {plan['target_secret_name']}")
    if plan["api_call_made"] is False:
        console.print("- no GitHub API call was made")
    if plan["secret_value_printed"] is False:
        console.print("- secret value will NOT be printed")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.env,
        status="planned",
    )


def plan_vault_secret_generate_recipe(recipe, console: Console) -> None:
    console.print("[bold]Vault generated secret dry-run plan[/bold]")
    console.print(f"- action: {recipe.action}")
    console.print(f"- generated secret name: {recipe.secret_name}")
    console.print(f"- environment: {recipe.env}")
    generator = recipe.generator or {}
    if "type" in generator:
        console.print(f"- generator type: {generator['type']}")
    if "length" in generator:
        console.print(f"- generator length: {generator['length']}")
    console.print(f"- create mode: {recipe.create_mode or 'create_only'}")
    console.print("- secret value will NOT be generated during planning")
    console.print("- secret value will NOT be printed")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.env,
        status="planned",
    )


def plan_vault_secret_rotate_recipe(recipe, console: Console) -> None:
    console.print("[bold]Vault secret rotation dry-run plan[/bold]")
    console.print(f"- action: {recipe.action}")
    console.print(f"- target secret name: {recipe.secret_name}")
    console.print(f"- environment: {recipe.env}")
    generator = recipe.generator or {}
    if "type" in generator:
        console.print(f"- generator type: {generator['type']}")
    if "length" in generator:
        console.print(f"- generator length: {generator['length']}")
    console.print(f"- rotation mode: {recipe.rotation_mode or 'replace_existing'}")
    console.print("- secret value will NOT be generated during planning")
    console.print("- secret value will NOT be printed")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.env,
        status="planned",
    )


def plan_vault_secret_import_file_recipe(recipe, console: Console) -> None:
    console.print("[bold]Vault secret import-file dry-run plan[/bold]")
    console.print(f"- action: {recipe.action}")
    console.print(f"- target secret name: {recipe.secret_name}")
    console.print(f"- environment: {recipe.env}")
    console.print(f"- source file: {recipe.source_file}")
    console.print(f"- create mode: {recipe.create_mode or 'create_only'}")
    console.print("- saved plans parse dotenv names only, never values")
    console.print("- source file contents will NOT be printed")
    console.print("- secret value will NOT be printed")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.env,
        status="planned",
    )


def plan_vault_secret_copy_many_recipe(recipe, console: Console) -> None:
    console.print("[bold]Vault secret copy-many dry-run plan[/bold]")
    console.print(f"- action: {recipe.action}")
    console.print(f"- source_env: {recipe.source_env or recipe.env}")
    console.print(f"- target_env: {recipe.target_env}")
    console.print(f"- create_mode: {recipe.create_mode or 'create_only'}")
    for mapping in recipe.secrets or []:
        console.print(f"- secret mapping: {mapping['source']} -> {mapping['target']}")
    console.print("- source secret values will NOT be read during planning")
    console.print("- target secret values will NOT be written during planning")
    console.print("- secret values will NOT be printed")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.source_env or recipe.env,
        status="planned",
    )


def plan_vault_secret_diff_envs_recipe(recipe, console: Console) -> None:
    console.print("[bold]Vault secret env-diff dry-run plan[/bold]")
    console.print(f"- action: {recipe.action}")
    console.print(f"- source_env: {recipe.source_env or recipe.env}")
    console.print(f"- target_env: {recipe.target_env}")
    for secret in recipe.secrets or []:
        console.print(f"- expected secret: {secret['name']}")
    console.print("- source secret values will NOT be read during planning")
    console.print("- target secret values will NOT be read during planning")
    console.print("- secret values will NOT be printed")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.source_env or recipe.env,
        status="planned",
    )


def plan_vault_secret_add_recipe(recipe, console: Console) -> None:
    console.print("[bold]Vault secret add dry-run plan[/bold]")
    console.print(f"- action: {recipe.action}")
    console.print(f"- secret name: {recipe.secret_name}")
    console.print(f"- environment: {recipe.env}")
    console.print(f"- create mode: {recipe.create_mode or 'create_only'}")
    console.print("- value source: human_prompt")
    console.print("- a human enters the value with: secretops add --plan <plan_id>")
    console.print("- secret value will NOT be printed")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.env,
        status="planned",
    )


def execute_vault_read_check_recipe(recipe, console: Console) -> None:
    vault = get_vault()
    try:
        exists = vault.secret_exists(recipe.secret_name, recipe.env)
    except VaultError as error:
        write_audit_event(
            action=recipe.action,
            secret_name=_audit_secret_name(recipe),
            env=recipe.env,
            status="failure",
        )
        console.print(f"[red]FAIL[/red] vault error: {error}")
        console.print("provider output was not printed")
        console.print("secret value was not printed")
        raise typer.Exit(1) from error
    if exists:
        write_audit_event(
            action=recipe.action,
            secret_name=_audit_secret_name(recipe),
            env=recipe.env,
            status="success",
        )
        console.print(f"[green]OK[/green] secret exists: {recipe.secret_name}")
        console.print("secret value was not printed")
    else:
        write_audit_event(
            action=recipe.action,
            secret_name=_audit_secret_name(recipe),
            env=recipe.env,
            status="failure",
        )
        console.print(f"[red]FAIL[/red] secret missing or inaccessible: {recipe.secret_name}")
        raise typer.Exit(1)


def fail_github_env_secret_execute(recipe, console: Console) -> None:
    console.print("[red]github.env_secret.plan is planning only and is not executable[/red]")
    console.print("- no GitHub API call was made")
    console.print("- secret value will NOT be printed")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.env,
        status="failure",
    )
    raise typer.Exit(1)


def fail_unsupported_recipe_action(recipe, console: Console) -> None:
    console.print(f"[red]unsupported recipe action: {recipe.action}[/red]")
    write_audit_event(
        action=recipe.action,
        secret_name=_audit_secret_name(recipe),
        env=recipe.env,
        status="failure",
    )
    raise typer.Exit(1)


def plan_recipe_action(recipe, console: Console) -> None:
    require_recipe_allowed(recipe, console)

    if recipe.action == "vault.read.check":
        plan_vault_read_recipe(recipe, console)
    elif recipe.action == "vault.secret.generate":
        plan_vault_secret_generate_recipe(recipe, console)
    elif recipe.action == "vault.secret.rotate":
        plan_vault_secret_rotate_recipe(recipe, console)
    elif recipe.action == "vault.secret.import_file":
        plan_vault_secret_import_file_recipe(recipe, console)
    elif recipe.action == "vault.secret.copy_many":
        plan_vault_secret_copy_many_recipe(recipe, console)
    elif recipe.action == "vault.secret.diff_envs":
        plan_vault_secret_diff_envs_recipe(recipe, console)
    elif recipe.action == "vault.secret.add":
        plan_vault_secret_add_recipe(recipe, console)
    elif recipe.action in {
        "github.env_secret.plan",
        "github.env_secret.write",
        "github.env_secret.sync_many",
        "github.env_secret.verify_many",
    }:
        plan_github_env_secret_recipe(recipe, console)
    else:
        fail_unsupported_recipe_action(recipe, console)


def execute_recipe_action(recipe, console: Console) -> None:
    require_recipe_allowed(recipe, console)

    if recipe.action == "vault.read.check":
        execute_vault_read_check_recipe(recipe, console)
    elif recipe.action == "github.env_secret.plan":
        fail_github_env_secret_execute(recipe, console)
    else:
        fail_unsupported_recipe_action(recipe, console)


