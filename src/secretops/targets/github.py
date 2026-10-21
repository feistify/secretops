from dataclasses import dataclass
from typing import Any

from secretops.targets.base import Target
from secretops.validation import (
    DEFAULT_GITHUB_HOST,
    validate_github_host,
    validate_github_target_metadata,
    validate_secret_name,
)


@dataclass(frozen=True)
class GitHubEnvSecretTarget(Target):
    owner: str
    repo: str
    environment: str
    secret_name: str
    host: str = DEFAULT_GITHUB_HOST

    def __post_init__(self) -> None:
        validate_github_host(self.host)
        owner, repo, environment = validate_github_target_metadata(
            owner=self.owner,
            repo=self.repo,
            environment=self.environment,
        )
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "repo", repo)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "secret_name", validate_secret_name(self.secret_name))

    def describe_write_plan(self) -> dict[str, Any]:
        return {
            "target_host": self.host,
            "target_owner": self.owner,
            "target_repo": self.repo,
            "target_environment": self.environment,
            "target_secret_name": self.secret_name,
            "api_call_made": False,
            "secret_value_printed": False,
        }
