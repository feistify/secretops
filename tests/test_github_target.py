import inspect

from secretops.targets.github import GitHubEnvSecretTarget


DUMMY_SECRET_NAME = "DUMMY_SECRET"
DUMMY_SECRET_VALUE = "not-a-real-secret"


def test_github_env_secret_target_describes_safe_write_plan_metadata():
    target = GitHubEnvSecretTarget(
        owner="example-owner",
        repo="secretops",
        environment="dev",
        secret_name=DUMMY_SECRET_NAME,
    )

    plan = target.describe_write_plan()

    assert plan == {
        "target_host": "github.com",
        "target_owner": "example-owner",
        "target_repo": "secretops",
        "target_environment": "dev",
        "target_secret_name": DUMMY_SECRET_NAME,
        "api_call_made": False,
        "secret_value_printed": False,
    }
    assert DUMMY_SECRET_VALUE not in repr(plan)


def test_github_env_secret_target_does_not_accept_secret_values():
    signature = inspect.signature(GitHubEnvSecretTarget.describe_write_plan)

    assert "secret_value" not in signature.parameters
    assert "value" not in signature.parameters


def test_github_env_secret_target_plan_makes_no_real_github_api_call(monkeypatch):
    calls = []

    def fake_urlopen(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("real GitHub API must not be called")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    target = GitHubEnvSecretTarget(
        owner="example-owner",
        repo="secretops",
        environment="dev",
        secret_name=DUMMY_SECRET_NAME,
    )

    plan = target.describe_write_plan()

    assert plan["api_call_made"] is False
    assert calls == []


from fake_process import FakeFactory

from secretops.targets.github_cli import GitHubCliEnvSecretTarget, GitHubWriteOutcome


def test_github_cli_target_rejects_invalid_secret_name_before_subprocess():
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        raise AssertionError("gh must not be called for invalid secret names")

    target = GitHubCliEnvSecretTarget(
        owner="example-owner",
        repo="secretops",
        environment="dev",
        popen_factory=fake_run,
        binary="gh",
    )

    invalid_names = [
        "X') or __import__('os').system('echo pwned')#",
        "BAD-NAME",
        "bad_lowercase",
        "BAD NAME",
        "BAD/NAME",
        "BAD=NAME",
        "",
    ]
    for name in invalid_names:
        try:
            target.set_secret(name, DUMMY_SECRET_VALUE)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid secret name accepted: {name!r}")

    assert calls == []


def test_github_cli_target_accepts_dot_prefixed_repo_names():
    target = GitHubCliEnvSecretTarget(
        owner="example-owner",
        repo=".github",
        environment="dev",
        popen_factory=FakeFactory(),
        binary="gh",
    )

    assert target.repo == ".github"
    assert target.set_secret(DUMMY_SECRET_NAME, DUMMY_SECRET_VALUE) is GitHubWriteOutcome.ACKNOWLEDGED


def test_github_cli_target_rejects_flag_like_and_reserved_repo_names():
    def fake_run(command, **kwargs):
        raise AssertionError("gh must not be called for invalid target metadata")

    invalid_repos = ["--repo-flag", "-repo", ".", ".."]
    for repo in invalid_repos:
        try:
            GitHubCliEnvSecretTarget(
                owner="example-owner",
                repo=repo,
                environment="dev",
                popen_factory=fake_run,
                binary="gh",
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid repo accepted: {repo!r}")
