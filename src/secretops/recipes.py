from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from secretops.policy import require_allowed
from secretops.validation import (
    validate_github_host,
    validate_github_target_metadata,
    validate_optional_secret_name,
    validate_secret_mappings,
    validate_secret_name,
)


@dataclass(frozen=True)
class Recipe:
    action: str
    secret_name: str | None
    env: str
    source_env: str | None = None
    target_env: str | None = None
    github_host: str | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    github_environment: str | None = None
    github_secret_name: str | None = None
    secrets: list[dict[str, str]] | None = None
    generator: dict[str, Any] | None = None
    create_mode: str = "create_only"
    rotation_mode: str | None = None
    source_file: str | None = None
    value_source: str | None = None

    def require_allowed(self) -> None:
        require_allowed(self.action)


def _load_secret_mappings(data: dict[str, Any]) -> list[dict[str, str]] | None:
    raw_mappings = data.get("secrets")
    if raw_mappings is None:
        return None
    mappings: list[dict[str, str]] = []
    for mapping in raw_mappings:
        if "name" in mapping:
            mappings.append({"name": validate_secret_name(mapping.get("name"))})
            continue
        mappings.append(
            {
                "source": validate_secret_name(mapping.get("source")),
                "target": validate_secret_name(mapping.get("target")),
            }
        )
    return validate_secret_mappings(mappings)


def _load_value_source(data: dict[str, Any]) -> str | None:
    # Recipes describe where a value will come from, never the value itself.
    if "value_source" not in data:
        return None
    if data.get("action") != "vault.secret.add" or data["value_source"] != "human_prompt":
        raise ValueError("value_source is supported only as human_prompt for vault.secret.add")
    return "human_prompt"


def load_recipe(path: str | Path) -> Recipe:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    github_owner = data.get("github_owner")
    github_repo = data.get("github_repo")
    github_environment = data.get("github_environment")
    if github_owner is not None or github_repo is not None or github_environment is not None:
        github_owner, github_repo, github_environment = validate_github_target_metadata(
            owner=github_owner,
            repo=github_repo,
            environment=github_environment,
        )
    github_host = data.get("github_host")
    if github_host is not None:
        github_host = validate_github_host(github_host)
    return Recipe(
        action=str(data["action"]),
        secret_name=validate_optional_secret_name(data.get("secret_name")),
        env=str(data.get("env", data.get("source_env", github_environment or ""))),
        source_env=str(data["source_env"]) if "source_env" in data else None,
        target_env=str(data["target_env"]) if "target_env" in data else None,
        github_host=github_host,
        github_owner=github_owner,
        github_repo=github_repo,
        github_environment=github_environment,
        github_secret_name=validate_optional_secret_name(data.get("github_secret_name")),
        secrets=_load_secret_mappings(data),
        generator=data.get("generator"),
        create_mode=str(data.get("create_mode", "create_only")),
        rotation_mode=data.get("rotation_mode"),
        source_file=data.get("source_file"),
        value_source=_load_value_source(data),
    )


