from pathlib import Path

import typer
from rich.console import Console

from secretops.audit import write_audit_event
from secretops.generators import generate_secret_value
from secretops.import_file import (
    ImportFileError,
    parse_dotenv_file,
    parse_import_source_file,
    planned_import_names,
    source_file_permissions_restricted,
)
from secretops.plans import (
    DIGEST_PREFIX,
    HUMAN_ENTRY_ACTIONS,
    VAULT_ACTIONS,
    PlanBusyError,
    PlanFileError,
    load_legacy_plan_for_display,
    load_plan,
    plan_digest,
    plan_lock,
    set_plan_status,
)
from secretops.policy import require_environment_allowed, requires_approval
from secretops.targets import (
    GitHubCliEnvSecretTarget,
    GitHubConfigurationError,
    GitHubError,
    GitHubLaunchError,
    GitHubWriteOutcome,
)
from secretops.vaults import (
    Vault,
    VaultError,
    WriteOutcome,
    configured_vault_destination,
    get_vault,
)


# Public exit codes of execute-plan: 1 for a failure with known outcomes,
# 3 when any write outcome is unknown (stop and let a human investigate).
EXIT_FAILED = 1
EXIT_OUTCOME_UNKNOWN = 3


def _expected_secret_names(plan_artifact) -> list[str]:
    return [mapping["name"] for mapping in (plan_artifact.secret_mappings or [])]


def _format_names(names: list[str]) -> str:
    return ", ".join(names) if names else "none"


def _diff_envs_result(plan_artifact, vault: Vault) -> dict[str, list[str]]:
    expected_names = _expected_secret_names(plan_artifact)
    source_env = plan_artifact.source_env or plan_artifact.env
    target_env = plan_artifact.target_env or ""
    present_source = [
        name for name in expected_names if vault.secret_exists(name, source_env)
    ]
    missing_source = [name for name in expected_names if name not in present_source]
    present_target = [
        name for name in expected_names if vault.secret_exists(name, target_env)
    ]
    missing_target = [name for name in expected_names if name not in present_target]
    return {
        "expected": expected_names,
        "present_source": present_source,
        "missing_source": missing_source,
        "present_target": present_target,
        "missing_target": missing_target,
    }


def _print_diff_envs_summary(console: Console, result: dict[str, list[str]]) -> None:
    console.print(f"expected count: {len(result['expected'])}")
    console.print(f"present in source: {_format_names(result['present_source'])}", soft_wrap=True)
    console.print(f"missing in source: {_format_names(result['missing_source'])}", soft_wrap=True)
    console.print(f"present in target: {_format_names(result['present_target'])}", soft_wrap=True)
    console.print(f"missing in target: {_format_names(result['missing_target'])}", soft_wrap=True)
    console.print("secret values were not read")
    console.print("secret values were not printed")


def _secret_name_for_policy_audit(plan_artifact) -> str:
    if plan_artifact.secret_mappings:
        return "multiple"
    return plan_artifact.secret_name or "unknown"


def _fail_plan(
    plan_artifact,
    console: Console,
    message: str,
    *,
    secret_name: str | None = None,
    extra_lines: list[str] | None = None,
    mark_failed: bool,
) -> None:
    write_audit_event(
        action=plan_artifact.action,
        secret_name=secret_name or _secret_name_for_policy_audit(plan_artifact),
        env=plan_artifact.source_env or plan_artifact.env,
        status="failure",
        plan_id=plan_artifact.plan_id,
    )
    if mark_failed:
        set_plan_status(plan_artifact, "failed")
    console.print(f"[red]FAIL[/red] {message}")
    for line in extra_lines or []:
        console.print(line)
    raise typer.Exit(1)


def _fail_vault_error(
    plan_artifact,
    console: Console,
    error: VaultError,
    *,
    mark_failed: bool,
) -> None:
    # A vault error is never treated as "missing": the operation stops.
    _fail_plan(
        plan_artifact,
        console,
        f"vault error: {error}",
        extra_lines=["provider output was not printed", "secret value was not printed"],
        mark_failed=mark_failed,
    )


def _github_target(plan_artifact, console: Console) -> GitHubCliEnvSecretTarget:
    """GitHub adapter for the planned target; stops the plan if gh is unusable."""
    target = plan_artifact.target or {}
    try:
        return GitHubCliEnvSecretTarget(
            owner=target["target_owner"],
            repo=target["target_repo"],
            environment=target["target_environment"],
            host=target["target_host"],
        )
    except GitHubConfigurationError as error:
        _fail_plan(
            plan_artifact,
            console,
            f"GitHub configuration error: {error}",
            extra_lines=["gh was not called", "secret values were not printed"],
            mark_failed=True,
        )


GITHUB_RESULT_CATEGORIES = ("acknowledged", "unknown", "not_written", "not_attempted")
GITHUB_FAILURE_TEXT = {
    "source_missing": "source secret is missing",
    "vault_error": "the source secret could not be read from the vault",
    "launch_failed": "gh could not be started",
    "interrupted": "interrupted before the write started",
}


def _github_destination(plan_artifact) -> dict[str, str]:
    target = plan_artifact.target or {}
    return {
        key: target[key]
        for key in ("target_host", "target_owner", "target_repo", "target_environment")
        if key in target
    }


def _try_audit(**event) -> bool:
    """Write one audit event; report failure instead of raising it."""
    try:
        write_audit_event(**event)
    except OSError:
        return False
    return True


def _transfer_to_github(
    plan_artifact,
    console: Console,
    github_target: GitHubCliEnvSecretTarget,
    vault: Vault,
    transfers: list[tuple[str, str]],
    *,
    single: bool,
) -> None:
    """Send vault secrets to GitHub in saved order, stopping at the first
    write that is not acknowledged. Every mapping ends in exactly one
    category; an audit event is written after each attempt."""
    destination = _github_destination(plan_artifact)
    results: list[dict] = []
    stopped = False
    audit_complete = True
    for index, (source, target) in enumerate(transfers):
        entry = {"index": index, "source": source, "target": target}
        results.append(entry)
        if stopped:
            entry["outcome"] = "not_attempted"
            continue
        failure = None
        try:
            value = vault.read_secret_value(source, plan_artifact.env)
        except VaultError as error:
            value = None
            failure = "vault_error"
            # VaultError messages are safe to print; they stay out of audit.
            entry["detail"] = f"vault error: {error}"
        except KeyboardInterrupt:
            # The vault CLI is stopped by subprocess; nothing was sent to GitHub.
            value = None
            failure = "interrupted"
        if value is None and failure is None:
            failure = "source_missing"
        if failure is None:
            try:
                written = github_target.set_secret(target, value)
            except GitHubLaunchError:
                failure = "launch_failed"
            else:
                entry["outcome"] = (
                    "acknowledged" if written is GitHubWriteOutcome.ACKNOWLEDGED else "unknown"
                )
        # One value at a time: drop the reference before the next mapping.
        value = None
        if failure is not None:
            entry["outcome"] = "not_written"
            entry["failure"] = failure
        audit_complete = _try_audit(
            action=plan_artifact.action,
            secret_name=source,
            env=plan_artifact.env,
            status={"acknowledged": "success", "unknown": "outcome_unknown"}.get(
                entry["outcome"], "failure"
            ),
            plan_id=plan_artifact.plan_id,
            details={
                **destination,
                "step": "mapping",
                "mapping_index": index,
                "target_secret_name": target,
                "outcome": entry["outcome"],
                **({"failure": failure} if failure else {}),
            },
        )
        if entry["outcome"] != "acknowledged" or not audit_complete:
            stopped = True

    categories = {
        category: [
            {key: entry[key] for key in ("index", "source", "target")}
            for entry in results
            if entry["outcome"] == category
        ]
        for category in GITHUB_RESULT_CATEGORIES
    }
    unknown = bool(categories["unknown"])
    complete = len(categories["acknowledged"]) == len(results)
    if audit_complete:
        audit_complete = _try_audit(
            action=plan_artifact.action,
            secret_name=transfers[0][0] if single else "multiple",
            env=plan_artifact.env,
            status="outcome_unknown" if unknown else ("success" if complete else "failure"),
            plan_id=plan_artifact.plan_id,
            details={**destination, "step": "summary", **categories},
        )
    succeeded = complete and audit_complete
    try:
        set_plan_status(plan_artifact, "executed" if succeeded else "failed")
        status_saved = True
    except Exception:  # noqa: BLE001 - report results even if the store fails
        status_saved = False

    _print_github_results(console, results, categories, unknown=unknown, single=single)
    if not audit_complete:
        console.print("[red]WARNING[/red] the audit record could not be completed")
        console.print("the results above are what this run knows; they may be missing from the audit log")
    if not status_saved:
        console.print("[red]WARNING[/red] the plan status could not be updated")
        console.print("do not run this plan again; ask the operator to record its outcome")
    console.print("secret values were not printed")
    if unknown:
        raise typer.Exit(EXIT_OUTCOME_UNKNOWN)
    if not succeeded or not status_saved:
        # An unsaved status can leave the plan approved and executable.
        raise typer.Exit(EXIT_FAILED)


def _print_github_results(
    console: Console, results, categories, *, unknown: bool, single: bool
) -> None:
    def pairs(category: str) -> str:
        return ", ".join(
            f"{item['source']} -> {item['target']}" for item in categories[category]
        ) or "none"

    if single:
        entry = results[0]
        if entry["outcome"] == "acknowledged":
            console.print(f"OK GitHub write acknowledged: {entry['target']}")
        elif entry["outcome"] == "unknown":
            console.print(f"[red]FAIL[/red] GitHub write outcome unknown: {entry['target']}")
        else:
            console.print(f"[red]FAIL[/red] GitHub write not started: {entry['target']}")
            console.print(entry.get("detail") or GITHUB_FAILURE_TEXT[entry["failure"]])
    elif len(categories["acknowledged"]) == len(results):
        console.print(f"OK GitHub writes acknowledged: {len(results)}")
    else:
        console.print("[red]FAIL[/red] Sync stopped.")
        for category, label in (
            ("acknowledged", "Acknowledged"),
            ("unknown", "Unknown"),
            ("not_written", "Not written"),
            ("not_attempted", "Not attempted"),
        ):
            console.print(f"{label}: {len(categories[category])}")
        for category in GITHUB_RESULT_CATEGORIES:
            if categories[category]:
                console.print(f"{category}: {pairs(category)}", soft_wrap=True)
        for entry in results:
            if entry.get("failure"):
                reason = entry.get("detail") or GITHUB_FAILURE_TEXT[entry["failure"]]
                console.print(f"{entry['target']}: {reason}")
        if categories["acknowledged"]:
            console.print("acknowledged writes were not rolled back")
    if unknown:
        console.print("Do not retry automatically. Ask the operator to resolve the outcome first.")
        console.print("A later name check cannot show whether the intended value was stored.")


def _fail_unknown_write(plan_artifact, console: Console, secret_name: str, label: str) -> None:
    write_audit_event(
        action=plan_artifact.action,
        secret_name=secret_name,
        env=plan_artifact.env,
        status="outcome_unknown",
        plan_id=plan_artifact.plan_id,
    )
    set_plan_status(plan_artifact, "failed")
    console.print(f"[red]FAIL[/red] {label} write outcome unknown: {secret_name}")
    console.print(f"the write was not confirmed; check {secret_name} before any retry")
    console.print("no automatic retry was attempted")
    console.print("secret value was not printed")
    raise typer.Exit(EXIT_OUTCOME_UNKNOWN)


def _write_secrets_in_order(
    plan_artifact, console: Console, vault: Vault, writes, *, env: str, label: str
) -> None:
    """Write (name, read_value) pairs in order. Stop at the first write that is
    not confirmed and report which names were written; never roll back or retry."""
    names = [name for name, _ in writes]
    for index, (name, read_value) in enumerate(writes):
        try:
            value = read_value()
            if value is None:
                _stop_partial_write(
                    plan_artifact, console, names, index, label, env,
                    reason=f"source secret missing for: {name}",
                )
            outcome = vault.write_secret(name, env, value)
        except VaultError as error:
            # Raised before the CLI started for this name: nothing was sent.
            _stop_partial_write(
                plan_artifact, console, names, index, label, env, reason=f"vault error: {error}"
            )
        if outcome is WriteOutcome.UNKNOWN:
            _stop_partial_write(plan_artifact, console, names, index, label, env, reason=None)


def _stop_partial_write(
    plan_artifact,
    console: Console,
    names: list[str],
    index: int,
    label: str,
    env: str,
    *,
    reason: str | None,
) -> None:
    name = names[index]
    unknown = reason is None
    not_written = names[index + 1 :] if unknown else names[index:]
    # The audit event names the write that stopped the run and its target
    # environment, plus the partial result, so it survives beyond the console.
    write_audit_event(
        action=plan_artifact.action,
        secret_name=name,
        env=env,
        status="outcome_unknown" if unknown else "failure",
        plan_id=plan_artifact.plan_id,
        details={
            "confirmed_names": names[:index],
            "not_written_names": not_written,
        },
    )
    set_plan_status(plan_artifact, "failed")
    if unknown:
        console.print(f"[red]FAIL[/red] {label} write outcome unknown: {name}")
        console.print(f"the write was not confirmed; check {name} before any retry")
    else:
        console.print(f"[red]FAIL[/red] {label} was not written: {name}")
        console.print(reason)
    console.print(f"written and confirmed: {_format_names(names[:index])}", soft_wrap=True)
    console.print(f"not written: {_format_names(not_written)}", soft_wrap=True)
    console.print("no automatic rollback or retry was attempted")
    console.print("secret values were not printed")
    raise typer.Exit(EXIT_OUTCOME_UNKNOWN if unknown else EXIT_FAILED)


def _required_secret_name(plan_artifact, console: Console, *, mark_failed: bool) -> str:
    if plan_artifact.secret_name:
        return plan_artifact.secret_name
    _fail_plan(
        plan_artifact,
        console,
        f"missing required secret name for {plan_artifact.action}",
        secret_name="unknown",
        extra_lines=["secret value was not printed"],
        mark_failed=mark_failed,
    )
    raise typer.Exit(1)


def _required_secret_name_for_preflight(plan_artifact, console: Console) -> str:
    # Preflight must not mutate plan status; only execution marks plans failed.
    return _required_secret_name(plan_artifact, console, mark_failed=False)


def _required_secret_name_for_execution(plan_artifact, console: Console) -> str:
    return _required_secret_name(plan_artifact, console, mark_failed=True)


def _fail_plan_file(plan_id: str, command: str, console: Console, error: PlanFileError) -> None:
    # Nothing from the file is trusted: the file stays untouched, and output
    # and audit carry only the plan id from the command line and a category.
    write_audit_event(
        action=command,
        secret_name="unknown",
        env="unknown",
        status="failure",
        plan_id=plan_id,
        details={"failure": error.category},
    )
    console.print(f"[red]FAIL[/red] {error}")
    console.print("secret values were not printed")
    raise typer.Exit(1) from error


def _load_plan_or_exit(plan_id: str, command: str, console: Console):
    try:
        return load_plan(plan_id)
    except PlanFileError as error:
        _fail_plan_file(plan_id, command, console, error)


def _bound_vault(plan_artifact) -> Vault | None:
    """Vault adapter bound to the destination recorded in the plan."""
    if plan_artifact.vault_destination is None:
        return None
    return get_vault(plan_artifact.vault_destination)


def _binding_failures(plan_artifact) -> list[str]:
    """Return why this loaded plan must not reach a provider, or nothing.

    Raises VaultError when the configured destination cannot be resolved.
    """
    failures: list[str] = []
    if plan_artifact.approval_digest is not None or plan_artifact.status == "approved":
        if plan_artifact.approval_digest is None:
            failures.append("approved plan has no approval digest; replan required")
        elif not plan_artifact.approval_digest.startswith(DIGEST_PREFIX):
            failures.append("approval digest format is not supported; replan required")
        elif plan_digest(plan_artifact.loaded_content) != plan_artifact.approval_digest:
            failures.append("plan content changed after approval")
    if plan_artifact.action in VAULT_ACTIONS and not failures:
        configured = configured_vault_destination()
        recorded = plan_artifact.vault_destination or {}
        differing = [
            key for key in sorted(set(configured) | set(recorded))
            if configured.get(key) != recorded.get(key)
        ]
        if differing:
            failures.append(
                "vault destination differs from configuration: " + ", ".join(differing)
            )
    return failures


def _require_plan_binding(plan_artifact, console: Console, *, mark_failed: bool) -> None:
    """Refuse a plan whose approved content or vault destination changed.

    Runs before any adapter is built. Execution marks the plan failed from
    the loaded object; preflight does not change the status.
    """
    try:
        failures = _binding_failures(plan_artifact)
    except VaultError as error:
        _fail_vault_error(plan_artifact, console, error, mark_failed=mark_failed)
    if not failures:
        return
    _fail_plan(
        plan_artifact,
        console,
        failures[0],
        extra_lines=[
            "no provider call was made",
            "review the plan with show-plan; a changed destination needs a new plan",
            "secret values were not printed",
        ],
        mark_failed=mark_failed,
    )


def show_saved_plan(plan_id: str, console: Console) -> None:
    legacy = False
    try:
        plan_artifact = load_plan(plan_id)
    except PlanFileError as error:
        if error.category != "replan_required":
            _fail_plan_file(plan_id, "show-plan", console, error)
        try:
            plan_artifact = load_legacy_plan_for_display(plan_id)
        except PlanFileError as legacy_error:
            _fail_plan_file(plan_id, "show-plan", console, legacy_error)
        legacy = True
    printed_fields = set()

    def print_field_once(key: str, value) -> None:
        if key in printed_fields:
            return
        printed_fields.add(key)
        console.print(f"{key}: {value}")

    print_field_once("plan_id", plan_artifact.plan_id)
    print_field_once("status", plan_artifact.status)
    print_field_once("action", plan_artifact.action)
    if plan_artifact.secret_name is not None:
        print_field_once("source_secret_name", plan_artifact.secret_name)
    print_field_once("source_environment", plan_artifact.env)
    if plan_artifact.target:
        for key in sorted(plan_artifact.target):
            print_field_once(key, plan_artifact.target[key])
    if plan_artifact.action.startswith("github.env_secret.") and plan_artifact.target_env:
        print_field_once("target_environment", plan_artifact.target_env)
    if plan_artifact.secret_mappings:
        for mapping in plan_artifact.secret_mappings:
            if "name" in mapping:
                console.print(f"expected_secret: {mapping['name']}")
            else:
                console.print(f"secret_mapping: {mapping['source']} -> {mapping['target']}")
    if plan_artifact.generator:
        for key in sorted(plan_artifact.generator):
            console.print(f"generator_{key}: {plan_artifact.generator[key]}")

    if plan_artifact.action == "vault.secret.generate":
        console.print(f"create_mode: {plan_artifact.create_mode or 'create_only'}")

    if plan_artifact.action == "vault.secret.rotate":
        console.print(f"rotation_mode: {plan_artifact.rotation_mode or 'replace_existing'}")

    if plan_artifact.action == "vault.secret.import_file":
        console.print(f"source_file: {plan_artifact.source_file}")
        console.print(f"create_mode: {plan_artifact.create_mode or 'create_only'}")
    if plan_artifact.action == "vault.secret.copy_many":
        console.print(f"source_env: {plan_artifact.source_env or plan_artifact.env}")
        console.print(f"target_env: {plan_artifact.target_env}")
        console.print(f"create_mode: {plan_artifact.create_mode or 'create_only'}")
    if plan_artifact.action == "vault.secret.diff_envs":
        console.print(f"source_env: {plan_artifact.source_env or plan_artifact.env}")
        console.print(f"target_env: {plan_artifact.target_env}")
    if plan_artifact.action in HUMAN_ENTRY_ACTIONS:
        console.print(f"create_mode: {plan_artifact.create_mode}")
        console.print(f"value_source: {plan_artifact.value_source}")
    console.print(f"secret_value_included: {plan_artifact.secret_value_included}")
    if legacy:
        console.print("[red]plan was created before destination binding; replan required[/red]")
        return
    console.print(f"contract_version: {plan_artifact.contract_version}")
    if plan_artifact.vault_destination:
        for key in ("backend", "domain", "project_id", "path"):
            console.print(f"vault_{key}: {plan_artifact.vault_destination[key]}")
    console.print(f"plan_digest: {plan_digest(plan_artifact.loaded_content)}", soft_wrap=True)
    if plan_artifact.approval_digest is not None:
        console.print(f"approval_digest: {plan_artifact.approval_digest}", soft_wrap=True)


def transition_planned_plan(
    plan_id: str,
    status: str,
    label: str,
    console: Console,
) -> None:
    command = "approve-plan" if status == "approved" else "reject-plan"
    plan_artifact = _load_plan_or_exit(plan_id, command, console)
    if plan_artifact.action not in HUMAN_ENTRY_ACTIONS:
        _transition_loaded_plan(plan_artifact, status, label, console)
        return
    if status == "approved":
        # Approval of a human-entry plan is recorded only together with the
        # value, by the human submission endpoint.
        console.print(f"[red]FAIL[/red] {plan_artifact.action} is approved while entering the value")
        console.print(f"use: secretops add --plan {plan_id}")
        raise typer.Exit(1)
    # Rejection and submission exclude each other through the plan lock; the
    # plan is read again under the lock so a completed submission is seen.
    try:
        with plan_lock(plan_id):
            plan_artifact = _load_plan_or_exit(plan_id, command, console)
            _transition_loaded_plan(plan_artifact, status, label, console)
    except PlanBusyError as error:
        console.print(f"[red]FAIL[/red] {error}; try again later")
        raise typer.Exit(1) from error


def _transition_loaded_plan(plan_artifact, status: str, label: str, console: Console) -> None:
    plan_id = plan_artifact.plan_id
    if plan_artifact.status != "planned":
        console.print(
            f"[red]FAIL[/red] plan status is {plan_artifact.status}; expected planned"
        )
        raise typer.Exit(1)

    # Status and digest are written from the content that was just checked;
    # the file is not read again in between.
    digest = plan_digest(plan_artifact.loaded_content) if status == "approved" else None
    set_plan_status(plan_artifact, status, approval_digest=digest)
    console.print(f"OK plan {label}: {plan_id}")
    if digest is not None:
        console.print(f"approval_digest: {digest}", soft_wrap=True)


def preflight_saved_plan(plan_id: str, console: Console) -> None:
    plan_artifact = _load_plan_or_exit(plan_id, "preflight-plan", console)
    # Preflight must not mutate plan status.
    _require_plan_binding(plan_artifact, console, mark_failed=False)
    try:
        _preflight_plan(plan_artifact, console)
    except VaultError as error:
        # Preflight must not mutate plan status.
        _fail_vault_error(plan_artifact, console, error, mark_failed=False)


def _preflight_plan(plan_artifact, console: Console) -> None:
    vault = _bound_vault(plan_artifact)

    if plan_artifact.action in {"vault.read.check", "github.env_secret.write"}:
        source_secret = plan_artifact.secret_name
        if source_secret is None:
            console.print(f"[red]unsupported plan action: {plan_artifact.action}[/red]")
            raise typer.Exit(1)
        secret_exists = vault.secret_exists(source_secret, plan_artifact.env)
        if secret_exists:
            write_audit_event(
                action=plan_artifact.action,
                secret_name=source_secret,
                env=plan_artifact.env,
                status="success",
                plan_id=plan_artifact.plan_id,
            )
            console.print(f"OK preflight passed: {plan_artifact.action}")
            console.print(f"environment: {plan_artifact.env}")
            console.print(f"source secret exists: {source_secret}")
            if plan_artifact.action == "github.env_secret.write":
                console.print("GitHub API call was not made")
            console.print("secret value was not printed")
            return

        write_audit_event(
            action=plan_artifact.action,
            secret_name=source_secret,
            env=plan_artifact.env,
            status="failure",
            plan_id=plan_artifact.plan_id,
        )
        console.print(f"[red]FAIL[/red] preflight failed: {plan_artifact.action}")
        console.print(f"environment: {plan_artifact.env}")
        console.print(f"missing source secret: {source_secret}")
        if plan_artifact.action == "github.env_secret.write":
            console.print("GitHub API call was not made")
        raise typer.Exit(1)

    if plan_artifact.action == "github.env_secret.verify_many":
        target = plan_artifact.target or {}
        expected_names = _expected_secret_names(plan_artifact)
        write_audit_event(
            action=plan_artifact.action,
            secret_name="multiple",
            env=target.get("target_environment") or plan_artifact.env,
            status="success",
            plan_id=plan_artifact.plan_id,
        )
        console.print("OK preflight passed: github.env_secret.verify_many")
        console.print(f"target_owner: {target.get('target_owner')}")
        console.print(f"target_repo: {target.get('target_repo')}")
        console.print(f"target_environment: {target.get('target_environment')}")
        console.print(f"expected secrets configured: {len(expected_names)}")
        console.print("GitHub API call was not made")
        console.print("secret values were not printed")
        return

    if plan_artifact.action == "github.env_secret.sync_many":
        mappings = plan_artifact.secret_mappings or []
        missing_sources = [
            mapping["source"]
            for mapping in mappings
            if not vault.secret_exists(mapping["source"], plan_artifact.env)
        ]
        status = "failure" if missing_sources else "success"
        write_audit_event(
            action=plan_artifact.action,
            secret_name="multiple",
            env=plan_artifact.env,
            status=status,
            plan_id=plan_artifact.plan_id,
        )
        if missing_sources:
            console.print(f"[red]FAIL[/red] preflight failed: {plan_artifact.action}")
        else:
            console.print(f"OK preflight passed: {plan_artifact.action}")
        console.print(f"environment: {plan_artifact.env}")
        console.print(f"mappings checked: {len(mappings)}")
        console.print(
            "missing source secrets: "
            + (", ".join(missing_sources) if missing_sources else "none")
        )
        console.print("GitHub API call was not made")
        console.print("secret value was not printed")
        if missing_sources:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.secret.diff_envs":
        source_env = plan_artifact.source_env or plan_artifact.env
        result = _diff_envs_result(plan_artifact, vault)
        failed = bool(result["missing_source"] or result["missing_target"])
        write_audit_event(
            action=plan_artifact.action,
            secret_name="multiple",
            env=source_env,
            status="failure" if failed else "success",
            plan_id=plan_artifact.plan_id,
        )
        if failed:
            console.print("[red]FAIL[/red] preflight failed: vault.secret.diff_envs")
        else:
            console.print("OK preflight passed: vault.secret.diff_envs")
        console.print(f"source_env: {source_env}")
        console.print(f"target_env: {plan_artifact.target_env or ''}")
        _print_diff_envs_summary(console, result)
        if failed:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.secret.copy_many":
        mappings = plan_artifact.secret_mappings or []
        source_env = plan_artifact.source_env or plan_artifact.env
        target_env = plan_artifact.target_env or ""
        create_mode = plan_artifact.create_mode or "create_only"
        missing_sources = [
            mapping["source"]
            for mapping in mappings
            if not vault.secret_exists(mapping["source"], source_env)
        ]
        existing_targets = [
            mapping["target"]
            for mapping in mappings
            if vault.secret_exists(mapping["target"], target_env)
        ]
        failed = bool(missing_sources) or (
            create_mode == "create_only" and bool(existing_targets)
        )
        write_audit_event(
            action=plan_artifact.action,
            secret_name="multiple",
            env=source_env,
            status="failure" if failed else "success",
            plan_id=plan_artifact.plan_id,
        )
        if failed:
            console.print("[red]FAIL[/red] preflight failed: vault.secret.copy_many")
        else:
            console.print("OK preflight passed: vault.secret.copy_many")
        console.print(f"source_env: {source_env}")
        console.print(f"target_env: {target_env}")
        console.print(f"create_mode: {create_mode}")
        console.print(f"mappings checked: {len(mappings)}")
        console.print(
            "missing source secrets: "
            + (", ".join(missing_sources) if missing_sources else "none")
        )
        console.print(
            "existing target secrets: "
            + (", ".join(existing_targets) if existing_targets else "none")
        )
        if create_mode == "create_only" and existing_targets:
            console.print("execution would not overwrite existing secrets")
        elif not failed:
            console.print("copy can proceed later")
        console.print("secret values were not read")
        console.print("secret values were not printed")
        if failed:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.secret.generate":
        generator = plan_artifact.generator or {}
        create_mode = plan_artifact.create_mode or "create_only"
        target_secret = _required_secret_name_for_preflight(plan_artifact, console)
        target_exists = vault.secret_exists(target_secret, plan_artifact.env)
        status = "failure" if target_exists else "success"
        write_audit_event(
            action=plan_artifact.action,
            secret_name=target_secret,
            env=plan_artifact.env,
            status=status,
            plan_id=plan_artifact.plan_id,
        )
        if target_exists:
            console.print("[red]FAIL[/red] preflight failed: vault.secret.generate")
        else:
            console.print("OK preflight passed: vault.secret.generate")
        console.print(f"environment: {plan_artifact.env}")
        console.print(f"target secret: {target_secret}")
        console.print(f"create_mode: {create_mode}")
        console.print(f"target secret exists: {target_exists}")
        if "type" in generator:
            console.print(f"generator type: {generator['type']}")
        if "length" in generator:
            console.print(f"generator length: {generator['length']}")
        if target_exists:
            console.print("execution would not overwrite existing secret")
        else:
            console.print("generation can proceed")
        console.print("secret value was not generated")
        console.print("secret value was not printed")
        if target_exists:
            raise typer.Exit(1)
        return

    if plan_artifact.action in HUMAN_ENTRY_ACTIONS:
        target_secret = _required_secret_name_for_preflight(plan_artifact, console)
        target_exists = vault.secret_exists(target_secret, plan_artifact.env)
        write_audit_event(
            action=plan_artifact.action,
            secret_name=target_secret,
            env=plan_artifact.env,
            status="failure" if target_exists else "success",
            plan_id=plan_artifact.plan_id,
        )
        if target_exists:
            console.print(f"[red]FAIL[/red] preflight failed: {plan_artifact.action}")
        else:
            console.print(f"OK preflight passed: {plan_artifact.action}")
        console.print(f"environment: {plan_artifact.env}")
        console.print(f"target secret: {target_secret}")
        console.print(f"create_mode: {plan_artifact.create_mode}")
        console.print(f"target secret exists: {target_exists}")
        if target_exists:
            console.print("the secret would not be created")
        else:
            console.print(f"a human can enter the value with: secretops add --plan {plan_artifact.plan_id}")
        console.print("secret value was not printed")
        if target_exists:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.secret.rotate":
        generator = plan_artifact.generator or {}
        rotation_mode = plan_artifact.rotation_mode or "replace_existing"
        target_secret = _required_secret_name_for_preflight(plan_artifact, console)
        target_exists = vault.secret_exists(target_secret, plan_artifact.env)
        status = "success" if target_exists else "failure"
        write_audit_event(
            action=plan_artifact.action,
            secret_name=target_secret,
            env=plan_artifact.env,
            status=status,
            plan_id=plan_artifact.plan_id,
        )
        if target_exists:
            console.print("OK preflight passed: vault.secret.rotate")
        else:
            console.print("[red]FAIL[/red] preflight failed: vault.secret.rotate")
        console.print(f"environment: {plan_artifact.env}")
        console.print(f"target secret: {target_secret}")
        console.print(f"rotation_mode: {rotation_mode}")
        console.print(f"target secret exists: {target_exists}")
        if "type" in generator:
            console.print(f"generator type: {generator['type']}")
        if "length" in generator:
            console.print(f"generator length: {generator['length']}")
        if target_exists:
            console.print("rotation can proceed later")
        else:
            console.print("use vault.secret.generate instead")
        console.print("secret value was not generated")
        console.print("secret value was not printed")
        if not target_exists:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.secret.import_file":
        create_mode = plan_artifact.create_mode or "create_only"
        source_file = plan_artifact.source_file or ""
        source_path = Path(source_file)
        source_exists = source_path.exists()
        source_is_file = source_path.is_file()
        permissions_restricted = (
            source_file_permissions_restricted(source_path)
            if source_exists and source_is_file
            else False
        )
        import_names = planned_import_names(
            plan_artifact.secret_mappings, plan_artifact.secret_name
        )
        parse_error = None
        if source_exists and source_is_file and permissions_restricted:
            try:
                entries = parse_dotenv_file(source_path)
                import_names = [entry.name for entry in entries]
            except ImportFileError as error:
                parse_error = str(error)
        planned_names = planned_import_names(
            plan_artifact.secret_mappings, plan_artifact.secret_name
        )
        names_changed = bool(planned_names) and import_names != planned_names
        existing_targets = [
            name
            for name in import_names
            if create_mode == "create_only" and vault.secret_exists(name, plan_artifact.env)
        ]
        failed = (
            (not source_exists)
            or (not source_is_file)
            or (not permissions_restricted)
            or parse_error is not None
            or not import_names
            or not planned_names
            or names_changed
            or bool(existing_targets)
            or create_mode != "create_only"
        )
        status = "failure" if failed else "success"
        write_audit_event(
            action=plan_artifact.action,
            secret_name="multiple" if len(import_names) != 1 else import_names[0],
            env=plan_artifact.env,
            status=status,
            plan_id=plan_artifact.plan_id,
        )
        if failed:
            console.print("[red]FAIL[/red] preflight failed: vault.secret.import_file")
        else:
            console.print("OK preflight passed: vault.secret.import_file")
        console.print(f"environment: {plan_artifact.env}")
        console.print(f"source_file: {source_file}")
        console.print(f"source file exists: {source_exists}")
        console.print(f"source file is file: {source_is_file}")
        console.print(f"source file permissions restricted: {permissions_restricted}")
        console.print(f"create_mode: {create_mode}")
        console.print(f"secret count: {len(import_names)}")
        console.print(f"secret names: {_format_names(import_names)}", soft_wrap=True)
        console.print(
            "existing target secrets: "
            + (", ".join(existing_targets) if existing_targets else "none")
        )
        if parse_error:
            console.print(parse_error)
        if not import_names:
            console.print(f"missing required secret name for {plan_artifact.action}")
        if not planned_names:
            console.print("saved plan has no imported secret names")
        if names_changed:
            console.print("source file names do not match saved plan")
        if create_mode != "create_only":
            console.print(f"unsupported create_mode: {create_mode}")
        if existing_targets:
            console.print("execution would not overwrite existing secrets")
        elif not failed:
            console.print("import can proceed later")
        console.print("secret values were not printed")
        if failed:
            raise typer.Exit(1)
        return

    console.print(f"[red]unsupported plan action: {plan_artifact.action}[/red]")
    write_audit_event(
        action=plan_artifact.action,
        secret_name=plan_artifact.secret_name or "multiple",
        env=plan_artifact.env,
        status="failure",
        plan_id=plan_artifact.plan_id,
    )
    raise typer.Exit(1)


def _execution_policy_environments(plan_artifact) -> list[str]:
    environments = [plan_artifact.env]
    if plan_artifact.source_env:
        environments.append(plan_artifact.source_env)
    if plan_artifact.target_env:
        environments.append(plan_artifact.target_env)
    target = plan_artifact.target or {}
    if target.get("target_environment"):
        environments.append(target["target_environment"])
    return list(dict.fromkeys(environment for environment in environments if environment))


def _approval_required_message(action: str) -> str:
    return {
        "github.env_secret.plan": "GitHub plan requires approval",
        "github.env_secret.write": "GitHub write plan requires approval",
        "github.env_secret.sync_many": "GitHub sync_many plan requires approval",
    }.get(action, f"{action} plan requires approval")


def _require_plan_execution_allowed(plan_artifact, console: Console) -> None:
    try:
        for environment in _execution_policy_environments(plan_artifact):
            require_environment_allowed(plan_artifact.action, environment)
        if requires_approval(plan_artifact.action) and plan_artifact.status != "approved":
            raise PermissionError(_approval_required_message(plan_artifact.action))
    except PermissionError as error:
        write_audit_event(
            action=plan_artifact.action,
            secret_name=_secret_name_for_policy_audit(plan_artifact),
            env=plan_artifact.source_env or plan_artifact.env,
            status="failure",
            plan_id=plan_artifact.plan_id,
        )
        set_plan_status(plan_artifact, "failed")
        console.print(f"[red]FAIL[/red] {error}")
        if plan_artifact.action in {"vault.secret.generate", "vault.secret.rotate"}:
            console.print("secret value was not generated")
        if plan_artifact.action == "vault.secret.import_file":
            console.print("source file contents were not read")
        console.print("secret value was not printed")
        raise typer.Exit(1) from error


def execute_saved_plan(plan_id: str, console: Console) -> None:
    # The plan is read once; checks and the operation use this object.
    plan_artifact = _load_plan_or_exit(plan_id, "execute-plan", console)

    if plan_artifact.status == "rejected":
        console.print("[red]FAIL[/red] rejected plan cannot execute")
        raise typer.Exit(1)

    # Refused before any other check, which would mark the plan failed and
    # overwrite the recorded outcome. Running again needs a new plan.
    if plan_artifact.status == "executed":
        console.print("[red]FAIL[/red] plan already executed; save a new plan to run it again")
        console.print("no provider call was made")
        raise typer.Exit(1)

    if plan_artifact.action in HUMAN_ENTRY_ACTIONS:
        # There is no value to execute with: a human enters it through the
        # human broker access. The plan is left unchanged for that step.
        write_audit_event(
            action=plan_artifact.action,
            secret_name=_secret_name_for_policy_audit(plan_artifact),
            env=plan_artifact.env,
            status="failure",
            plan_id=plan_artifact.plan_id,
            details={"failure": "human_completion_required"},
        )
        console.print(f"[red]FAIL[/red] {plan_artifact.action} requires human completion")
        console.print(f"a human runs: secretops add --plan {plan_artifact.plan_id}")
        console.print("no provider call was made")
        console.print("secret value was not printed")
        raise typer.Exit(1)

    # Repeated even after a passing preflight: configuration can change.
    _require_plan_binding(plan_artifact, console, mark_failed=True)
    _require_plan_execution_allowed(plan_artifact, console)

    try:
        _execute_plan(plan_artifact, console)
    except VaultError as error:
        _fail_vault_error(plan_artifact, console, error, mark_failed=True)


def _execute_plan(plan_artifact, console: Console) -> None:
    if plan_artifact.action == "github.env_secret.plan":
        _fail_plan(
            plan_artifact,
            console,
            "github.env_secret.plan is planning only and is not executable",
            extra_lines=["secret value was not printed"],
            mark_failed=True,
        )

    if plan_artifact.action == "vault.secret.diff_envs":
        vault = _bound_vault(plan_artifact)
        source_env = plan_artifact.source_env or plan_artifact.env
        result = _diff_envs_result(plan_artifact, vault)
        failed = bool(result["missing_source"] or result["missing_target"])
        write_audit_event(
            action=plan_artifact.action,
            secret_name="multiple",
            env=source_env,
            status="failure" if failed else "success",
            plan_id=plan_artifact.plan_id,
        )
        set_plan_status(plan_artifact, "failed" if failed else "executed")
        if failed:
            console.print(
                "[red]FAIL[/red] vault environment secret name diff found missing expected names"
            )
        else:
            console.print(
                f"OK vault environment secret names match: {len(result['expected'])}"
            )
        console.print(f"source_env: {source_env}")
        console.print(f"target_env: {plan_artifact.target_env or ''}")
        _print_diff_envs_summary(console, result)
        if failed:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.secret.import_file":
        vault = _bound_vault(plan_artifact)
        create_mode = plan_artifact.create_mode or "create_only"
        source_file = plan_artifact.source_file or ""
        source_path = Path(source_file)
        planned_names = planned_import_names(
            plan_artifact.secret_mappings, plan_artifact.secret_name
        )
        failure_lines: list[str] = []
        entries = []
        source_exists = source_path.exists()
        source_is_file = source_path.is_file()
        permissions_restricted = (
            source_file_permissions_restricted(source_path)
            if source_exists and source_is_file
            else False
        )
        if create_mode != "create_only":
            failure_lines.append(f"unsupported create_mode: {create_mode}")
        if not source_exists:
            failure_lines.append("source file exists: False")
        elif not source_is_file:
            failure_lines.append("source file is file: False")
        elif not permissions_restricted:
            failure_lines.append("source file permissions restricted: False")
        else:
            try:
                entries = parse_dotenv_file(source_path)
            except ImportFileError as error:
                failure_lines.append(str(error))

        import_names = [entry.name for entry in entries] or planned_names
        if not planned_names:
            failure_lines.append("saved plan has no imported secret names")
        if entries and planned_names and import_names != planned_names:
            failure_lines.append("source file names do not match saved plan")
        if not import_names:
            failure_lines.append(f"missing required secret name for {plan_artifact.action}")
        existing_targets = [
            name
            for name in import_names
            if create_mode == "create_only" and vault.secret_exists(name, plan_artifact.env)
        ]
        if existing_targets:
            failure_lines.append("existing target secrets: " + ", ".join(existing_targets))
            failure_lines.append("execution would not overwrite existing secrets")

        audit_secret_name = "multiple" if len(import_names) != 1 else import_names[0]
        if failure_lines:
            write_audit_event(
                action=plan_artifact.action,
                secret_name=audit_secret_name,
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print("[red]FAIL[/red] vault.secret.import_file safety check failed")
            console.print(f"environment: {plan_artifact.env}")
            console.print(f"source_file: {source_file}")
            console.print(f"secret count: {len(import_names)}")
            console.print(f"secret names: {_format_names(import_names)}", soft_wrap=True)
            for line in failure_lines:
                console.print(line)
            console.print("secret values were not printed")
            raise typer.Exit(1)

        _write_secrets_in_order(
            plan_artifact,
            console,
            vault,
            [(entry.name, lambda entry=entry: entry.value) for entry in entries],
            env=plan_artifact.env,
            label="imported secret",
        )

        write_audit_event(
            action=plan_artifact.action,
            secret_name=audit_secret_name,
            env=plan_artifact.env,
            status="success",
            plan_id=plan_artifact.plan_id,
        )
        set_plan_status(plan_artifact, "executed")
        console.print(f"OK imported dotenv secrets: {len(entries)}")
        console.print(f"environment: {plan_artifact.env}")
        console.print(f"secret names: {_format_names(import_names)}", soft_wrap=True)
        console.print("secret values were not printed")
        return

    if plan_artifact.action == "vault.secret.rotate":
        target_secret = _required_secret_name_for_execution(plan_artifact, console)
        if plan_artifact.status != "approved":
            write_audit_event(
                action=plan_artifact.action,
                secret_name=target_secret,
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print("[red]FAIL[/red] vault.secret.rotate plan requires approval")
            console.print("secret value was not generated")
            console.print("secret value was not printed")
            raise typer.Exit(1)

        vault = _bound_vault(plan_artifact)
        if not vault.secret_exists(target_secret, plan_artifact.env):
            write_audit_event(
                action=plan_artifact.action,
                secret_name=target_secret,
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print(f"[red]FAIL[/red] rotated secret target is missing: {target_secret}")
            console.print("use vault.secret.generate instead")
            console.print("secret value was not generated")
            console.print("secret value was not printed")
            raise typer.Exit(1)

        try:
            rotated_value = generate_secret_value(plan_artifact.generator or {})
        except Exception:
            _fail_plan(
                plan_artifact,
                console,
                f"rotated secret was not stored: {target_secret}",
                secret_name=target_secret,
                extra_lines=["secret value was not generated", "secret value was not printed"],
                mark_failed=True,
            )

        outcome = vault.write_secret(target_secret, plan_artifact.env, rotated_value)
        if outcome is WriteOutcome.CONFIRMED:
            write_audit_event(
                action=plan_artifact.action,
                secret_name=target_secret,
                env=plan_artifact.env,
                status="success",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "executed")
            console.print(f"OK rotated secret stored: {target_secret}")
            console.print("secret value was not printed")
            return

        _fail_unknown_write(plan_artifact, console, target_secret, "rotated secret")


    if plan_artifact.action == "vault.secret.copy_many":
        if plan_artifact.status != "approved":
            write_audit_event(
                action=plan_artifact.action,
                secret_name="multiple",
                env=plan_artifact.source_env or plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print("[red]FAIL[/red] vault.secret.copy_many plan requires approval")
            console.print("secret values were not printed")
            raise typer.Exit(1)

        vault = _bound_vault(plan_artifact)
        mappings = plan_artifact.secret_mappings or []
        source_env = plan_artifact.source_env or plan_artifact.env
        target_env = plan_artifact.target_env or ""
        create_mode = plan_artifact.create_mode or "create_only"
        missing_sources = [
            mapping["source"]
            for mapping in mappings
            if not vault.secret_exists(mapping["source"], source_env)
        ]
        existing_targets = [
            mapping["target"]
            for mapping in mappings
            if vault.secret_exists(mapping["target"], target_env)
        ]
        if missing_sources or (create_mode == "create_only" and existing_targets):
            write_audit_event(
                action=plan_artifact.action,
                secret_name="multiple",
                env=source_env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print("[red]FAIL[/red] vault.secret.copy_many safety check failed")
            console.print(
                "missing source secrets: "
                + (", ".join(missing_sources) if missing_sources else "none")
            )
            console.print(
                "existing target secrets: "
                + (", ".join(existing_targets) if existing_targets else "none")
            )
            console.print("secret values were not printed")
            raise typer.Exit(1)

        _write_secrets_in_order(
            plan_artifact,
            console,
            vault,
            [
                (
                    mapping["target"],
                    lambda source=mapping["source"]: vault.read_secret_value(source, source_env),
                )
                for mapping in mappings
            ],
            env=target_env,
            label="copied secret",
        )

        write_audit_event(
            action=plan_artifact.action,
            secret_name="multiple",
            env=source_env,
            status="success",
            plan_id=plan_artifact.plan_id,
        )
        set_plan_status(plan_artifact, "executed")
        console.print(f"OK vault secrets copied: {len(mappings)}")
        console.print("secret values were not printed")
        return

    if plan_artifact.action == "vault.secret.generate":
        target_secret = _required_secret_name_for_execution(plan_artifact, console)
        if plan_artifact.status != "approved":
            write_audit_event(
                action=plan_artifact.action,
                secret_name=target_secret,
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print("[red]FAIL[/red] vault.secret.generate plan requires approval")
            raise typer.Exit(1)

        vault = _bound_vault(plan_artifact)
        create_mode = plan_artifact.create_mode or "create_only"
        if create_mode != "create_only":
            write_audit_event(
                action=plan_artifact.action,
                secret_name=target_secret,
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print(f"[red]FAIL[/red] unsupported create_mode: {create_mode}")
            console.print("secret value was not generated")
            console.print("secret value was not printed")
            raise typer.Exit(1)

        if vault.secret_exists(target_secret, plan_artifact.env):
            write_audit_event(
                action=plan_artifact.action,
                secret_name=target_secret,
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print(f"[red]FAIL[/red] generated secret already exists: {target_secret}")
            console.print(f"create_mode: {create_mode}")
            console.print("secret value was not generated")
            console.print("secret value was not printed")
            raise typer.Exit(1)

        try:
            generated_value = generate_secret_value(plan_artifact.generator or {})
        except Exception:
            _fail_plan(
                plan_artifact,
                console,
                f"generated secret was not stored: {target_secret}",
                secret_name=target_secret,
                extra_lines=["secret value was not generated", "secret value was not printed"],
                mark_failed=True,
            )

        outcome = vault.write_secret(target_secret, plan_artifact.env, generated_value)
        if outcome is WriteOutcome.CONFIRMED:
            write_audit_event(
                action=plan_artifact.action,
                secret_name=target_secret,
                env=plan_artifact.env,
                status="success",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "executed")
            console.print(f"OK generated secret stored: {plan_artifact.secret_name}")
            console.print("secret value was not printed")
            return

        _fail_unknown_write(plan_artifact, console, target_secret, "generated secret")

    if plan_artifact.action == "github.env_secret.verify_many":
        target = plan_artifact.target or {}
        expected_names = _expected_secret_names(plan_artifact)
        github_target = _github_target(plan_artifact, console)
        try:
            returned_names = github_target.list_secret_names()
        except GitHubError as error:
            write_audit_event(
                action=plan_artifact.action,
                secret_name="multiple",
                env=target.get("target_environment") or plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print("[red]FAIL[/red] GitHub environment secret verification failed")
            console.print(str(error))
            console.print(f"expected count: {len(expected_names)}")
            console.print("provider output was not printed")
            console.print("secret values were not printed")
            raise typer.Exit(EXIT_FAILED) from error

        # GitHub secret names are case-insensitive. Only expected names are
        # compared and shown; other names in the environment are ignored.
        returned_name_set = {name.upper() for name in returned_names}
        present_names = [name for name in expected_names if name.upper() in returned_name_set]
        missing_names = [name for name in expected_names if name.upper() not in returned_name_set]
        failed = bool(missing_names)
        write_audit_event(
            action=plan_artifact.action,
            secret_name="multiple",
            env=target.get("target_environment") or plan_artifact.env,
            status="failure" if failed else "success",
            plan_id=plan_artifact.plan_id,
        )
        set_plan_status(plan_artifact, "failed" if failed else "executed")
        if failed:
            console.print(
                "[red]FAIL[/red] GitHub environment secret verification missing expected names"
            )
        else:
            console.print(
                f"OK GitHub environment secrets verified: {len(expected_names)}"
            )
        console.print(f"expected count: {len(expected_names)}")
        console.print(f"present names: {_format_names(present_names)}", soft_wrap=True)
        console.print(f"missing names: {_format_names(missing_names)}", soft_wrap=True)
        console.print("secret values were not printed")
        if failed:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.read.check":
        if plan_artifact.status not in {"planned", "approved"}:
            console.print(
                f"[red]FAIL[/red] vault read plan cannot execute with status: {plan_artifact.status}"
            )
            raise typer.Exit(1)

        vault = _bound_vault(plan_artifact)
        source_secret = _required_secret_name_for_execution(plan_artifact, console)
        if vault.secret_exists(source_secret, plan_artifact.env):
            write_audit_event(
                action=plan_artifact.action,
                secret_name=source_secret,
                env=plan_artifact.env,
                status="success",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "executed")
            console.print(f"OK secret exists: {source_secret}")
            console.print("secret value was not printed")
        else:
            write_audit_event(
                action=plan_artifact.action,
                secret_name=source_secret,
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            console.print(
                f"[red]FAIL[/red] secret missing or inaccessible: {source_secret}"
            )
            raise typer.Exit(1)
    elif plan_artifact.action == "github.env_secret.sync_many":
        if plan_artifact.status != "approved":
            console.print("[red]FAIL[/red] GitHub sync_many plan requires approval")
            write_audit_event(
                action=plan_artifact.action,
                secret_name="multiple",
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            raise typer.Exit(1)

        github_target = _github_target(plan_artifact, console)
        vault = _bound_vault(plan_artifact)
        _transfer_to_github(
            plan_artifact,
            console,
            github_target,
            vault,
            [(mapping["source"], mapping["target"]) for mapping in plan_artifact.secret_mappings or []],
            single=False,
        )
    elif plan_artifact.action == "github.env_secret.write":
        if plan_artifact.status != "approved":
            console.print("[red]FAIL[/red] GitHub write plan requires approval")
            write_audit_event(
                action=plan_artifact.action,
                secret_name=plan_artifact.secret_name,
                env=plan_artifact.env,
                status="failure",
                plan_id=plan_artifact.plan_id,
            )
            set_plan_status(plan_artifact, "failed")
            raise typer.Exit(1)

        target = plan_artifact.target or {}
        github_target = _github_target(plan_artifact, console)
        vault = _bound_vault(plan_artifact)
        source_secret = _required_secret_name_for_execution(plan_artifact, console)
        _transfer_to_github(
            plan_artifact,
            console,
            github_target,
            vault,
            [(source_secret, target["target_secret_name"])],
            single=True,
        )
    else:
        console.print(f"[red]unsupported plan action: {plan_artifact.action}[/red]")
        write_audit_event(
            action=plan_artifact.action,
            secret_name=plan_artifact.secret_name,
            env=plan_artifact.env,
            status="failure",
            plan_id=plan_artifact.plan_id,
        )
        set_plan_status(plan_artifact, "failed")
        raise typer.Exit(1)


