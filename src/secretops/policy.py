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


def _load_policy_text(path: str | Path | None = None) -> str:
    explicit_path = path or os.environ.get(POLICY_PATH_ENV)
    if explicit_path:
        return Path(explicit_path).read_text(encoding="utf-8")
    return _load_default_policy_text()


def load_policy(path: str | Path | None = None) -> Policy:
    data = yaml.safe_load(_load_policy_text(path)) or {}
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
