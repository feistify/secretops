import contextlib
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from secretops.audit import write_audit_event
from secretops.generators import generate_secret_value
from secretops.import_file import (
    ImportFileError,
    parse_dotenv_file,
    planned_import_names,
    source_file_permissions_restricted,
)
from secretops.plans import (
    DIGEST_PREFIX,
    HUMAN_ENTRY_ACTIONS,
    VAULT_ACTIONS,
    PLAN_ID_PATTERN,
    PlanBusyError,
    PlanFileError,
    PlanLockError,
    format_timestamp,
    load_legacy_plan_for_display,
    load_plan,
    parse_timestamp,
    plan_digest,
    plan_lock,
    set_plan_status,
    utc_now,
)
from secretops.policy import load_policy, require_environment_allowed, requires_approval
from secretops.targets import (
    GitHubCliEnvSecretTarget,
    GitHubConfigurationError,
    GitHubError,
    GitHubLaunchError,
    GitHubWriteOutcome,
)
from secretops.validation import safe_text, shown
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
    console.print(f"present in source: {shown(_format_names(result['present_source']))}", soft_wrap=True)
    console.print(f"missing in source: {shown(_format_names(result['missing_source']))}", soft_wrap=True)
    console.print(f"present in target: {shown(_format_names(result['present_target']))}", soft_wrap=True)
    console.print(f"missing in target: {shown(_format_names(result['missing_target']))}", soft_wrap=True)
    console.print("secret values were not read")
    console.print("secret values were not printed")


def _secret_name_for_policy_audit(plan_artifact) -> str:
    if plan_artifact.secret_mappings:
        return "multiple"
    return plan_artifact.secret_name or "unknown"


def _audit_env(plan_artifact) -> str:
    """The environment an audit event names for this plan: where a copy
    writes (its target), otherwise the source or the plan's environment.
    A copy's source is in `source_env` (see _audit_source)."""
    if plan_artifact.action == "vault.secret.copy_many":
        return plan_artifact.target_env or plan_artifact.env
    return plan_artifact.source_env or plan_artifact.env


def _audit_source(plan_artifact) -> dict[str, str]:
    """Audit details naming a copy's source environment (its events name the
    target in `env`), so a search by either environment finds them."""
    if plan_artifact.action == "vault.secret.copy_many":
        return {"source_env": plan_artifact.source_env or plan_artifact.env}
    return {}


def _fail_plan(
    plan_artifact,
    console: Console,
    message: str,
    *,
    secret_name: str | None = None,
    extra_lines: list[str] | None = None,
    mark_failed: bool,
) -> None:
    source = _audit_source(plan_artifact)
    write_audit_event(
        action=plan_artifact.action,
        secret_name=secret_name or _secret_name_for_policy_audit(plan_artifact),
        env=_audit_env(plan_artifact),
        status="failure",
        plan_id=plan_artifact.plan_id,
        details={**source},
    )
    if mark_failed:
        set_plan_status(plan_artifact, "failed")
    console.print(f"[red]FAIL[/red] {shown(message)}")
    for line in extra_lines or []:
        console.print(shown(line))
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


def _record_write_start(plan_artifact, *, secret_name: str, env: str, details: dict) -> None:
    """Record that provider writes are about to start: no audit, no write.

    Raises AuditWriteError before any vault or GitHub write when the audit
    log cannot be written; the plan is still unchanged then, and the CLI
    stops with a fixed message. Audit events after a write use _try_audit.
    """
    write_audit_event(
        action=plan_artifact.action,
        secret_name=secret_name,
        env=env,
        status="started",
        plan_id=plan_artifact.plan_id,
        details={**details, "step": "write_started"},
    )


def _try_audit(**event) -> bool:
    """Write one audit event; report failure instead of raising it."""
    try:
        write_audit_event(**event)
    except OSError:
        return False
    return True


def _try_save_status(plan_artifact, status: str) -> bool:
    """Save the plan status after a write was attempted; report failure
    instead of raising it, so the write results are still shown."""
    try:
        set_plan_status(plan_artifact, status)
    except Exception:  # noqa: BLE001 - report results even if the store fails
        return False
    return True


def _warn_audit_incomplete(console: Console) -> None:
    console.print("[red]WARNING[/red] the audit record could not be completed")
    console.print("the results above are what this run knows; they may be missing from the audit log")


def _warn_status_not_saved(console: Console) -> None:
    # An unsaved status can leave the plan approved and executable.
    console.print("[red]WARNING[/red] the plan status could not be updated")
    console.print("do not run this plan again; ask the operator to record its outcome")


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
    _record_write_start(
        plan_artifact,
        secret_name=transfers[0][0] if single else "multiple",
        env=plan_artifact.env,
        details={
            **destination,
            "source_secret_names": [source for source, _ in transfers],
            "target_secret_names": [target for _, target in transfers],
        },
    )
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
    status_saved = _try_save_status(plan_artifact, "executed" if succeeded else "failed")

    _print_github_results(console, results, categories, unknown=unknown, single=single)
    if not audit_complete:
        _warn_audit_incomplete(console)
    if not status_saved:
        _warn_status_not_saved(console)
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
            console.print(f"OK GitHub write acknowledged: {shown(entry['target'])}")
        elif entry["outcome"] == "unknown":
            console.print(f"[red]FAIL[/red] GitHub write outcome unknown: {shown(entry['target'])}")
        else:
            console.print(f"[red]FAIL[/red] GitHub write not started: {shown(entry['target'])}")
            console.print(shown(entry.get("detail") or GITHUB_FAILURE_TEXT[entry["failure"]]))
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
            console.print(f"{shown(label)}: {len(categories[category])}")
        for category in GITHUB_RESULT_CATEGORIES:
            if categories[category]:
                console.print(f"{shown(category)}: {shown(pairs(category))}", soft_wrap=True)
        for entry in results:
            if entry.get("failure"):
                reason = entry.get("detail") or GITHUB_FAILURE_TEXT[entry["failure"]]
                console.print(f"{shown(entry['target'])}: {shown(reason)}")
        if categories["acknowledged"]:
            console.print("acknowledged writes were not rolled back")
    if unknown:
        console.print("Do not retry automatically. Ask the operator to resolve the outcome first.")
        console.print("A later name check cannot show whether the intended value was stored.")


def _fail_unknown_write(plan_artifact, console: Console, secret_name: str, label: str) -> None:
    audited = _try_audit(
        action=plan_artifact.action,
        secret_name=secret_name,
        env=plan_artifact.env,
        status="outcome_unknown",
        plan_id=plan_artifact.plan_id,
    )
    status_saved = _try_save_status(plan_artifact, "failed")
    console.print(f"[red]FAIL[/red] {shown(label)} write outcome unknown: {shown(secret_name)}")
    console.print(f"the write was not confirmed; check {shown(secret_name)} before any retry")
    console.print("no automatic retry was attempted")
    _warn_unrecorded(console, audited=audited, status_saved=status_saved)
    console.print("secret value was not printed")
    raise typer.Exit(EXIT_OUTCOME_UNKNOWN)


def _warn_unrecorded(console: Console, *, audited: bool, status_saved: bool) -> None:
    if not audited:
        _warn_audit_incomplete(console)
    if not status_saved:
        _warn_status_not_saved(console)


def _finish_confirmed_write(
    plan_artifact,
    console: Console,
    ok_lines: list[str],
    *,
    secret_name: str,
    env: str,
    not_printed: str,
    source_env: str | None = None,
) -> None:
    """Record a fully confirmed vault write, as the GitHub path does: the
    success event first, then the status; `executed` only when the event
    was written, otherwise `failed`. Neither failure is raised: the results
    are shown with a warning and the exit code is 1."""
    audited = _try_audit(
        action=plan_artifact.action,
        secret_name=secret_name,
        env=env,
        status="success",
        plan_id=plan_artifact.plan_id,
        details={"source_env": source_env} if source_env else {},
    )
    status_saved = _try_save_status(plan_artifact, "executed" if audited else "failed")
    for line in ok_lines:
        console.print(shown(line), soft_wrap=True)
    _warn_unrecorded(console, audited=audited, status_saved=status_saved)
    if not audited and status_saved:
        console.print(
            "the writes above were made; the plan is marked failed only because the audit "
            "record is incomplete; do not plan the same names again"
        )
    console.print(shown(not_printed))
    if not (audited and status_saved):
        raise typer.Exit(EXIT_FAILED)


def _write_secrets_in_order(
    plan_artifact,
    console: Console,
    vault: Vault,
    writes,
    *,
    env: str,
    label: str,
    start_details: dict | None = None,
) -> None:
    """Write (name, read_value) pairs in order. Stop at the first write that is
    not confirmed and report which names were written; never roll back or retry."""
    names = [name for name, _ in writes]
    start_details = start_details or {}
    _record_write_start(
        plan_artifact,
        secret_name=names[0] if len(names) == 1 else "multiple",
        env=env,
        details={**start_details, "secret_names": names},
    )
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
    source = _audit_source(plan_artifact)
    audited = _try_audit(
        action=plan_artifact.action,
        secret_name=name,
        env=env,
        status="outcome_unknown" if unknown else "failure",
        plan_id=plan_artifact.plan_id,
        details={
            **source,
            "confirmed_names": names[:index],
            "not_written_names": not_written,
        },
    )
    status_saved = _try_save_status(plan_artifact, "failed")
    if unknown:
        console.print(f"[red]FAIL[/red] {shown(label)} write outcome unknown: {shown(name)}")
        console.print(f"the write was not confirmed; check {shown(name)} before any retry")
    else:
        console.print(f"[red]FAIL[/red] {shown(label)} was not written: {shown(name)}")
        console.print(shown(reason))
    console.print(f"written and confirmed: {shown(_format_names(names[:index]))}", soft_wrap=True)
    console.print(f"not written: {shown(_format_names(not_written))}", soft_wrap=True)
    console.print("no automatic rollback or retry was attempted")
    _warn_unrecorded(console, audited=audited, status_saved=status_saved)
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
    console.print(f"[red]FAIL[/red] {shown(error)}", soft_wrap=True)
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
        console.print(f"{shown(key)}: {shown(value)}")

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
                console.print(f"expected_secret: {shown(mapping['name'])}")
            else:
                console.print(f"secret_mapping: {shown(mapping['source'])} -> {shown(mapping['target'])}")
    if plan_artifact.generator:
        for key in sorted(plan_artifact.generator):
            console.print(f"generator_{shown(key)}: {shown(plan_artifact.generator[key])}")

    if plan_artifact.action == "vault.secret.generate":
        console.print(f"create_mode: {shown(plan_artifact.create_mode or 'create_only')}")

    if plan_artifact.action == "vault.secret.rotate":
        console.print(f"rotation_mode: {shown(plan_artifact.rotation_mode or 'replace_existing')}")

    if plan_artifact.action == "vault.secret.import_file":
        console.print(f"source_file: {shown(plan_artifact.source_file)}")
        console.print(f"create_mode: {shown(plan_artifact.create_mode or 'create_only')}")
    if plan_artifact.action == "vault.secret.copy_many":
        console.print(f"source_env: {shown(plan_artifact.source_env or plan_artifact.env)}")
        console.print(f"target_env: {shown(plan_artifact.target_env)}")
        console.print(f"create_mode: {shown(plan_artifact.create_mode or 'create_only')}")
    if plan_artifact.action == "vault.secret.diff_envs":
        console.print(f"source_env: {shown(plan_artifact.source_env or plan_artifact.env)}")
        console.print(f"target_env: {shown(plan_artifact.target_env)}")
    if plan_artifact.action in HUMAN_ENTRY_ACTIONS:
        console.print(f"create_mode: {shown(plan_artifact.create_mode)}")
        console.print(f"value_source: {shown(plan_artifact.value_source)}")
    console.print(f"secret_value_included: {shown(plan_artifact.secret_value_included)}")
    if legacy:
        console.print("[red]plan was created before destination binding; replan required[/red]")
        return
    console.print(f"contract_version: {shown(plan_artifact.contract_version)}")
    if plan_artifact.vault_destination:
        for key in ("backend", "domain", "project_id", "path"):
            console.print(f"vault_{shown(key)}: {shown(plan_artifact.vault_destination[key])}")
    console.print(f"plan_digest: {shown(plan_digest(plan_artifact.loaded_content))}", soft_wrap=True)
    if plan_artifact.approval_digest is not None:
        console.print(f"approval_digest: {shown(plan_artifact.approval_digest)}", soft_wrap=True)
    for line in _plan_time_lines(plan_artifact):
        console.print(shown(line))


def _plan_time_lines(plan_artifact) -> list[str]:
    lines = [f"planned_at: {plan_artifact.planned_at or 'not recorded (older version)'}"]
    if plan_artifact.approved_at is not None:
        lines.append(f"approved_at: {plan_artifact.approved_at}")
    elif plan_artifact.status == "approved":
        lines.append("approved_at: not recorded (older version)")
    if plan_artifact.status == "approved" and plan_artifact.action not in HUMAN_ENTRY_ACTIONS:
        hours = load_policy().approval_valid_hours
        refusal = approval_expiry_refusal(plan_artifact, hours, utc_now())
        if plan_artifact.approved_at is not None:
            expires = _approval_expires_at(plan_artifact.approved_at, hours)
            lines.append(f"approval_expires_at: {format_timestamp(expires)}")
        lines.append(f"approval_valid: {'no; ' + refusal if refusal else 'yes'}")
    return lines


# pending-plans: the fixed broker prefix of the local broker, as in the docs.
HUMAN_BROKER_COMMAND = "sudo -u secretops-broker /usr/local/bin/secretops-broker"
PENDING_PLANS_SHOWN = 50
PENDING_NAMES_SHOWN = 10
# Layout for an 80-column terminal: every line with plan-derived text holds
# one free-text field, at the end, after a fixed label, and is at most
# PENDING_LINE_CHARS long, so no field can start a terminal row of its own.
PENDING_LINE_CHARS = 79
PENDING_CUT_MARK = "..."
# Only this much of a field is examined, so a huge field costs no more.
PENDING_FIELD_SCAN_CHARS = 4096


def _human_action_needed(plan_artifact) -> str | None:
    """What a planned plan waits for from the human, or None."""
    if plan_artifact.action in HUMAN_ENTRY_ACTIONS:
        return "your entry"
    if _needs_approval(plan_artifact):
        return "approval"
    return None


def _age_words(planned_at: str, now: datetime) -> str:
    seconds = int((now - parse_timestamp(planned_at)).total_seconds())
    if seconds < 0:
        return "in the future; check the clock"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def _field(value, limit: int) -> str:
    """One plan field for the listing: printable ASCII on one line, at most
    `limit` characters. Every plan-derived value goes through this, so no
    field can add lines, control sequences, wide or combining characters,
    padding that wraps into a fake line, or unbounded output."""
    raw = str(value)
    cut = len(raw) > PENDING_FIELD_SCAN_CHARS
    text = safe_text(raw[:PENDING_FIELD_SCAN_CHARS])
    text = " ".join("".join(c if c.isascii() else "?" for c in text).split())
    if cut or len(text) > limit:
        return text[: limit - len(PENDING_CUT_MARK)] + PENDING_CUT_MARK
    return text


def _labelled(label: str, value) -> str:
    """A fixed label followed by one field, the whole line within budget."""
    return label + _field(value, PENDING_LINE_CHARS - len(label))


def _pending_name_lines(plan_artifact) -> list[str]:
    pairs = []
    for mapping in plan_artifact.secret_mappings or []:
        if "name" in mapping:
            pairs.append((mapping["name"], None))
        else:
            pairs.append((mapping["source"], mapping["target"]))
    if not pairs and plan_artifact.secret_name is not None:
        pairs.append(
            (plan_artifact.secret_name, (plan_artifact.target or {}).get("target_secret_name"))
        )
    label = "   name: "
    # A renamed pair holds two fields; each gets half of what is left.
    side = (PENDING_LINE_CHARS - len(label) - len(" -> ")) // 2
    lines = []
    for source, target in pairs[:PENDING_NAMES_SHOWN]:
        if target is None or target == source:
            lines.append(_labelled(label, source))
        else:
            lines.append(f"{label}{_field(source, side)} -> {_field(target, side)}")
    if len(pairs) > PENDING_NAMES_SHOWN:
        hidden = len(pairs) - PENDING_NAMES_SHOWN
        lines.append(f"   and {hidden} more name{'' if hidden == 1 else 's'}")
    return lines


def _pending_where_lines(plan_artifact) -> list[str]:
    # Environments and GitHub target only: the vault URL, project and folder
    # stay in show-plan.
    target = plan_artifact.target
    if target is not None:
        lines = []
        if plan_artifact.action in VAULT_ACTIONS:
            lines.append(_labelled("   from: vault ", plan_artifact.env))
        repository = (
            f"{target['target_host']}/{target['target_owner']}/{target['target_repo']}"
        )
        lines.append(_labelled("   to: ", repository))
        lines.append(_labelled("   github environment: ", target["target_environment"]))
        return lines
    if plan_artifact.target_env is not None:
        return [
            _labelled("   from: vault ", plan_artifact.source_env or plan_artifact.env),
            _labelled("   to: vault ", plan_artifact.target_env),
        ]
    return [_labelled("   in: vault ", plan_artifact.env)]


def _environment_policy_refuses(plan_artifact) -> bool:
    # The same check execute-plan applies before any provider call.
    try:
        for environment in _execution_policy_environments(plan_artifact):
            require_environment_allowed(plan_artifact.action, environment)
    except PermissionError:
        return True
    return False


def pending_plan_lines(plans, unreadable: int, now: datetime) -> list[str]:
    """Plain lines listing planned plans that wait for the human.

    Every plan field is agent-prepared text and passes through _field.
    """
    waiting = [
        (plan, needed)
        for plan in plans
        if plan.status == "planned" and (needed := _human_action_needed(plan)) is not None
    ]
    # Plans without a planning time (older versions) first, then oldest to
    # newest, so the newest is closest to the prompt.
    waiting.sort(
        key=lambda item: (item[0].planned_at is not None, item[0].planned_at or "", item[0].plan_id)
    )
    lines = []
    if not waiting:
        lines.append("No plans wait for you.")
    else:
        lines.append(f"Plans waiting for you: {len(waiting)}")
        hidden = len(waiting) - PENDING_PLANS_SHOWN
        if hidden > 0:
            lines.append(
                f"{hidden} older plan{'' if hidden == 1 else 's'} not shown; "
                f"the {PENDING_PLANS_SHOWN} newest follow."
            )
            waiting = waiting[hidden:]
    for number, (plan, needed) in enumerate(waiting, start=1):
        # The plan id and action of a loaded plan are pattern-checked and
        # from a fixed set (load_plan), so the header, action and command
        # lines hold no free text; _field still bounds them for plans that
        # did not come from the loader. Command lines may exceed the line
        # budget: their only plan-derived part is the plan id.
        plan_id = _field(plan.plan_id, 64)
        lines.append("")
        lines.append(f"{number}. {plan_id}")
        if plan.planned_at is None:
            lines.append("   planned: not recorded (older version)")
        else:
            planned_at = _field(plan.planned_at, 20)
            lines.append(f"   planned: {planned_at} ({_age_words(plan.planned_at, now)})")
        lines.append(f"   action: {_field(plan.action, 40)}   needs: {needed}")
        lines.extend(_pending_where_lines(plan))
        lines.extend(_pending_name_lines(plan))
        if _environment_policy_refuses(plan):
            lines.append("   note: the policy denies this environment; approval will not make it run")
        if needed == "approval":
            lines.append(f"   review:  {HUMAN_BROKER_COMMAND} show-plan --id {plan_id}")
            lines.append(f"   approve: {HUMAN_BROKER_COMMAND} approve-plan --id {plan_id}")
        else:
            lines.append(f"   enter:   secretops add --plan {plan_id}")
    if unreadable:
        lines.append("")
        if unreadable == 1:
            lines.append(
                "1 plan file could not be read; inspect it with show-plan or remove it."
            )
        else:
            lines.append(
                f"{unreadable} plan files could not be read; "
                "inspect them with show-plan or remove them."
            )
    return lines


def _approval_expires_at(approved_at: str, hours: int) -> datetime:
    return parse_timestamp(approved_at) + timedelta(hours=hours)


def approval_expiry_refusal(plan_artifact, hours: int, now: datetime) -> str | None:
    """Why an approved plan may no longer execute, or None.

    Fails closed: no recorded approval time (approved by an older version),
    an approval time in the future (the clock moved back), and the moment
    the period ends all count as expired.
    """
    if plan_artifact.status != "approved":
        return None
    if plan_artifact.approved_at is None:
        return "plan was approved by an older secretops version without an approval time"
    approved = parse_timestamp(plan_artifact.approved_at)
    if approved > now:
        return "approval time is in the future (the clock may have changed)"
    if now >= approved + timedelta(hours=hours):
        return f"approval expired: approved more than {hours} hours ago"
    return None


def transition_planned_plan(
    plan_id: str,
    status: str,
    label: str,
    console: Console,
) -> None:
    command = "approve-plan" if status == "approved" else "reject-plan"
    # Loaded first so a missing or invalid plan fails as before, without a
    # lock file.
    plan_artifact = _load_plan_or_exit(plan_id, command, console)
    if status == "approved" and plan_artifact.action in HUMAN_ENTRY_ACTIONS:
        # Always refused: no lock, so this command cannot make a human
        # submission find the plan busy.
        _refuse_human_entry_approval(plan_artifact, console)
    if not PLAN_ID_PATTERN.fullmatch(plan_id):
        _fail_plan_file(plan_id, command, console, PlanFileError("not_found"))
    # Decisions, submission and execution exclude each other through the plan
    # lock; the plan is read again under the lock and the decision is made
    # and written only from that read, so a finished run is seen.
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(plan_lock(plan_id))
        except (PlanBusyError, PlanLockError) as error:
            message = f"{error}; try again later" if isinstance(error, PlanBusyError) else str(error)
            console.print(f"[red]FAIL[/red] {shown(message)}")
            raise typer.Exit(1) from error
        plan_artifact = _load_plan_or_exit(plan_id, command, console)
        if status == "approved" and plan_artifact.action in HUMAN_ENTRY_ACTIONS:
            _refuse_human_entry_approval(plan_artifact, console)
        _transition_loaded_plan(plan_artifact, status, label, console)


def _refuse_human_entry_approval(plan_artifact, console: Console) -> None:
    # Approval of a human-entry plan is recorded only together with the
    # value, by the human submission endpoint.
    console.print(f"[red]FAIL[/red] {shown(plan_artifact.action)} is approved while entering the value")
    console.print(f"use: secretops add --plan {shown(plan_artifact.plan_id)}")
    raise typer.Exit(1)


def _transition_loaded_plan(plan_artifact, status: str, label: str, console: Console) -> None:
    plan_id = plan_artifact.plan_id
    if plan_artifact.status != "planned":
        console.print(
            f"[red]FAIL[/red] plan status is {shown(plan_artifact.status)}; expected planned"
        )
        raise typer.Exit(1)

    # Status and digest are written from the content that was just checked;
    # the file is not read again in between.
    digest = plan_digest(plan_artifact.loaded_content) if status == "approved" else None
    approved_at = format_timestamp(utc_now()) if status == "approved" else None
    try:
        set_plan_status(plan_artifact, status, approval_digest=digest, approved_at=approved_at)
    except OSError as error:
        # The plan file is replaced atomically, so a failed save leaves the
        # old file: the plan is still planned and nothing was recorded.
        console.print("[red]FAIL[/red] the decision could not be saved")
        console.print("the plan is unchanged and still planned; no audit record was written")
        console.print("ask the operator to check the plan directory")
        raise typer.Exit(1) from error
    # Recorded after the status is saved, still under the plan lock, so the
    # audit log holds only decisions that took effect. Same step and outcome
    # names as the approval recorded by `secretops add`.
    source = _audit_source(plan_artifact)
    audit_written = _try_audit(
        action=plan_artifact.action,
        secret_name=_secret_name_for_policy_audit(plan_artifact),
        env=_audit_env(plan_artifact),
        status="success",
        plan_id=plan_id,
        details={
            **source,
            "step": "approve" if status == "approved" else "reject",
            "outcome": status,
            **({"approval_digest": digest} if digest is not None else {}),
        },
    )
    console.print(f"OK plan {shown(label)}: {shown(plan_id)}")
    if digest is not None:
        console.print(f"approval_digest: {shown(digest)}", soft_wrap=True)
        hours = load_policy().approval_valid_hours
        console.print(f"approved_at: {shown(approved_at)}")
        console.print(
            f"approval_expires_at: {shown(format_timestamp(_approval_expires_at(approved_at, hours)))}"
        )
    if not audit_written:
        console.print("[red]WARNING[/red] the audit record could not be written")
        console.print(f"the decision was saved and stands; the plan is {shown(status)}")
        console.print("ask the operator to check the audit log")
        raise typer.Exit(EXIT_FAILED)


def preflight_saved_plan(plan_id: str, console: Console) -> None:
    plan_artifact = _load_plan_or_exit(plan_id, "preflight-plan", console)
    # Preflight must not mutate plan status.
    _require_plan_binding(plan_artifact, console, mark_failed=False)
    _preflight_approval_validity(plan_artifact, console)
    try:
        _preflight_plan(plan_artifact, console)
    except VaultError as error:
        # Preflight must not mutate plan status.
        _fail_vault_error(plan_artifact, console, error, mark_failed=False)


def _preflight_approval_validity(plan_artifact, console: Console) -> None:
    """Fail preflight for an approved plan that execute-plan would refuse as
    expired. A plan that is not approved yet passes as before."""
    if plan_artifact.action in HUMAN_ENTRY_ACTIONS:
        return
    refusal = approval_expiry_refusal(
        plan_artifact, load_policy().approval_valid_hours, utc_now()
    )
    if refusal is None:
        return
    source = _audit_source(plan_artifact)
    write_audit_event(
        action=plan_artifact.action,
        secret_name=_secret_name_for_policy_audit(plan_artifact),
        env=_audit_env(plan_artifact),
        status="failure",
        plan_id=plan_artifact.plan_id,
        details={**source, "failure": "approval_expired", "step": "preflight"},
    )
    console.print(f"[red]FAIL[/red] preflight failed: {shown(plan_artifact.action)}")
    console.print(f"approval_valid: no; {shown(refusal)}")
    console.print("no provider call was made")
    console.print("save a new plan and ask the human to review and approve it")
    console.print("secret value was not printed")
    raise typer.Exit(1)


def _preflight_plan(plan_artifact, console: Console) -> None:
    vault = _bound_vault(plan_artifact)

    if plan_artifact.action in {"vault.read.check", "github.env_secret.write"}:
        source_secret = plan_artifact.secret_name
        if source_secret is None:
            console.print(f"[red]unsupported plan action: {shown(plan_artifact.action)}[/red]")
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
            console.print(f"OK preflight passed: {shown(plan_artifact.action)}")
            console.print(f"environment: {shown(plan_artifact.env)}")
            console.print(f"source secret exists: {shown(source_secret)}")
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
        console.print(f"[red]FAIL[/red] preflight failed: {shown(plan_artifact.action)}")
        console.print(f"environment: {shown(plan_artifact.env)}")
        console.print(f"missing source secret: {shown(source_secret)}")
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
        console.print(f"target_owner: {shown(target.get('target_owner'))}")
        console.print(f"target_repo: {shown(target.get('target_repo'))}")
        console.print(f"target_environment: {shown(target.get('target_environment'))}")
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
            console.print(f"[red]FAIL[/red] preflight failed: {shown(plan_artifact.action)}")
        else:
            console.print(f"OK preflight passed: {shown(plan_artifact.action)}")
        console.print(f"environment: {shown(plan_artifact.env)}")
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
        console.print(f"source_env: {shown(source_env)}")
        console.print(f"target_env: {shown(plan_artifact.target_env or '')}")
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
            env=target_env,
            status="failure" if failed else "success",
            plan_id=plan_artifact.plan_id,
            details={"source_env": source_env},
        )
        if failed:
            console.print("[red]FAIL[/red] preflight failed: vault.secret.copy_many")
        else:
            console.print("OK preflight passed: vault.secret.copy_many")
        console.print(f"source_env: {shown(source_env)}")
        console.print(f"target_env: {shown(target_env)}")
        console.print(f"create_mode: {shown(create_mode)}")
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
        console.print(f"environment: {shown(plan_artifact.env)}")
        console.print(f"target secret: {shown(target_secret)}")
        console.print(f"create_mode: {shown(create_mode)}")
        console.print(f"target secret exists: {shown(target_exists)}")
        if "type" in generator:
            console.print(f"generator type: {shown(generator['type'])}")
        if "length" in generator:
            console.print(f"generator length: {shown(generator['length'])}")
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
            console.print(f"[red]FAIL[/red] preflight failed: {shown(plan_artifact.action)}")
        else:
            console.print(f"OK preflight passed: {shown(plan_artifact.action)}")
        console.print(f"environment: {shown(plan_artifact.env)}")
        console.print(f"target secret: {shown(target_secret)}")
        console.print(f"create_mode: {shown(plan_artifact.create_mode)}")
        console.print(f"target secret exists: {shown(target_exists)}")
        if target_exists:
            console.print("the secret would not be created")
        else:
            console.print(f"a human can enter the value with: secretops add --plan {shown(plan_artifact.plan_id)}")
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
        console.print(f"environment: {shown(plan_artifact.env)}")
        console.print(f"target secret: {shown(target_secret)}")
        console.print(f"rotation_mode: {shown(rotation_mode)}")
        console.print(f"target secret exists: {shown(target_exists)}")
        if "type" in generator:
            console.print(f"generator type: {shown(generator['type'])}")
        if "length" in generator:
            console.print(f"generator length: {shown(generator['length'])}")
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
        console.print(f"environment: {shown(plan_artifact.env)}")
        console.print(f"source_file: {shown(source_file)}")
        console.print(f"source file exists: {shown(source_exists)}")
        console.print(f"source file is file: {shown(source_is_file)}")
        console.print(f"source file permissions restricted: {shown(permissions_restricted)}")
        console.print(f"create_mode: {shown(create_mode)}")
        console.print(f"secret count: {len(import_names)}")
        console.print(f"secret names: {shown(_format_names(import_names))}", soft_wrap=True)
        console.print(
            "existing target secrets: "
            + (", ".join(existing_targets) if existing_targets else "none")
        )
        if parse_error:
            console.print(shown(parse_error))
        if not import_names:
            console.print(f"missing required secret name for {shown(plan_artifact.action)}")
        if not planned_names:
            console.print("saved plan has no imported secret names")
        if names_changed:
            console.print("source file names do not match saved plan")
        if create_mode != "create_only":
            console.print(f"unsupported create_mode: {shown(create_mode)}")
        if existing_targets:
            console.print("execution would not overwrite existing secrets")
        elif not failed:
            console.print("import can proceed later")
        console.print("secret values were not printed")
        if failed:
            raise typer.Exit(1)
        return

    console.print(f"[red]unsupported plan action: {shown(plan_artifact.action)}[/red]")
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


def _not_printed_lines(action: str) -> list[str]:
    lines = []
    if action in {"vault.secret.generate", "vault.secret.rotate"}:
        lines.append("secret value was not generated")
    if action == "vault.secret.import_file":
        lines.append("source file contents were not read")
    return [*lines, "secret value was not printed"]


def _refuse_unapproved(plan_artifact, console: Console, *, secret_name: str | None = None) -> None:
    """Refuse a plan that needs approval and is not approved.

    A `planned` plan still waits for the human: it is left unchanged, so the
    same plan can be approved and executed later. Any other status (only
    `failed` reaches this point) is refused as before and stays `failed`.
    """
    pending = plan_artifact.status == "planned"
    source = _audit_source(plan_artifact)
    write_audit_event(
        action=plan_artifact.action,
        secret_name=secret_name or _secret_name_for_policy_audit(plan_artifact),
        env=_audit_env(plan_artifact),
        status="failure",
        plan_id=plan_artifact.plan_id,
        details={**source, **({"failure": "approval_pending"} if pending else {})},
    )
    if not pending:
        set_plan_status(plan_artifact, "failed")
    console.print(f"[red]FAIL[/red] {shown(_approval_required_message(plan_artifact.action))}")
    if pending:
        console.print("the plan is not approved yet; it stays planned and nothing was done")
        console.print("no provider call was made")
        console.print("wait until the human has approved it; do not retry to check")
    for line in _not_printed_lines(plan_artifact.action):
        console.print(shown(line))
    raise typer.Exit(1)


# Write actions that need an approved plan even when a custom policy sets
# requires_approval: false; each also checks this again on its own path.
APPROVAL_ALWAYS_REQUIRED = frozenset(
    {
        "vault.secret.generate",
        "vault.secret.rotate",
        "vault.secret.import_file",
        "vault.secret.copy_many",
        "github.env_secret.write",
        "github.env_secret.sync_many",
    }
)


def _needs_approval(plan_artifact) -> bool:
    return (
        plan_artifact.action in APPROVAL_ALWAYS_REQUIRED
        or requires_approval(plan_artifact.action)
    )


def _require_environment_policy(plan_artifact, console: Console) -> None:
    # A plan the environment policy refuses can never run: it is marked
    # failed, also while it still waits for approval.
    try:
        for environment in _execution_policy_environments(plan_artifact):
            require_environment_allowed(plan_artifact.action, environment)
    except PermissionError as error:
        source = _audit_source(plan_artifact)
        write_audit_event(
            action=plan_artifact.action,
            secret_name=_secret_name_for_policy_audit(plan_artifact),
            env=_audit_env(plan_artifact),
            status="failure",
            plan_id=plan_artifact.plan_id,
            details={**source},
        )
        set_plan_status(plan_artifact, "failed")
        console.print(f"[red]FAIL[/red] {shown(error)}", soft_wrap=True)
        for line in _not_printed_lines(plan_artifact.action):
            console.print(shown(line))
        raise typer.Exit(1) from error


def _refuse_expired_approval(plan_artifact, console: Console) -> None:
    """Refuse an approved plan whose approval is no longer valid, before any
    provider call. The plan is marked failed: approve-plan accepts only
    planned plans, so the way on is a new plan and a new approval."""
    refusal = approval_expiry_refusal(
        plan_artifact, load_policy().approval_valid_hours, utc_now()
    )
    if refusal is None:
        return
    source = _audit_source(plan_artifact)
    write_audit_event(
        action=plan_artifact.action,
        secret_name=_secret_name_for_policy_audit(plan_artifact),
        env=_audit_env(plan_artifact),
        status="failure",
        plan_id=plan_artifact.plan_id,
        details={**source, "failure": "approval_expired", "step": "execute"},
    )
    set_plan_status(plan_artifact, "failed")
    console.print(f"[red]FAIL[/red] {shown(refusal)}")
    console.print("no provider call was made; the plan is now failed")
    console.print("save a new plan and ask the human to review and approve it")
    for line in _not_printed_lines(plan_artifact.action):
        console.print(shown(line))
    raise typer.Exit(1)


def execute_saved_plan(plan_id: str, console: Console) -> None:
    # Loaded first so a missing or invalid plan fails as before, without a
    # lock file.
    plan_artifact = _load_plan_or_exit(plan_id, "execute-plan", console)
    if plan_artifact.action in HUMAN_ENTRY_ACTIONS:
        # Always refused, and the action of a plan never changes: no lock, so
        # this command cannot make a human submission find the plan busy.
        _execute_loaded_plan(plan_artifact, console)
        return
    if not PLAN_ID_PATTERN.fullmatch(plan_id):
        _fail_plan_file(plan_id, "execute-plan", console, PlanFileError("not_found"))
    # Concurrent executions exclude each other through the plan lock; the
    # plan is read again under the lock so a finished run is seen.
    try:
        with plan_lock(plan_id):
            _execute_loaded_plan(_load_plan_or_exit(plan_id, "execute-plan", console), console)
    except (PlanBusyError, PlanLockError) as error:
        # Raised only while taking the lock, before anything else ran.
        message = f"{error}; try again later" if isinstance(error, PlanBusyError) else str(error)
        console.print(f"[red]FAIL[/red] {shown(message)}")
        console.print("no provider call was made")
        raise typer.Exit(1) from error


def _execute_loaded_plan(plan_artifact, console: Console) -> None:
    # Checks and the operation use this one loaded object.
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
        console.print(f"[red]FAIL[/red] {shown(plan_artifact.action)} requires human completion")
        console.print(f"a human runs: secretops add --plan {shown(plan_artifact.plan_id)}")
        console.print("no provider call was made")
        console.print("secret value was not printed")
        raise typer.Exit(1)

    # Order of checks, all before any provider call:
    # 1. environment policy (refused: marked failed);
    # 2. approval (a planned plan stays planned; a failed one stays failed),
    #    before the binding check, so a vault error or a changed destination
    #    cannot fail a plan that still waits for the human;
    # 3. binding: approval digest and vault destination (marked failed),
    #    repeated even after a passing preflight, as configuration can change;
    # 4. approval validity (expired: marked failed).
    _require_environment_policy(plan_artifact, console)
    if _needs_approval(plan_artifact) and plan_artifact.status != "approved":
        _refuse_unapproved(plan_artifact, console)
    _require_plan_binding(plan_artifact, console, mark_failed=True)
    _refuse_expired_approval(plan_artifact, console)

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
        console.print(f"source_env: {shown(source_env)}")
        console.print(f"target_env: {shown(plan_artifact.target_env or '')}")
        _print_diff_envs_summary(console, result)
        if failed:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.secret.import_file":
        if plan_artifact.status != "approved":
            _refuse_unapproved(plan_artifact, console)
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
            console.print(f"environment: {shown(plan_artifact.env)}")
            console.print(f"source_file: {shown(source_file)}")
            console.print(f"secret count: {len(import_names)}")
            console.print(f"secret names: {shown(_format_names(import_names))}", soft_wrap=True)
            for line in failure_lines:
                console.print(shown(line))
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

        _finish_confirmed_write(
            plan_artifact,
            console,
            [
                f"OK imported dotenv secrets: {len(entries)}",
                f"environment: {plan_artifact.env}",
                f"secret names: {_format_names(import_names)}",
            ],
            secret_name=audit_secret_name,
            env=plan_artifact.env,
            not_printed="secret values were not printed",
        )
        return

    if plan_artifact.action == "vault.secret.rotate":
        target_secret = _required_secret_name_for_execution(plan_artifact, console)
        if plan_artifact.status != "approved":
            _refuse_unapproved(plan_artifact, console, secret_name=target_secret)

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
            console.print(f"[red]FAIL[/red] rotated secret target is missing: {shown(target_secret)}")
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

        _record_write_start(
            plan_artifact,
            secret_name=target_secret,
            env=plan_artifact.env,
            details={"secret_names": [target_secret]},
        )
        outcome = vault.write_secret(target_secret, plan_artifact.env, rotated_value)
        if outcome is WriteOutcome.CONFIRMED:
            _finish_confirmed_write(
                plan_artifact,
                console,
                [f"OK rotated secret stored: {target_secret}"],
                secret_name=target_secret,
                env=plan_artifact.env,
                not_printed="secret value was not printed",
            )
            return

        _fail_unknown_write(plan_artifact, console, target_secret, "rotated secret")


    if plan_artifact.action == "vault.secret.copy_many":
        if plan_artifact.status != "approved":
            _refuse_unapproved(plan_artifact, console)

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
                env=target_env,
                status="failure",
                plan_id=plan_artifact.plan_id,
                details={"source_env": source_env},
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
            start_details={
                "source_env": source_env,
                "source_secret_names": [mapping["source"] for mapping in mappings],
            },
        )

        _finish_confirmed_write(
            plan_artifact,
            console,
            [f"OK vault secrets copied: {len(mappings)}"],
            secret_name="multiple",
            env=target_env,
            not_printed="secret values were not printed",
            source_env=source_env,
        )
        return

    if plan_artifact.action == "vault.secret.generate":
        target_secret = _required_secret_name_for_execution(plan_artifact, console)
        if plan_artifact.status != "approved":
            _refuse_unapproved(plan_artifact, console, secret_name=target_secret)

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
            console.print(f"[red]FAIL[/red] unsupported create_mode: {shown(create_mode)}")
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
            console.print(f"[red]FAIL[/red] generated secret already exists: {shown(target_secret)}")
            console.print(f"create_mode: {shown(create_mode)}")
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

        _record_write_start(
            plan_artifact,
            secret_name=target_secret,
            env=plan_artifact.env,
            details={"secret_names": [target_secret]},
        )
        outcome = vault.write_secret(target_secret, plan_artifact.env, generated_value)
        if outcome is WriteOutcome.CONFIRMED:
            _finish_confirmed_write(
                plan_artifact,
                console,
                [f"OK generated secret stored: {plan_artifact.secret_name}"],
                secret_name=target_secret,
                env=plan_artifact.env,
                not_printed="secret value was not printed",
            )
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
            console.print(shown(str(error)))
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
        console.print(f"present names: {shown(_format_names(present_names))}", soft_wrap=True)
        console.print(f"missing names: {shown(_format_names(missing_names))}", soft_wrap=True)
        console.print("secret values were not printed")
        if failed:
            raise typer.Exit(1)
        return

    if plan_artifact.action == "vault.read.check":
        if plan_artifact.status not in {"planned", "approved"}:
            console.print(
                f"[red]FAIL[/red] vault read plan cannot execute with status: {shown(plan_artifact.status)}"
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
            console.print(f"OK secret exists: {shown(source_secret)}")
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
                f"[red]FAIL[/red] secret missing or inaccessible: {shown(source_secret)}"
            )
            raise typer.Exit(1)
    elif plan_artifact.action == "github.env_secret.sync_many":
        if plan_artifact.status != "approved":
            _refuse_unapproved(plan_artifact, console)

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
            _refuse_unapproved(plan_artifact, console)

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
        console.print(f"[red]unsupported plan action: {shown(plan_artifact.action)}[/red]")
        write_audit_event(
            action=plan_artifact.action,
            secret_name=plan_artifact.secret_name,
            env=plan_artifact.env,
            status="failure",
            plan_id=plan_artifact.plan_id,
        )
        set_plan_status(plan_artifact, "failed")
        raise typer.Exit(1)


