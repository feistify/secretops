import contextlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from enum import Enum

from secretops.validation import (
    DEFAULT_GITHUB_HOST,
    validate_github_host,
    validate_github_target_metadata,
    validate_secret_name,
)

# PATH for the gh process and for finding gh. Fixed so the caller's PATH
# cannot choose which gh, or which helpers of gh, run.
CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"
# The only inherited variables gh receives: where the broker user's stored
# login lives, and how to reach GitHub (TLS, proxy, locale). Tokens,
# GH_HOST, GH_REPO, and unrelated broker credentials are never passed.
# DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR locate a keyring that holds
# a stored login; they carry no credential themselves.
PASSTHROUGH_VARS = (
    "HOME",
    "XDG_CONFIG_HOME",
    "GH_CONFIG_DIR",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "LANG",
    "LC_ALL",
)
# Set by the broker so gh never prompts, checks for updates, or colors output.
FIXED_VARS = {
    "PATH": CHILD_PATH,
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
}


DEFAULT_TIMEOUT_SECONDS = 60
# After a timeout the process is killed; collecting what is left of its
# pipes is bounded too, so an inherited pipe cannot hold the broker.
CLEANUP_SECONDS = 5
# GitHub's own secret-name rule: letters, digits, and underscores, not
# starting with a digit. Wider than the project pattern, on purpose: other
# secrets in the environment may use it.
GITHUB_SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# One row of non-TTY `gh secret list --env` output: name, a tab, and the
# update time. gh v2.4.0 prints the date (YYYY-MM-DD); later versions print
# an RFC 3339 timestamp. Environment secrets have no visibility column.
LISTING_ROW = re.compile(
    r"(?P<name>[^\t]+)\t\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?"
)


class GitHubError(Exception):
    """A gh call failed. Messages are fixed text: no provider output,
    request input, or credentials."""


class GitHubConfigurationError(GitHubError):
    """The GitHub CLI cannot be used as configured; gh was not called."""


class GitHubLaunchError(GitHubError):
    """gh could not be started; nothing was sent by this call."""


class GitHubTimeoutError(GitHubError):
    """gh did not finish in time and was stopped."""


class GitHubProviderError(GitHubError):
    """gh ran but reported failure."""


class GitHubResponseError(GitHubError):
    """gh output did not have the expected structure."""


class GitHubInterruptedError(GitHubError):
    """The broker was interrupted (Ctrl+C) while gh ran; gh was stopped."""


class GitHubWriteOutcome(Enum):
    """Result of a write that was started.

    ACKNOWLEDGED: gh exited with status 0. This is not a read-back: GitHub
    secret values cannot be read. UNKNOWN: the write may have reached GitHub
    (nonzero exit, signal, timeout, or a failure after launch).
    """

    ACKNOWLEDGED = "acknowledged"
    UNKNOWN = "unknown"


def github_child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The complete environment for every gh call."""
    env = {name: environ[name] for name in PASSTHROUGH_VARS if environ.get(name)}
    env.update(FIXED_VARS)
    return env


def resolve_gh_binary(environ: Mapping[str, str]) -> str:
    # gh holds the broker's GitHub login, so it is never looked up through
    # the caller's PATH. SECRETOPS_GH_BIN belongs in broker configuration.
    configured = environ.get("SECRETOPS_GH_BIN", "")
    if configured:
        if not (
            os.path.isabs(configured)
            and os.path.isfile(configured)
            and os.access(configured, os.X_OK)
        ):
            raise GitHubConfigurationError(
                "SECRETOPS_GH_BIN must be an absolute path to an executable file"
            )
        return configured
    binary = shutil.which("gh", path=CHILD_PATH)
    if not binary:
        raise GitHubConfigurationError(
            f"gh CLI was not found in {CHILD_PATH}; set SECRETOPS_GH_BIN"
        )
    return binary

def provider_timeout(environ: Mapping[str, str]) -> int:
    """SECRETOPS_PROVIDER_TIMEOUT_SECONDS, validated like the vault adapter's."""
    raw = environ.get("SECRETOPS_PROVIDER_TIMEOUT_SECONDS", "")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = int(raw)
    except ValueError:
        timeout = 0
    if timeout <= 0:
        raise GitHubConfigurationError(
            "SECRETOPS_PROVIDER_TIMEOUT_SECONDS must be a positive integer"
        )
    return timeout


class GitHubCliEnvSecretTarget:
    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        environment: str,
        host: str = DEFAULT_GITHUB_HOST,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        binary: str | None = None,
        environ: Mapping[str, str] | None = None,
    ):
        self.host = validate_github_host(host)
        self.owner, self.repo, self.environment = validate_github_target_metadata(
            owner=owner,
            repo=repo,
            environment=environment,
        )
        self.popen_factory = popen_factory
        self._environ = environ if environ is not None else os.environ
        # Resolved now, so a missing gh or a bad timeout stops the plan
        # before any secret is read.
        self.binary = binary or resolve_gh_binary(self._environ)
        self.timeout = provider_timeout(self._environ)

    def _repo_arg(self) -> str:
        # Host-qualified, so GH_HOST or the gh default host cannot redirect it.
        return f"{self.host}/{self.owner}/{self.repo}"

    def _env(self) -> dict[str, str]:
        return github_child_env(self._environ)

    def _start(self, command: list[str]) -> subprocess.Popen:
        """Launch gh. A failure here means gh never ran."""
        try:
            return self.popen_factory(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env(),
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise GitHubLaunchError("gh could not be started") from error

    def _communicate(self, process: subprocess.Popen, stdin: str | None) -> tuple[int, str]:
        """Send stdin and wait for gh within the timeout.

        Raises GitHubTimeoutError after stopping gh, or GitHubProviderError
        when communication fails. Output is returned for parsing only.
        """
        try:
            stdout, _ = process.communicate(input=stdin, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            # The exception may carry partial output; it is dropped here.
            self._stop(process)
            raise GitHubTimeoutError(f"gh did not finish within {self.timeout} seconds") from None
        except KeyboardInterrupt:
            # gh may already have sent the request: stop it and let the
            # caller report the outcome instead of leaving it running.
            self._stop(process)
            raise GitHubInterruptedError("interrupted while gh was running") from None
        except Exception:  # noqa: BLE001 - classified by the caller
            self._stop(process)
            raise GitHubProviderError("communication with gh failed") from None
        return process.returncode, stdout or ""

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        # Best-effort: kill, then collect within a bound. Partial output is
        # discarded.
        with contextlib.suppress(OSError):
            process.kill()
        try:
            process.communicate(timeout=CLEANUP_SECONDS)
        except Exception:  # noqa: BLE001 - fall back to closing the pipes
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            with contextlib.suppress(Exception):
                process.wait(timeout=CLEANUP_SECONDS)

    def set_secret(self, secret_name: str, secret_value: str) -> GitHubWriteOutcome:
        """Write one environment secret.

        Raises GitHubLaunchError when gh could not be started (nothing was
        sent). Once gh runs, anything but exit status 0 is UNKNOWN.
        """
        secret_name = validate_secret_name(secret_name)
        # Important:
        # Pass the secret value via stdin only. Never put it in command args,
        # and never print stdout/stderr returned by gh.
        process = self._start(
            [
                self.binary,
                "secret",
                "set",
                secret_name,
                "--repo",
                self._repo_arg(),
                "--env",
                self.environment,
            ]
        )
        try:
            returncode, _ = self._communicate(process, secret_value)
        except GitHubError:
            return GitHubWriteOutcome.UNKNOWN
        if returncode == 0:
            return GitHubWriteOutcome.ACKNOWLEDGED
        return GitHubWriteOutcome.UNKNOWN

    def list_secret_names(self) -> list[str]:
        """Return the names of the environment's secrets.

        Metadata only. Every failure raises GitHubError; an empty list is a
        valid answer. Every row must match LISTING_ROW and carry a valid
        GitHub secret name; names outside the project pattern are returned
        too, and callers compare only the names they expect.
        """
        process = self._start(
            [
                self.binary,
                "secret",
                "list",
                "--repo",
                self._repo_arg(),
                "--env",
                self.environment,
            ]
        )
        returncode, stdout = self._communicate(process, None)
        if returncode != 0:
            raise GitHubProviderError(f"gh secret list failed with exit status {returncode}")
        names: list[str] = []
        for line in stdout.splitlines():
            if not line:
                continue
            row = LISTING_ROW.fullmatch(line)
            if not row or not GITHUB_SECRET_NAME.fullmatch(row.group("name")):
                raise GitHubResponseError("unexpected gh secret list output")
            names.append(row.group("name"))
        return names
