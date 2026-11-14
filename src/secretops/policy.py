import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

POLICY_PATH_ENV = "SECRETOPS_POLICY_PATH"
DEFAULT_POLICY_RESOURCE_PACKAGE = "secretops.policies"
DEFAULT_POLICY_RESOURCE_NAME = "default.yaml"
DEFAULT_ALLOWED_ENVIRONMENTS = ["dev", "staging", "production"]


@dataclass(frozen=True)
class ActionPolicy:
    requires_approval: bool = True
    allowed_environments: list[str] = field(default_factory=list)
    production_allowed: bool = False


@dataclass(frozen=True)
class Policy:
    allowed_actions: list[str]
    denied_actions: list[str]
    actions: dict[str, ActionPolicy]


POLICY_KEYS = ("allowed_actions", "denied_actions", "actions")
ACTION_POLICY_KEYS = ("requires_approval", "allowed_environments", "production_allowed")
FIX_HINT = "correct or remove it; see docs/CONFIGURATION.md#policy-file"


class PolicyError(Exception):
    """The policy file cannot be used. The message names the file, a key
    and its location at most, never other file content."""


def _key_name(key) -> str:
    text = repr(str(key))
    return text if len(text) <= 80 else text[:77] + "..."


def _check_keys(mapping, allowed: tuple[str, ...], location: str, source: str) -> None:
    if not isinstance(mapping, dict):
        raise PolicyError(f"policy file {source}: {location} must be a mapping; {FIX_HINT}")
    for key in mapping:
        if key not in allowed:
            raise PolicyError(
                f"policy file {source}: unknown key {_key_name(key)} in {location} "
                f"(allowed: {', '.join(allowed)}); {FIX_HINT}"
            )


def validate_policy_data(data, source: str) -> None:
    """Refuse unknown keys at the top level and in each `actions` entry.

    Action names under `actions` are not checked: any name is accepted and
    an entry for an action that is not allowed has no effect.
    """
    _check_keys(data, POLICY_KEYS, "the top level", source)
    actions = data.get("actions")
    if actions is None:
        return
    if not isinstance(actions, dict):
        raise PolicyError(f"policy file {source}: actions must be a mapping; {FIX_HINT}")
    for action, metadata in actions.items():
        if metadata is not None:
            _check_keys(
                metadata, ACTION_POLICY_KEYS, f"actions entry {_key_name(action)}", source
            )


def _load_action_policies(data: dict) -> dict[str, ActionPolicy]:
    policies = {}
    for action, metadata in (data.get("actions") or {}).items():
        metadata = metadata or {}
        policies[str(action)] = ActionPolicy(
            requires_approval=bool(metadata.get("requires_approval", True)),
            allowed_environments=[
                str(environment)
                for environment in metadata.get(
                    "allowed_environments", []
                )
            ],
            production_allowed=bool(metadata.get("production_allowed", False)),
        )
    return policies


def _load_default_policy_text() -> str:
    return (
        resources.files(DEFAULT_POLICY_RESOURCE_PACKAGE)
        .joinpath(DEFAULT_POLICY_RESOURCE_NAME)
        .read_text(encoding="utf-8")
    )


def _load_policy_text(path: str | Path | None = None) -> tuple[str, str]:
    """Return the policy text and a name for it in messages."""
    explicit_path = path or os.environ.get(POLICY_PATH_ENV)
    if explicit_path:
        try:
            return Path(explicit_path).read_text(encoding="utf-8"), str(explicit_path)
        except (OSError, UnicodeDecodeError) as error:
            raise PolicyError(f"policy file {explicit_path} cannot be read") from error
    return _load_default_policy_text(), "(packaged default)"


def load_policy(path: str | Path | None = None) -> Policy:
    """Load and strictly validate the policy. Raises PolicyError."""
    text, source = _load_policy_text(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        # The parser message can quote file content; only the line is kept.
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise PolicyError(f"policy file {source} is not valid YAML{where}") from None
    data = {} if data is None else data
    validate_policy_data(data, source)
    return Policy(
        allowed_actions=list(data.get("allowed_actions", [])),
        denied_actions=list(data.get("denied_actions", [])),
        actions=_load_action_policies(data),
    )


def is_allowed(action: str, policy: Policy | None = None) -> bool:
    policy = policy or load_policy()
    return action in policy.allowed_actions and action not in policy.denied_actions


def require_allowed(action: str, policy: Policy | None = None) -> None:
    if not is_allowed(action, policy):
        raise PermissionError(f"Action denied by policy: {action}")


def get_action_policy(action: str, policy: Policy | None = None) -> ActionPolicy:
    policy = policy or load_policy()
    return policy.actions.get(action, ActionPolicy())


def requires_approval(action: str, policy: Policy | None = None) -> bool:
    return get_action_policy(action, policy).requires_approval


def is_production_allowed(action: str, policy: Policy | None = None) -> bool:
    return get_action_policy(action, policy).production_allowed


def require_environment_allowed(
    action: str,
    env: str,
    policy: Policy | None = None,
) -> None:
    policy = policy or load_policy()
    require_allowed(action, policy)
    action_policy = get_action_policy(action, policy)
    if env == "production" and not action_policy.production_allowed:
        raise PermissionError(f"production execution denied by policy: {action}")
    if env not in action_policy.allowed_environments:
        raise PermissionError(f"environment denied by policy for {action}: {env}")
