import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import yaml

from secretops.validation import validate_secret_name
from secretops.vaults.base import (
    Vault,
    VaultConfigurationError,
    VaultError,
    VaultProviderError,
    VaultResponseError,
    VaultTimeoutError,
    VaultUnsupportedValueError,
    WriteOutcome,
)

BACKEND_NAME = "infisical"
DEFAULT_DOMAIN = "https://eu.infisical.com"
DEFAULT_PORTS = {"http": 80, "https": 443}
DESTINATION_FIELDS = ("backend", "domain", "project_id", "path")
DEFAULT_TIMEOUT_SECONDS = 60
# PATH for the provider process. Fixed so the caller's PATH cannot choose
# which helpers the CLI or its child resolve.
CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"
TLS_PASSTHROUGH_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")

# Runs inside `infisical run` with the fetched secrets in its environment.
# argv: nonce, mode ("exists" or "read"), secret name (last, by convention).
# It prints exactly one JSON line and never writes the value anywhere else.
HELPER_CODE = (
    "import json, os, sys\n"
    "nonce, mode, name = sys.argv[1:4]\n"
    "if any(key.startswith('LD_') for key in os.environ):\n"
    "    sys.exit(3)\n"
    "result = {'nonce': nonce, 'present': name in os.environ}\n"
    "if mode == 'read' and result['present']:\n"
    "    result['value'] = os.environ[name]\n"
    "sys.stdout.write(json.dumps(result) + '\\n')\n"
)


def canonical_domain(url: str) -> str:
    """Return the canonical form of an Infisical server URL.

    Compared as text, without DNS resolution: scheme and host lowercased,
    default port removed, trailing slash of the path removed. URLs with user
    info, a query, or a fragment are rejected.
    """
    invalid = VaultConfigurationError(
        "INFISICAL_DOMAIN must be an http(s) URL without credentials, query, or fragment"
    )
    if not isinstance(url, str) or not url or any(char.isspace() for char in url):
        raise invalid
    if "?" in url or "#" in url:
        raise invalid
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise invalid from error
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in DEFAULT_PORTS or not host or "@" in parts.netloc:
        raise invalid
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


@dataclass(frozen=True)
class InfisicalConnection:
    """Resolved, non-secret description of where secrets are read and written."""

    domain: str
    project_id: str
    path: str

    def to_destination(self) -> dict[str, str]:
        return {
            "backend": BACKEND_NAME,
            "domain": self.domain,
            "project_id": self.project_id,
            "path": self.path,
        }

    @classmethod
    def from_destination(cls, destination: Mapping[str, Any]) -> "InfisicalConnection":
        if (
            set(destination) != set(DESTINATION_FIELDS)
            or destination["backend"] != BACKEND_NAME
            or not all(isinstance(destination[field], str) for field in DESTINATION_FIELDS)
        ):
            raise VaultConfigurationError("plan vault destination is not a valid Infisical destination")
        connection = cls(
            domain=destination["domain"],
            project_id=destination["project_id"],
            path=destination["path"],
        )
        # A recorded destination must already be canonical; it is used as is.
        if (
            canonical_domain(connection.domain) != connection.domain
            or not connection.project_id
            or connection.project_id != connection.project_id.strip()
            or not connection.path.startswith("/")
            or connection.path != connection.path.strip()
        ):
            raise VaultConfigurationError("plan vault destination is not a valid Infisical destination")
        return connection


class InfisicalCliVault(Vault):
    # `secrets set` (CLI v0.43.99, util.SetRawSecrets) lists the folder and
    # then creates missing names and updates present ones: an upsert with no
    # create-only option. create_secret therefore uses the non-atomic
    # check-then-write default.
    atomic_create = False

    def __init__(
        self,
        *,
        domain: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        binary: str | None = None,
        environ: Mapping[str, str] | None = None,
        connection: InfisicalConnection | None = None,
    ):
        self._domain = domain
        # When set, every call uses this destination and the project, path,
        # and domain are never read from the environment again.
        self._fixed_connection = connection
        self.runner = runner
        self._binary = binary
        self._environ = environ if environ is not None else os.environ

    def connection(self) -> InfisicalConnection:
        """Return the destination this adapter reads and writes."""
        if self._fixed_connection is not None:
            return self._fixed_connection
        return self.configured_connection()

    def configured_connection(self) -> InfisicalConnection:
        """Resolve the destination from configuration. Needs no token."""
        project_id = self._environ.get("SECRETOPS_INFISICAL_PROJECT_ID", "").strip()
        path = self._environ.get("SECRETOPS_INFISICAL_PATH", "").strip()
        if not project_id:
            raise VaultConfigurationError("SECRETOPS_INFISICAL_PROJECT_ID is not set")
        if not path.startswith("/"):
            raise VaultConfigurationError("SECRETOPS_INFISICAL_PATH must be set to an absolute folder path")
        domain = canonical_domain(
            self._domain or self._environ.get("INFISICAL_DOMAIN", DEFAULT_DOMAIN)
        )
        return InfisicalConnection(domain=domain, project_id=project_id, path=path)

    def _token(self) -> str:
        token = self._environ.get("INFISICAL_TOKEN", "")
        if not token:
            raise VaultConfigurationError(
                "INFISICAL_TOKEN is not set; machine authentication is required"
            )
        return token

    def _timeout(self) -> int:
        raw = self._environ.get("SECRETOPS_PROVIDER_TIMEOUT_SECONDS", "")
        if not raw:
            return DEFAULT_TIMEOUT_SECONDS
        try:
            timeout = int(raw)
        except ValueError:
            timeout = 0
        if timeout <= 0:
            raise VaultConfigurationError("SECRETOPS_PROVIDER_TIMEOUT_SECONDS must be a positive integer")
        return timeout

    def _binary_path(self) -> str:
        # The CLI receives the token, so it is never looked up through the
        # caller's PATH, which may contain agent-writable directories.
        if self._binary:
            return self._binary
        configured = self._environ.get("SECRETOPS_INFISICAL_BIN", "")
        if configured:
            if not (
                os.path.isabs(configured)
                and os.path.isfile(configured)
                and os.access(configured, os.X_OK)
            ):
                raise VaultConfigurationError(
                    "SECRETOPS_INFISICAL_BIN must be an absolute path to an executable file"
                )
            return configured
        binary = shutil.which("infisical", path=CHILD_PATH)
        if not binary:
            raise VaultConfigurationError(
                "infisical CLI was not found; set SECRETOPS_INFISICAL_BIN"
            )
        return binary

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        # Important:
        # The provider process gets an explicit environment, never the
        # broker's own: no user session, keyring, config, or local variables
        # of the same name as a secret. Provider stdout/stderr are captured
        # and never printed, because provider CLIs can leak values.
        token = self._token()
        timeout = self._timeout()
        with tempfile.TemporaryDirectory(prefix="secretops-home-") as home:
            env = {"PATH": CHILD_PATH, "HOME": home, "INFISICAL_TOKEN": token}
            for name in TLS_PASSTHROUGH_VARS:
                if self._environ.get(name):
                    env[name] = self._environ[name]
            try:
                return self.runner(
                    command,
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                raise VaultTimeoutError("infisical CLI timed out") from error
            except OSError as error:
                raise VaultProviderError("infisical CLI could not be started") from error

    def _query(self, name: str, env: str, mode: str) -> dict:
        name = validate_secret_name(name)
        connection = self.connection()
        binary = self._binary_path()
        nonce = secrets.token_hex(16)
        result = self._run(
            [
                binary,
                "--domain",
                connection.domain,
                "run",
                f"--env={env}",
                f"--projectId={connection.project_id}",
                f"--path={connection.path}",
                "--expand=false",
                "--secret-overriding=false",
                "--include-imports=false",
                "--",
                sys.executable,
                "-I",
                "-c",
                HELPER_CODE,
                nonce,
                mode,
                name,
            ]
        )
        if result.returncode != 0:
            raise VaultProviderError(f"infisical CLI failed with exit status {result.returncode}")
        return self._parse_response(result.stdout, nonce, mode)

    @staticmethod
    def _parse_response(stdout: str, nonce: str, mode: str) -> dict:
        malformed = VaultResponseError("unexpected infisical response")
        lines = stdout.split("\n")
        if len(lines) != 2 or lines[1] != "":
            raise malformed
        try:
            response = json.loads(lines[0])
        except ValueError as error:
            raise malformed from error
        if not isinstance(response, dict) or response.get("nonce") != nonce:
            raise malformed
        present = response.get("present")
        if not isinstance(present, bool):
            raise malformed
        expected_keys = {"nonce", "present"}
        if mode == "read" and present:
            expected_keys.add("value")
            if not isinstance(response.get("value"), str):
                raise malformed
        if set(response) != expected_keys:
            raise malformed
        return response

    def secret_exists(self, name: str, env: str) -> bool:
        return self._query(name, env, "exists")["present"]

    def read_secret_value(self, name: str, env: str) -> str | None:
        # The value stays inside secretops: callers pass it to a target and
        # must never print or log it.
        response = self._query(name, env, "read")
        return response["value"] if response["present"] else None

    def write_secret(self, name: str, env: str, value: str) -> WriteOutcome:
        name = validate_secret_name(name)
        # `secrets set --file` rejects empty and whitespace-only values (Go
        # strings.TrimSpace), so refuse them before the CLI starts.
        if not value.strip():
            raise VaultUnsupportedValueError(
                "infisical CLI cannot store an empty or whitespace-only value"
            )
        connection = self.connection()
        binary = self._binary_path()
        temp_path = None
        try:
            # The value goes in a YAML file passed with --file. `NAME=@path`
            # is not used: with a machine token the CLI stores the literal
            # `@path` instead of the file content. The `.yaml` suffix selects
            # the YAML parser (the .env parser strips spaces and quotes), and
            # every scalar is double-quoted so the CLI's YAML 1.2 parser reads
            # values such as `0o17`, `true` or `null` as strings.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                prefix="secretops-",
                suffix=".yaml",
            ) as temp_file:
                temp_path = temp_file.name
                os.chmod(temp_path, 0o600)
                yaml.safe_dump(
                    {name: value},
                    temp_file,
                    default_style='"',
                    allow_unicode=True,
                    width=float("inf"),
                )
                temp_file.flush()

            result = self._run(
                [
                    binary,
                    "--domain",
                    connection.domain,
                    "secrets",
                    "set",
                    f"--file={temp_path}",
                    "--env",
                    env,
                    f"--projectId={connection.project_id}",
                    f"--path={connection.path}",
                    "--type=shared",
                ]
            )
        except VaultTimeoutError:
            return WriteOutcome.UNKNOWN
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

        # The CLI has started: from here on, only a matching read-back
        # confirms the write. A non-zero exit or a failed read-back does not
        # prove the value was not stored, so it is unknown, never failed.
        if result.returncode != 0:
            return WriteOutcome.UNKNOWN
        try:
            stored = self.read_secret_value(name, env)
        except VaultError:
            return WriteOutcome.UNKNOWN
        return WriteOutcome.CONFIRMED if stored == value else WriteOutcome.UNKNOWN
